"""Durable Plex activation and identity reconciliation.

This module is intentionally small at the worker boundary.  It owns the
activation cut-over and the Plex identity set; planning and delivery remain
consumers of the durable records.  A scan page is committed together with its
cursor, so a process crash can only repeat an idempotent page, never skip one.

The repository uses auxiliary tables instead of changing the existing schema
in this change.  It mirrors the canonical ``activation`` and
``activation_cursors`` rows when those tables are present, which makes the
adapter usable by the current worker while keeping all activation decisions
durable and independently testable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import cast

from .db import Database, LeaderLease, utc_timestamp
from .planner import (
    ActivationDecision,
    ActivationDisposition,
    classify_activation_item,
)

UTC = timezone.utc
DEFAULT_PAGE_SIZE = 100
DEFAULT_ITEMS_PER_RUN = 500
DEFAULT_FRESHNESS = timedelta(days=2)
_PHASES = ("pass1", "pass2")


class RuntimeActivationError(RuntimeError):
    """Base error for a durable activation operation."""


class ActivationBlocked(RuntimeActivationError):
    """The durable activation state is blocked and needs an explicit reset."""


class ScanIntegrityError(RuntimeActivationError):
    """Plex pagination or identity evidence cannot be trusted."""


class LeaderFenced(RuntimeActivationError):
    """A worker attempted a write after its durable leader epoch changed."""


@dataclass(frozen=True, slots=True)
class LibraryTarget:
    """One configured server/library pair.

    ``client`` is normally a :class:`~media_companion.clients.plex.PlexClient`.
    A target may instead provide a ``fetch_page`` callable, useful for a
    worker which already has a typed Plex transport.
    """

    server_uuid: str
    library_uuid: str
    library: object | None = None
    client: object | None = None
    fetch_page: Callable[..., object] | None = None

    def __post_init__(self) -> None:
        for name in ("server_uuid", "library_uuid"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be blank")
            if len(value.encode("utf-8")) > 256:
                raise ValueError(f"{name} is too long")
            object.__setattr__(self, name, value.strip())

    @property
    def key(self) -> str:
        return f"{self.server_uuid}\x1f{self.library_uuid}"


@dataclass(frozen=True, slots=True)
class ScanPage:
    """Normalized bounded page returned by a Plex page provider."""

    items: tuple[object, ...]
    cursor: str | None = None
    next_cursor: str | None = None
    total: int | None = None
    complete: bool | None = None
    has_more: bool | None = None

    def __post_init__(self) -> None:
        if self.total is not None and (
            isinstance(self.total, bool)
            or not isinstance(self.total, int)
            or self.total < 0
        ):
            raise ValueError("page total must be a non-negative integer")
        if self.complete is not None and not isinstance(self.complete, bool):
            raise ValueError("page complete must be boolean")
        if self.has_more is not None and not isinstance(self.has_more, bool):
            raise ValueError("page has_more must be boolean")


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Result of one bounded worker invocation."""

    phase: str
    processed: int
    complete: bool
    status: str
    quarantined: int = 0
    targets_complete: int = 0
    targets_total: int = 0


@dataclass(frozen=True, slots=True)
class ActivationState:
    activation_id: str
    status: str
    baseline_started_at: datetime | None
    baseline_completed_at: datetime | None
    activated_at: datetime | None
    delivery_enabled: bool
    pass1_complete: bool
    pass2_complete: bool
    version: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class IdentityObservation:
    server_uuid: str
    library_uuid: str
    rating_key: str
    logical_key: str
    generation: int
    added_at: datetime | None
    lifecycle_status: str = "active"


def _now(value: datetime | None, clock: Callable[[], datetime]) -> datetime:
    result = value or clock()
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return result.astimezone(UTC)


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    with suppress(ValueError):
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(UTC)
    return None


def _value(item: object, *names: str, default: object = None) -> object:
    if isinstance(item, Mapping):
        for name in names:
            if name in item:
                return item[name]
    for name in names:
        with suppress(Exception):
            value = getattr(item, name)
            if value is not None:
                return value
    return default


def _json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)[
            :4096
        ]
    except (TypeError, ValueError):
        return "{}"


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result or None


def _page_from(value: object, *, requested_cursor: str, page_size: int) -> ScanPage:
    if isinstance(value, ScanPage):
        return value
    items: object = ()
    cursor: object = None
    next_cursor: object = None
    total: object = None
    complete: object = None
    has_more: object = None
    if isinstance(value, Mapping):
        items = value.get("items", value.get("records", value.get("metadata", ())))
        cursor = value.get("cursor", value.get("offset"))
        next_cursor = value.get(
            "next_cursor", value.get("next", value.get("nextCursor"))
        )
        total = value.get("total", value.get("totalSize", value.get("totalCount")))
        complete = value.get("complete")
        has_more = value.get("has_more", value.get("hasMore"))
    else:
        items = _value(value, "items", "records", "metadata", default=())
        cursor = _value(value, "cursor", "offset")
        next_cursor = _value(value, "next_cursor", "next", "nextCursor")
        total = _value(value, "total", "totalSize", "totalCount")
        complete = _value(value, "complete")
        has_more = _value(value, "has_more", "hasMore")
    if isinstance(items, (str, bytes, bytearray)) or not isinstance(items, Iterable):
        raise ScanIntegrityError("Plex page items are not iterable")
    values = tuple(items)
    if len(values) > page_size:
        raise ScanIntegrityError("Plex page exceeds requested bound")
    cursor_text = None if cursor is None else str(cursor)
    next_text = None if next_cursor is None else str(next_cursor)
    total_int: int | None
    if total is None:
        total_int = None
    elif isinstance(total, bool) or not isinstance(total, int) or total < 0:
        try:
            total_int = int(str(total))
        except (TypeError, ValueError) as exc:
            raise ScanIntegrityError("Plex page total is invalid") from exc
        if total_int < 0:
            raise ScanIntegrityError("Plex page total is invalid")
    else:
        total_int = total
    if complete is not None and not isinstance(complete, bool):
        raise ScanIntegrityError("Plex page complete marker is invalid")
    if has_more is not None and not isinstance(has_more, bool):
        raise ScanIntegrityError("Plex page continuation marker is invalid")
    # An ordinary Plex ``Page`` exposes ``truncated``.  Translate it without
    # making a short page the sole proof when a server supplied a total.
    truncated = _value(value, "truncated", default=None)
    if has_more is None and isinstance(truncated, bool):
        has_more = truncated
    if cursor_text is None and requested_cursor:
        cursor_text = requested_cursor
    if next_text is None and (
        has_more is True or (has_more is None and len(values) == page_size)
    ):
        if requested_cursor.isdigit():
            next_text = str(int(requested_cursor or "0") + len(values))
    if complete is None:
        if has_more is False:
            complete = True
        elif next_text is None and total_int is not None:
            complete = int(requested_cursor or "0") + len(values) >= total_int
        elif next_text is None and len(values) < page_size:
            complete = True
    return ScanPage(values, cursor_text, next_text, total_int, complete, has_more)


class DurablePlexActivation:
    """Leader-fenced activation and reconciliation service.

    The worker calls :meth:`run_pass` twice with phase ``1`` and ``2``.  It
    may call it repeatedly; each invocation is bounded by ``items_per_run``.
    After activation, :meth:`run_full_reconciliation` performs the daily full
    identity-set diff.  ``ready`` reads only durable state and the current
    SQLite leader lease.
    """

    def __init__(
        self,
        database: Database,
        targets: Sequence[LibraryTarget | Mapping[str, object]],
        *,
        activation_id: str = "media-companion",
        worker_id: str = "runtime-activation",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        page_size: int = DEFAULT_PAGE_SIZE,
        items_per_run: int = DEFAULT_ITEMS_PER_RUN,
        freshness: timedelta = DEFAULT_FRESHNESS,
        leader_lease_seconds: int = 300,
    ) -> None:
        if not isinstance(targets, Sequence) or not targets:
            raise ValueError("at least one configured Plex library is required")
        if isinstance(page_size, bool) or not 1 <= page_size <= DEFAULT_PAGE_SIZE:
            raise ValueError("page_size must be between 1 and 100")
        if isinstance(items_per_run, bool) or items_per_run < page_size:
            raise ValueError("items_per_run must be at least page_size")
        if freshness <= timedelta(0):
            raise ValueError("freshness must be positive")
        self.database = database
        self.targets = tuple(self._target(value) for value in targets)
        if len({target.key for target in self.targets}) != len(self.targets):
            raise ValueError("configured server/library pairs must be unique")
        self.activation_id = activation_id.strip()
        self.worker_id = worker_id.strip()
        if not self.activation_id or not self.worker_id:
            raise ValueError("activation_id and worker_id must not be blank")
        self.clock = clock
        self.page_size = page_size
        self.items_per_run = items_per_run
        self.freshness = freshness
        self.leader_lease_seconds = leader_lease_seconds
        self._lease: LeaderLease | None = None
        self._ensure_schema()

    @staticmethod
    def _target(value: LibraryTarget | Mapping[str, object]) -> LibraryTarget:
        if isinstance(value, LibraryTarget):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("target must be LibraryTarget or mapping")
        return LibraryTarget(
            str(value.get("server_uuid", value.get("server"))),
            str(value.get("library_uuid", value.get("library"))),
            value.get("library_object", value.get("library")),
            value.get("client", value.get("plex")),
            cast(Callable[..., object] | None, value.get("fetch_page")),
        )

    def _ensure_schema(self) -> None:
        with self.database.transaction() as connection:
            # The canonical migrations are normally applied by app startup.
            # Do not silently invent a partial canonical ledger here.
            has_activation = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='activation'"
            ).fetchone()
            if has_activation is None:
                raise RuntimeActivationError(
                    "canonical database migrations are required"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_activation_state (
                    activation_id TEXT PRIMARY KEY,
                    baseline_started_at TEXT,
                    baseline_completed_at TEXT,
                    activated_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('pending','baseline','active','blocked')),
                    delivery_enabled INTEGER NOT NULL DEFAULT 0 CHECK(delivery_enabled IN (0,1)),
                    pass1_complete INTEGER NOT NULL DEFAULT 0 CHECK(pass1_complete IN (0,1)),
                    pass2_complete INTEGER NOT NULL DEFAULT 0 CHECK(pass2_complete IN (0,1)),
                    last_error TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_activation_scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activation_id TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK(phase IN ('pass1','pass2','incremental','full')),
                    server_uuid TEXT NOT NULL,
                    library_uuid TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    cursor TEXT NOT NULL DEFAULT '',
                    seen_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN ('pending','running','complete','failed')),
                    started_at TEXT,
                    completed_at TEXT,
                    fresh_at TEXT,
                    error_code TEXT,
                    error_text TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(activation_id,phase,server_uuid,library_uuid,generation)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_activation_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER NOT NULL REFERENCES runtime_activation_scans(id) ON DELETE CASCADE,
                    logical_key TEXT NOT NULL,
                    server_uuid TEXT NOT NULL,
                    library_uuid TEXT NOT NULL,
                    rating_key TEXT NOT NULL,
                    tombstone_generation INTEGER NOT NULL DEFAULT 0,
                    added_at TEXT,
                    classification TEXT NOT NULL,
                    UNIQUE(scan_id,logical_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_activation_identities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activation_id TEXT NOT NULL,
                    server_uuid TEXT NOT NULL,
                    library_uuid TEXT NOT NULL,
                    rating_key TEXT NOT NULL,
                    tombstone_generation INTEGER NOT NULL,
                    logical_key TEXT NOT NULL,
                    added_at TEXT,
                    lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('active','tombstone','quarantined')),
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    deleted_at TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(activation_id,server_uuid,library_uuid,rating_key,tombstone_generation)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_activation_quarantine (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activation_id TEXT NOT NULL,
                    scan_id INTEGER,
                    server_uuid TEXT NOT NULL,
                    library_uuid TEXT NOT NULL,
                    rating_key TEXT,
                    logical_key TEXT,
                    reason_code TEXT NOT NULL,
                    added_at TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_activation_scan ON runtime_activation_scans(activation_id,phase,status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_activation_identity ON runtime_activation_identities(activation_id,server_uuid,library_uuid,lifecycle_status)"
            )
            # ``updated_at`` is diagnostic only.  Baseline time is captured
            # later by ``_begin_phase`` immediately before the first page.
            now = utc_timestamp()
            connection.execute(
                """
                INSERT INTO runtime_activation_state(activation_id,status,updated_at)
                VALUES(?, 'pending', ?)
                ON CONFLICT(activation_id) DO NOTHING
                """,
                (self.activation_id, now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO activation(activation_id,status,delivery_enabled,version) VALUES(?, 'pending', 0, 0)",
                (self.activation_id,),
            )

    def _lease_for_write(self, now: datetime) -> LeaderLease:
        lease = self._lease
        if lease is None:
            lease = self.database.acquire_leader(
                self.worker_id,
                lease_name="media",
                lease_seconds=self.leader_lease_seconds,
                now=now,
            )
            if lease is None:
                raise LeaderFenced("another worker owns the media leader lease")
        else:
            renewed = self.database.renew_leader(
                lease,
                lease_seconds=self.leader_lease_seconds,
                now=now,
            )
            if renewed is None:
                # A restarted/paused worker may legitimately reacquire an
                # expired lease.  If another owner won the CAS, acquire
                # returns ``None`` and the write remains fenced.
                reacquired = self.database.acquire_leader(
                    self.worker_id,
                    lease_name="media",
                    lease_seconds=self.leader_lease_seconds,
                    now=now,
                )
                if reacquired is None:
                    self._lease = None
                    raise LeaderFenced("media leader lease expired or was fenced")
                lease = reacquired
            else:
                lease = renewed
        self._lease = lease
        return lease

    @staticmethod
    def _assert_fence(
        connection: sqlite3.Connection, lease: LeaderLease, now_text: str
    ) -> None:
        row = connection.execute(
            "SELECT owner,claim_token,epoch,expires_at FROM leader_leases WHERE lease_name='media'"
        ).fetchone()
        if (
            row is None
            or str(row[0]) != lease.owner
            or str(row[1]) != lease.token
            or int(row[2]) != lease.epoch
            or row[3] is None
            or str(row[3]) <= now_text
        ):
            raise LeaderFenced("write was fenced by a newer leader epoch")

    @staticmethod
    def _row_state(row: sqlite3.Row) -> ActivationState:
        return ActivationState(
            str(row["activation_id"]),
            str(row["status"]),
            _parse_time(row["baseline_started_at"]),
            _parse_time(row["baseline_completed_at"]),
            _parse_time(row["activated_at"]),
            bool(row["delivery_enabled"]),
            bool(row["pass1_complete"]),
            bool(row["pass2_complete"]),
            int(row["version"]),
            None if row["last_error"] is None else str(row["last_error"]),
        )

    def state(self) -> ActivationState:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_activation_state WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
        if row is None:
            raise RuntimeActivationError("activation state is missing")
        return self._row_state(row)

    get_state = state

    def _scan_rows(
        self, connection: sqlite3.Connection, phase: str
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT * FROM runtime_activation_scans
                WHERE activation_id=? AND phase=?
                ORDER BY server_uuid,library_uuid,id
                """,
                (self.activation_id, phase),
            ).fetchall()
        )

    def _ensure_phase_rows(
        self, connection: sqlite3.Connection, phase: str, now_text: str
    ) -> None:
        for target in self.targets:
            row = connection.execute(
                """
                SELECT id FROM runtime_activation_scans
                WHERE activation_id=? AND phase=? AND server_uuid=? AND library_uuid=?
                ORDER BY generation DESC LIMIT 1
                """,
                (self.activation_id, phase, target.server_uuid, target.library_uuid),
            ).fetchone()
            if row is not None:
                continue
            connection.execute(
                """
                INSERT INTO runtime_activation_scans(
                    activation_id,phase,server_uuid,library_uuid,generation,status,version
                ) VALUES(?,?,?,?,1,'pending',0)
                """,
                (self.activation_id, phase, target.server_uuid, target.library_uuid),
            )

    def _begin_phase(self, phase: str, now: datetime, lease: LeaderLease) -> None:
        now_text = utc_timestamp(now)
        with self.database.transaction() as connection:
            self._assert_fence(connection, lease, now_text)
            state_row = connection.execute(
                "SELECT * FROM runtime_activation_state WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
            if state_row is None:
                raise RuntimeActivationError("activation state is missing")
            state = self._row_state(state_row)
            if state.status == "blocked":
                raise ActivationBlocked(state.last_error or "activation is blocked")
            if phase == "pass1" and state.baseline_started_at is None:
                # This is the linearization point: no Plex page is requested
                # before this timestamp is committed.
                changed = connection.execute(
                    """
                    UPDATE runtime_activation_state
                    SET baseline_started_at=?,status='baseline',version=version+1,updated_at=?
                    WHERE activation_id=? AND version=? AND baseline_started_at IS NULL
                    """,
                    (now_text, now_text, self.activation_id, state.version),
                ).rowcount
                if changed != 1:
                    raise LeaderFenced("activation start CAS lost")
                connection.execute(
                    """
                    UPDATE activation SET baseline_started_at=?,status='baseline',version=version+1,updated_at=?
                    WHERE activation_id=?
                    """,
                    (now_text, now_text, self.activation_id),
                )
            self._ensure_phase_rows(connection, phase, now_text)

    def _fetch_page(
        self, target: LibraryTarget, cursor: str, page_size: int
    ) -> ScanPage:
        provider = target.fetch_page
        if callable(provider):
            value = provider(target, cursor, page_size)
            return _page_from(value, requested_cursor=cursor, page_size=page_size)
        client = target.client
        if client is None:
            raise RuntimeActivationError(f"no Plex client for {target.key}")
        method = getattr(client, "fetch_page", None)
        if callable(method):
            return _page_from(
                method(
                    target.library or target.library_uuid,
                    cursor=cursor,
                    limit=page_size,
                ),
                requested_cursor=cursor,
                page_size=page_size,
            )
        method = getattr(client, "library_items", None)
        if callable(method):
            offset = int(cursor or "0") if (cursor or "0").isdigit() else 0
            page = method(
                target.library or target.library_uuid, limit=page_size, offset=offset
            )
            total = _value(page, "total")
            truncated = _value(page, "truncated")
            items = _value(page, "items", default=())
            item_values = (
                tuple(items)
                if isinstance(items, Iterable)
                and not isinstance(items, (str, bytes, bytearray))
                else ()
            )
            return _page_from(
                {
                    "items": item_values,
                    "cursor": str(offset),
                    "next_cursor": str(offset + len(item_values))
                    if bool(truncated)
                    else None,
                    "total": total,
                    "has_more": bool(truncated),
                },
                requested_cursor=cursor,
                page_size=page_size,
            )
        iterator = getattr(client, "iter_library_items", None)
        if callable(iterator):
            offset = int(cursor or "0") if (cursor or "0").isdigit() else 0
            values = tuple(
                iterator(target.library or target.library_uuid, page_size=page_size)
            )
            chunk = values[offset : offset + page_size]
            next_cursor = (
                str(offset + len(chunk)) if offset + len(chunk) < len(values) else None
            )
            return ScanPage(
                chunk, str(offset), next_cursor, len(values), next_cursor is None
            )
        raise RuntimeActivationError(
            f"Plex client has no paginated library method for {target.key}"
        )

    @staticmethod
    def _identity(
        item: object, target: LibraryTarget, generation: int
    ) -> tuple[str, str, datetime | None, object, object]:
        rating = _value(item, "rating_key", "ratingKey", "key")
        if isinstance(rating, int) and not isinstance(rating, bool) and rating > 0:
            rating = str(rating)
        if (
            not isinstance(rating, str)
            or not rating.isdigit()
            or rating.startswith("0")
            or int(rating) <= 0
        ):
            raise ScanIntegrityError("Plex rating key is invalid")
        server = _text(_value(item, "server_uuid", "serverUUID", "serverUuid"))
        library = _text(
            _value(
                item,
                "library_uuid",
                "libraryUUID",
                "libraryUuid",
                "libraryKey",
                "librarySectionID",
            )
        )
        if server is not None and server != target.server_uuid:
            raise ScanIntegrityError("Plex item belongs to a different server")
        if library is not None and library != target.library_uuid:
            raise ScanIntegrityError("Plex item belongs to a different library")
        logical = _text(_value(item, "logical_key", "logicalKey"))
        key = (
            logical
            or f"{target.server_uuid}:{target.library_uuid}:{rating}:{generation}"
        )
        added_raw = _value(item, "added_at", "addedAt")
        added = _parse_time(added_raw)
        coarse: object = _value(item, "coarse", "added_at_coarse", default=False)
        precision = _text(_value(item, "timestamp_precision", "added_at_precision"))
        if precision is not None and precision.lower() in {
            "day",
            "hour",
            "minute",
            "second",
            "coarse",
            "unknown",
        }:
            coarse = True
        ambiguous: object = _value(
            item, "clock_ambiguous", "added_at_ambiguous", default=False
        )
        return rating, key, added, coarse, ambiguous

    def _known_generation(
        self, connection: sqlite3.Connection, target: LibraryTarget, rating: str
    ) -> tuple[int, sqlite3.Row | None]:
        row = connection.execute(
            """
            SELECT * FROM runtime_activation_identities
            WHERE activation_id=? AND server_uuid=? AND library_uuid=? AND rating_key=?
            ORDER BY tombstone_generation DESC LIMIT 1
            """,
            (self.activation_id, target.server_uuid, target.library_uuid, rating),
        ).fetchone()
        return (0, None) if row is None else (int(row["tombstone_generation"]), row)

    def _upsert_identity(
        self,
        connection: sqlite3.Connection,
        target: LibraryTarget,
        item: object,
        *,
        observed_at: datetime,
        phase: str,
        scan_id: int,
        pass_one_keys: set[str],
        baseline_started: datetime,
    ) -> tuple[str, int, int]:
        rating_probe = _value(item, "rating_key", "ratingKey", "key")
        rating = (
            str(rating_probe)
            if isinstance(rating_probe, int) and not isinstance(rating_probe, bool)
            else rating_probe
        )
        if (
            not isinstance(rating, str)
            or not rating.isdigit()
            or rating.startswith("0")
            or int(rating) <= 0
        ):
            raise ScanIntegrityError("Plex rating key is invalid")
        known_generation, existing = self._known_generation(connection, target, rating)
        explicit = _value(item, "tombstone_generation", "generation")
        generation = known_generation
        if (
            isinstance(explicit, int)
            and not isinstance(explicit, bool)
            and explicit >= 0
        ):
            generation = max(generation, explicit)
        rating, logical, added, coarse, ambiguous = self._identity(
            item, target, generation
        )
        if existing is not None and str(existing["lifecycle_status"]) == "tombstone":
            old_added = _parse_time(existing["added_at"])
            if added is None or old_added is None or added <= old_added:
                reason = (
                    "readd_added_at_missing"
                    if added is None or old_added is None
                    else "readd_added_at_not_later"
                )
                connection.execute(
                    "INSERT INTO runtime_activation_quarantine(activation_id,scan_id,server_uuid,library_uuid,rating_key,logical_key,reason_code,added_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        self.activation_id,
                        scan_id,
                        target.server_uuid,
                        target.library_uuid,
                        rating,
                        logical,
                        reason,
                        utc_timestamp(added) if added else None,
                        utc_timestamp(observed_at),
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO runtime_activation_members(scan_id,logical_key,server_uuid,library_uuid,rating_key,tombstone_generation,added_at,classification) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        scan_id,
                        f"{target.server_uuid}:{target.library_uuid}:{rating}:{generation}",
                        target.server_uuid,
                        target.library_uuid,
                        rating,
                        generation,
                        utc_timestamp(added) if added else None,
                        "quarantined",
                    ),
                )
                return logical, generation, 1
            generation += 1
            logical = (
                _text(_value(item, "logical_key", "logicalKey"))
                or f"{target.server_uuid}:{target.library_uuid}:{rating}:{generation}"
            )
        physical_key = (
            f"{target.server_uuid}:{target.library_uuid}:{rating}:{generation}"
        )
        # Pass-one membership is physical identity, not receipt time.  A
        # caller-supplied logical key is retained for provider crosswalks, but
        # the stable member key is always persisted as the physical key.
        member_key = physical_key
        if phase == "pass2":
            if member_key in pass_one_keys:
                classification = "historical"
            else:
                decision = classify_activation_item(
                    {
                        "logical_key": member_key,
                        "added_at": added,
                        "coarse": coarse,
                        "clock_ambiguous": ambiguous,
                    },
                    baseline_started_at=baseline_started,
                )
                classification = decision.disposition.value
                if decision.quarantined:
                    connection.execute(
                        "INSERT INTO runtime_activation_quarantine(activation_id,scan_id,server_uuid,library_uuid,rating_key,logical_key,reason_code,added_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            self.activation_id,
                            scan_id,
                            target.server_uuid,
                            target.library_uuid,
                            rating,
                            member_key,
                            decision.reason,
                            utc_timestamp(added) if added else None,
                            utc_timestamp(observed_at),
                        ),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO runtime_activation_members(scan_id,logical_key,server_uuid,library_uuid,rating_key,tombstone_generation,added_at,classification) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            scan_id,
                            member_key,
                            target.server_uuid,
                            target.library_uuid,
                            rating,
                            generation,
                            utc_timestamp(added) if added else None,
                            classification,
                        ),
                    )
                    return member_key, generation, 1
        else:
            classification = "historical"
        added_text = utc_timestamp(added) if added is not None else None
        existing_identity = connection.execute(
            "SELECT id FROM runtime_activation_identities WHERE activation_id=? AND server_uuid=? AND library_uuid=? AND rating_key=? AND tombstone_generation=?",
            (
                self.activation_id,
                target.server_uuid,
                target.library_uuid,
                rating,
                generation,
            ),
        ).fetchone()
        if existing_identity is None:
            connection.execute(
                "INSERT INTO runtime_activation_identities(activation_id,server_uuid,library_uuid,rating_key,tombstone_generation,logical_key,added_at,lifecycle_status,first_seen_at,last_seen_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                (
                    self.activation_id,
                    target.server_uuid,
                    target.library_uuid,
                    rating,
                    generation,
                    member_key,
                    added_text,
                    "active",
                    utc_timestamp(observed_at),
                    utc_timestamp(observed_at),
                ),
            )
        else:
            connection.execute(
                "UPDATE runtime_activation_identities SET logical_key=?,added_at=COALESCE(added_at,?),lifecycle_status='active',last_seen_at=?,version=version+1 WHERE id=?",
                (
                    member_key,
                    added_text,
                    utc_timestamp(observed_at),
                    int(existing_identity[0]),
                ),
            )
        connection.execute(
            "INSERT OR IGNORE INTO runtime_activation_members(scan_id,logical_key,server_uuid,library_uuid,rating_key,tombstone_generation,added_at,classification) VALUES(?,?,?,?,?,?,?,?)",
            (
                scan_id,
                member_key,
                target.server_uuid,
                target.library_uuid,
                rating,
                generation,
                added_text,
                classification,
            ),
        )
        # The canonical table is a compatibility projection; the runtime
        # table remains the authority for pass generations and classifications.
        connection.execute(
            "INSERT OR IGNORE INTO activation_members(activation_id,logical_key,server_uuid,library_uuid,rating_key,tombstone_generation,pass_number,added_at,classification) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                self.activation_id,
                member_key,
                target.server_uuid,
                target.library_uuid,
                rating,
                generation,
                1 if phase == "pass1" else 2,
                added_text,
                classification,
            ),
        )
        return (
            member_key,
            generation,
            1 if classification == ActivationDisposition.QUARANTINED.value else 0,
        )

    def _mark_scan_failed(
        self, scan_id: int, error: BaseException, now: datetime, lease: LeaderLease
    ) -> None:
        now_text = utc_timestamp(now)
        safe = f"{type(error).__name__}:{str(error)[:256]}"
        with self.database.transaction() as connection:
            self._assert_fence(connection, lease, now_text)
            connection.execute(
                "UPDATE runtime_activation_scans SET status='failed',error_code=?,error_text=?,version=version+1 WHERE id=?",
                (type(error).__name__, safe, scan_id),
            )
            state = connection.execute(
                "SELECT version FROM runtime_activation_state WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
            if state is not None:
                connection.execute(
                    "UPDATE runtime_activation_state SET status='blocked',last_error=?,version=version+1,updated_at=? WHERE activation_id=? AND version=?",
                    (safe, now_text, self.activation_id, int(state[0])),
                )
            connection.execute(
                "UPDATE activation SET status='blocked',delivery_enabled=0,version=version+1,updated_at=? WHERE activation_id=?",
                (now_text, self.activation_id),
            )

    def run_pass(
        self,
        pass_number: int,
        *,
        now: datetime | None = None,
        items_per_run: int | None = None,
    ) -> ScanResult:
        if pass_number not in (1, 2):
            raise ValueError("pass_number must be 1 or 2")
        phase = _PHASES[pass_number - 1]
        current = _now(now, self.clock)
        lease = self._lease_for_write(current)
        state = self.state()
        if pass_number == 2 and not state.pass1_complete:
            raise ActivationBlocked("pass one is not complete")
        self._begin_phase(phase, current, lease)
        budget = items_per_run or self.items_per_run
        if isinstance(budget, bool) or budget < 1:
            raise ValueError("items_per_run must be positive")
        processed = 0
        quarantined = 0
        pass_one_keys: set[str] = set()
        baseline = self.state().baseline_started_at
        if baseline is None:
            raise RuntimeActivationError("baseline start is missing")
        with self.database.connection() as connection:
            if phase == "pass2":
                rows = connection.execute(
                    "SELECT logical_key FROM runtime_activation_members m JOIN runtime_activation_scans s ON s.id=m.scan_id WHERE s.activation_id=? AND s.phase='pass1' AND s.status='complete'",
                    (self.activation_id,),
                ).fetchall()
                pass_one_keys = {str(row[0]) for row in rows}
        for target in self.targets:
            with self.database.connection() as connection:
                scan = connection.execute(
                    "SELECT * FROM runtime_activation_scans WHERE activation_id=? AND phase=? AND server_uuid=? AND library_uuid=? ORDER BY generation DESC LIMIT 1",
                    (
                        self.activation_id,
                        phase,
                        target.server_uuid,
                        target.library_uuid,
                    ),
                ).fetchone()
            if scan is None or str(scan["status"]) == "complete":
                continue
            if str(scan["status"]) == "failed":
                raise ActivationBlocked(str(scan["error_text"] or "scan failed"))
            cursor = str(scan["cursor"] or "")
            scan_id = int(scan["id"])
            if str(scan["status"]) == "pending":
                now_text = utc_timestamp(current)
                with self.database.transaction() as connection:
                    self._assert_fence(connection, lease, now_text)
                    connection.execute(
                        "UPDATE runtime_activation_scans SET status='running',started_at=?,version=version+1 WHERE id=? AND status='pending'",
                        (now_text, scan_id),
                    )
            while processed < budget:
                try:
                    page = self._fetch_page(
                        target, cursor, min(self.page_size, budget - processed)
                    )
                    if (
                        page.cursor is not None
                        and not (cursor == "" and str(page.cursor) in {"", "0"})
                        and str(page.cursor) != cursor
                    ):
                        raise ScanIntegrityError(
                            "Plex cursor changed during page fetch"
                        )
                    if (
                        page.total is not None
                        and int(scan["seen_count"]) + len(page.items) > page.total
                    ):
                        raise ScanIntegrityError(
                            "Plex total is lower than observed identity count"
                        )
                    page_keys: set[str] = set()
                    with self.database.connection() as prior_connection:
                        prior_keys = {
                            str(item[0])
                            for item in prior_connection.execute(
                                "SELECT rating_key FROM runtime_activation_members WHERE scan_id=?",
                                (scan_id,),
                            ).fetchall()
                        }
                    for item in page.items:
                        rating_probe = _value(item, "rating_key", "ratingKey", "key")
                        rating = (
                            str(rating_probe)
                            if isinstance(rating_probe, int)
                            else rating_probe
                        )
                        if (
                            not isinstance(rating, str)
                            or rating in page_keys
                            or rating in prior_keys
                        ):
                            raise ScanIntegrityError("Plex page repeats an identity")
                        page_keys.add(rating)
                    with self.database.transaction() as connection:
                        self._assert_fence(connection, lease, utc_timestamp(current))
                        count_before = int(
                            connection.execute(
                                "SELECT seen_count FROM runtime_activation_scans WHERE id=?",
                                (scan_id,),
                            ).fetchone()[0]
                        )
                        for item in page.items:
                            _, _, q = self._upsert_identity(
                                connection,
                                target,
                                item,
                                observed_at=current,
                                phase=phase,
                                scan_id=scan_id,
                                pass_one_keys=pass_one_keys,
                                baseline_started=baseline,
                            )
                            quarantined += q
                            connection.execute(
                                "UPDATE runtime_activation_scans SET seen_count=seen_count+1 WHERE id=?",
                                (scan_id,),
                            )
                        done = bool(page.complete is True)
                        if page.complete is False and page.next_cursor is None:
                            raise ScanIntegrityError(
                                "Plex page is explicitly incomplete without a cursor"
                            )
                        next_cursor = page.next_cursor
                        if next_cursor is not None and str(next_cursor) == cursor:
                            raise ScanIntegrityError("Plex cursor regressed")
                        if (
                            page.total is not None
                            and next_cursor is None
                            and count_before + len(page.items) < page.total
                        ):
                            raise ScanIntegrityError(
                                "Plex page ended before reported total"
                            )
                        if page.has_more is True and next_cursor is None:
                            raise ScanIntegrityError(
                                "Plex page reports more items without cursor"
                            )
                        if (
                            next_cursor is None
                            and page.complete is not True
                            and len(page.items)
                            == min(self.page_size, budget - processed)
                        ):
                            # A bounded worker invocation may stop at its own
                            # budget; derive an offset cursor when possible.
                            if cursor.isdigit():
                                next_cursor = str(int(cursor or "0") + len(page.items))
                            else:
                                raise ScanIntegrityError(
                                    "opaque Plex page lacks continuation cursor"
                                )
                        complete = done or next_cursor is None
                        connection.execute(
                            "UPDATE runtime_activation_scans SET cursor=?,status=?,completed_at=CASE WHEN ? THEN ? ELSE completed_at END,fresh_at=CASE WHEN ? THEN ? ELSE fresh_at END,version=version+1 WHERE id=?",
                            (
                                "" if next_cursor is None else str(next_cursor),
                                "complete" if complete else "running",
                                int(complete),
                                utc_timestamp(current),
                                int(complete),
                                utc_timestamp(current),
                                scan_id,
                            ),
                        )
                        cursor = "" if next_cursor is None else str(next_cursor)
                    processed += len(page.items)
                    if complete:
                        break
                    if not page.items and next_cursor is not None:
                        # Empty pages are valid only if the server advances a
                        # cursor; keep bounded progress and avoid a spin.
                        continue
                    if not page.items:
                        raise ScanIntegrityError("Plex page made no progress")
                except Exception as exc:
                    if isinstance(exc, LeaderFenced):
                        raise
                    self._mark_scan_failed(scan_id, exc, current, lease)
                    raise ScanIntegrityError(str(exc)) from exc
            # A run budget can end in the middle of a target.  The cursor and
            # all identities are already committed; restart simply continues.
            if processed >= budget:
                break
        with self.database.connection() as connection:
            rows = self._scan_rows(connection, phase)
            complete_count = sum(str(row["status"]) == "complete" for row in rows)
            all_complete = bool(rows) and complete_count == len(rows)
        if all_complete:
            self._finish_phase(phase, current, lease)
        return ScanResult(
            phase,
            processed,
            all_complete,
            "complete" if all_complete else "running",
            quarantined,
            complete_count,
            len(rows),
        )

    def _finish_phase(self, phase: str, now: datetime, lease: LeaderLease) -> None:
        now_text = utc_timestamp(now)
        with self.database.transaction() as connection:
            self._assert_fence(connection, lease, now_text)
            state_row = connection.execute(
                "SELECT * FROM runtime_activation_state WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
            if state_row is None:
                raise RuntimeActivationError("activation state is missing")
            state = self._row_state(state_row)
            if phase == "pass1":
                connection.execute(
                    "UPDATE runtime_activation_state SET pass1_complete=1,version=version+1,updated_at=? WHERE activation_id=? AND version=?",
                    (now_text, self.activation_id, state.version),
                )
            else:
                if not state.pass1_complete:
                    raise ActivationBlocked("pass one is not complete")
                connection.execute(
                    "UPDATE runtime_activation_state SET pass2_complete=1,status='active',baseline_completed_at=?,activated_at=?,delivery_enabled=0,version=version+1,updated_at=? WHERE activation_id=? AND version=?",
                    (now_text, now_text, now_text, self.activation_id, state.version),
                )
                connection.execute(
                    "UPDATE activation SET status='active',baseline_completed_at=?,activated_at=?,delivery_enabled=0,version=version+1,updated_at=? WHERE activation_id=?",
                    (now_text, now_text, now_text, self.activation_id),
                )
            # Pass-two completion is the overlap/full-sweep evidence for the
            # cut-over.  Later daily scans replace this durable freshness.
            for target in self.targets:
                connection.execute(
                    "INSERT INTO activation_cursors(activation_id,server_uuid,library_uuid,scan_generation,last_incremental_at,last_full_sweep_at,status,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(activation_id,server_uuid,library_uuid) DO UPDATE SET scan_generation=excluded.scan_generation,last_incremental_at=excluded.last_incremental_at,last_full_sweep_at=excluded.last_full_sweep_at,status=excluded.status,updated_at=excluded.updated_at",
                    (
                        self.activation_id,
                        target.server_uuid,
                        target.library_uuid,
                        1,
                        now_text,
                        now_text,
                        "complete",
                        now_text,
                    ),
                )

    def enable_delivery(
        self, *, now: datetime | None = None, accounting_ready: bool = True
    ) -> bool:
        current = _now(now, self.clock)
        lease = self._lease_for_write(current)
        state = self.state()
        if not state.pass2_complete or state.status != "active" or not accounting_ready:
            return False
        if not self._all_full_fresh(current):
            return False
        with self.database.transaction() as connection:
            self._assert_fence(connection, lease, utc_timestamp(current))
            changed = connection.execute(
                "UPDATE runtime_activation_state SET delivery_enabled=1,version=version+1,updated_at=? WHERE activation_id=? AND version=? AND status='active' AND pass2_complete=1",
                (utc_timestamp(current), self.activation_id, state.version),
            ).rowcount
            if changed:
                connection.execute(
                    "UPDATE activation SET delivery_enabled=1,version=version+1,updated_at=? WHERE activation_id=?",
                    (utc_timestamp(current), self.activation_id),
                )
            return changed == 1

    def _all_full_fresh(self, now: datetime) -> bool:
        with self.database.connection() as connection:
            full_rows = connection.execute(
                """
                SELECT s.* FROM runtime_activation_scans s
                JOIN (
                    SELECT server_uuid,library_uuid,MAX(generation) AS generation
                    FROM runtime_activation_scans
                    WHERE activation_id=? AND phase='full'
                    GROUP BY server_uuid,library_uuid
                ) latest ON latest.server_uuid=s.server_uuid
                    AND latest.library_uuid=s.library_uuid
                    AND latest.generation=s.generation
                WHERE s.activation_id=? AND s.phase='full'
                """,
                (self.activation_id, self.activation_id),
            ).fetchall()
            if full_rows:
                rows = full_rows
            else:
                rows = connection.execute(
                    "SELECT * FROM runtime_activation_scans WHERE activation_id=? AND phase='pass2' ORDER BY server_uuid,library_uuid",
                    (self.activation_id,),
                ).fetchall()
        if len(rows) != len(self.targets) or any(
            str(row["status"]) != "complete" for row in rows
        ):
            return False
        return all(
            (fresh := _parse_time(row["fresh_at"])) is not None
            and now - fresh <= self.freshness
            for row in rows
        )

    def ready(self, *, now: datetime | None = None) -> bool:
        current = _now(now, self.clock)
        state = self.state()
        if (
            state.status != "active"
            or not state.delivery_enabled
            or not state.pass2_complete
        ):
            return False
        if not self._all_full_fresh(current):
            return False
        try:
            leader = self.database.current_leader("media", now=current)
        except Exception:
            return False
        return bool(
            leader is not None
            and leader.owner == self.worker_id
            and self._lease is not None
            and leader.epoch == self._lease.epoch
        )

    is_ready = ready

    def classify_observation(self, item: object) -> ActivationDecision:
        """Apply the durable baseline rule to a late webhook observation."""

        state = self.state()
        if state.baseline_started_at is None:
            raise ActivationBlocked("baseline has not started")
        key = _text(_value(item, "logical_key", "logicalKey"))
        if key is None:
            rating = _value(item, "rating_key", "ratingKey", "key")
            if rating is not None:
                server = _text(_value(item, "server_uuid", "serverUUID", "serverUuid"))
                library = _text(
                    _value(item, "library_uuid", "libraryUUID", "libraryUuid")
                )
                generation = _value(
                    item, "tombstone_generation", "generation", default=0
                )
                if server is None and library is None and len(self.targets) == 1:
                    server, library = (
                        self.targets[0].server_uuid,
                        self.targets[0].library_uuid,
                    )
                if server is not None and library is not None:
                    key = f"{server}:{library}:{rating}:{generation}"
                else:
                    key = str(rating)
            else:
                key = None
        return classify_activation_item(
            {
                "logical_key": key,
                "added_at": _value(item, "added_at", "addedAt"),
                "coarse": _value(item, "coarse", "added_at_coarse", default=False),
                "clock_ambiguous": _value(
                    item, "clock_ambiguous", "added_at_ambiguous", default=False
                ),
            },
            baseline_started_at=state.baseline_started_at,
            pass_one_membership=self.pass_one_members(),
        )

    def pass_one_members(self) -> frozenset[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT logical_key FROM runtime_activation_members m JOIN runtime_activation_scans s ON s.id=m.scan_id WHERE s.activation_id=? AND s.phase='pass1' AND s.status='complete'",
                (self.activation_id,),
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def run_full_reconciliation(
        self, *, now: datetime | None = None, items_per_run: int | None = None
    ) -> ScanResult:
        """Run a bounded full identity-set diff after activation.

        Active identities are tombstoned only in the transaction that marks a
        target's scan complete.  A failed or partial page therefore cannot
        erase content.  A later observation with a strictly newer verified
        ``addedAt`` receives a new tombstone generation.
        """

        current = _now(now, self.clock)
        if self.state().status != "active":
            raise ActivationBlocked("activation is not active")
        lease = self._lease_for_write(current)
        budget = items_per_run or self.items_per_run
        processed = 0
        quarantined = 0
        for target in self.targets:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM runtime_activation_scans WHERE activation_id=? AND phase='full' AND server_uuid=? AND library_uuid=? ORDER BY generation DESC LIMIT 1",
                    (self.activation_id, target.server_uuid, target.library_uuid),
                ).fetchone()
            if row is None:
                with self.database.transaction() as connection:
                    self._assert_fence(connection, lease, utc_timestamp(current))
                    connection.execute(
                        "INSERT INTO runtime_activation_scans(activation_id,phase,server_uuid,library_uuid,generation,status,version) VALUES(?,?,?,?,1,'pending',0)",
                        (
                            self.activation_id,
                            "full",
                            target.server_uuid,
                            target.library_uuid,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM runtime_activation_scans WHERE activation_id=? AND phase='full' AND server_uuid=? AND library_uuid=? ORDER BY generation DESC LIMIT 1",
                        (self.activation_id, target.server_uuid, target.library_uuid),
                    ).fetchone()
            if row is not None and str(row["status"]) == "complete":
                # Every invocation is a new identity-set generation.  A
                # completed scan is immutable evidence for its tombstone
                # diff; never append a later day to that set.
                with self.database.transaction() as connection:
                    self._assert_fence(connection, lease, utc_timestamp(current))
                    generation = int(row["generation"]) + 1
                    connection.execute(
                        "INSERT INTO runtime_activation_scans(activation_id,phase,server_uuid,library_uuid,generation,status,version) VALUES(?,?,?,?,?,'pending',0)",
                        (
                            self.activation_id,
                            "full",
                            target.server_uuid,
                            target.library_uuid,
                            generation,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM runtime_activation_scans WHERE activation_id=? AND phase='full' AND server_uuid=? AND library_uuid=? AND generation=?",
                        (
                            self.activation_id,
                            target.server_uuid,
                            target.library_uuid,
                            generation,
                        ),
                    ).fetchone()
            if row is None:
                continue
            if str(row["status"]) == "failed":
                raise ActivationBlocked(
                    str(row["error_text"] or "full reconciliation failed")
                )
            scan_id = int(row["id"])
            cursor = str(row["cursor"] or "")
            if str(row["status"]) == "pending":
                with self.database.transaction() as connection:
                    self._assert_fence(connection, lease, utc_timestamp(current))
                    connection.execute(
                        "UPDATE runtime_activation_scans SET status='running',started_at=?,version=version+1 WHERE id=? AND status='pending'",
                        (utc_timestamp(current), scan_id),
                    )
            seen: set[str] = set()
            while processed < budget:
                try:
                    page = self._fetch_page(
                        target, cursor, min(self.page_size, budget - processed)
                    )
                    if (
                        page.cursor is not None
                        and not (cursor == "" and str(page.cursor) in {"", "0"})
                        and str(page.cursor) != cursor
                    ):
                        raise ScanIntegrityError(
                            "Plex cursor changed during full sweep"
                        )
                    with self.database.connection() as prior_connection:
                        prior_keys = {
                            str(item[0])
                            for item in prior_connection.execute(
                                "SELECT rating_key FROM runtime_activation_members WHERE scan_id=?",
                                (scan_id,),
                            ).fetchall()
                        }
                    for item in page.items:
                        rating_probe = _value(item, "rating_key", "ratingKey", "key")
                        rating = (
                            str(rating_probe)
                            if isinstance(rating_probe, int)
                            else rating_probe
                        )
                        if (
                            not isinstance(rating, str)
                            or rating in seen
                            or rating in prior_keys
                        ):
                            raise ScanIntegrityError("full sweep repeated an identity")
                        seen.add(rating)
                    with self.database.transaction() as connection:
                        self._assert_fence(connection, lease, utc_timestamp(current))
                        for item in page.items:
                            _, _, q = self._upsert_identity(
                                connection,
                                target,
                                item,
                                observed_at=current,
                                phase="full",
                                scan_id=scan_id,
                                pass_one_keys=set(),
                                baseline_started=self.state().baseline_started_at
                                or current,
                            )
                            quarantined += q
                        next_cursor = page.next_cursor
                        if page.complete is False and next_cursor is None:
                            raise ScanIntegrityError("full sweep page incomplete")
                        if next_cursor is not None and str(next_cursor) == cursor:
                            raise ScanIntegrityError("full sweep cursor regressed")
                        if page.has_more is True and next_cursor is None:
                            raise ScanIntegrityError(
                                "full sweep continuation is missing"
                            )
                        complete = page.complete is True or next_cursor is None
                        if next_cursor is None and not complete and cursor.isdigit():
                            next_cursor = str(int(cursor or "0") + len(page.items))
                        if not page.items and not complete:
                            raise ScanIntegrityError("full sweep made no progress")
                        connection.execute(
                            "UPDATE runtime_activation_scans SET cursor=?,seen_count=seen_count+?,status=?,completed_at=CASE WHEN ? THEN ? ELSE completed_at END,fresh_at=CASE WHEN ? THEN ? ELSE fresh_at END,version=version+1 WHERE id=?",
                            (
                                "" if next_cursor is None else str(next_cursor),
                                len(page.items),
                                "complete" if complete else "running",
                                int(complete),
                                utc_timestamp(current),
                                int(complete),
                                utc_timestamp(current),
                                scan_id,
                            ),
                        )
                        cursor = "" if next_cursor is None else str(next_cursor)
                        if complete:
                            full_seen = {
                                str(item[0])
                                for item in connection.execute(
                                    "SELECT rating_key FROM runtime_activation_members WHERE scan_id=?",
                                    (scan_id,),
                                ).fetchall()
                            }
                            active = connection.execute(
                                "SELECT id,rating_key,tombstone_generation FROM runtime_activation_identities WHERE activation_id=? AND server_uuid=? AND library_uuid=? AND lifecycle_status='active'",
                                (
                                    self.activation_id,
                                    target.server_uuid,
                                    target.library_uuid,
                                ),
                            ).fetchall()
                            for old in active:
                                if str(old[1]) not in full_seen:
                                    connection.execute(
                                        "UPDATE runtime_activation_identities SET lifecycle_status='tombstone',deleted_at=?,last_seen_at=?,version=version+1 WHERE id=? AND lifecycle_status='active'",
                                        (
                                            utc_timestamp(current),
                                            utc_timestamp(current),
                                            int(old[0]),
                                        ),
                                    )
                    processed += len(page.items)
                    if complete:
                        break
                except Exception as exc:
                    if isinstance(exc, LeaderFenced):
                        raise
                    self._mark_scan_failed(scan_id, exc, current, lease)
                    raise ScanIntegrityError(str(exc)) from exc
            if processed >= budget:
                break
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT s.* FROM runtime_activation_scans s
                JOIN (
                    SELECT server_uuid,library_uuid,MAX(generation) AS generation
                    FROM runtime_activation_scans
                    WHERE activation_id=? AND phase='full'
                    GROUP BY server_uuid,library_uuid
                ) latest ON latest.server_uuid=s.server_uuid
                    AND latest.library_uuid=s.library_uuid
                    AND latest.generation=s.generation
                WHERE s.activation_id=? AND s.phase='full'
                """,
                (self.activation_id, self.activation_id),
            ).fetchall()
            complete_count = sum(str(row["status"]) == "complete" for row in rows)
            all_complete = (
                bool(rows)
                and len(rows) == len(self.targets)
                and complete_count == len(rows)
            )
        return ScanResult(
            "full",
            processed,
            all_complete,
            "complete" if all_complete else "running",
            quarantined,
            complete_count,
            len(self.targets),
        )

    full_reconciliation = run_full_reconciliation

    def reset_blocked(self, *, now: datetime | None = None) -> None:
        """Start a fresh activation generation after an operator-reviewed failure."""

        current = _now(now, self.clock)
        lease = self._lease_for_write(current)
        with self.database.transaction() as connection:
            self._assert_fence(connection, lease, utc_timestamp(current))
            connection.execute(
                "DELETE FROM runtime_activation_members WHERE scan_id IN (SELECT id FROM runtime_activation_scans WHERE activation_id=? AND phase IN ('pass1','pass2'))",
                (self.activation_id,),
            )
            connection.execute(
                "DELETE FROM runtime_activation_scans WHERE activation_id=? AND phase IN ('pass1','pass2')",
                (self.activation_id,),
            )
            connection.execute(
                "UPDATE runtime_activation_state SET baseline_started_at=NULL,baseline_completed_at=NULL,activated_at=NULL,status='pending',delivery_enabled=0,pass1_complete=0,pass2_complete=0,last_error=NULL,version=version+1,updated_at=? WHERE activation_id=?",
                (utc_timestamp(current), self.activation_id),
            )
            connection.execute(
                "UPDATE activation SET baseline_started_at=NULL,baseline_completed_at=NULL,activated_at=NULL,status='pending',delivery_enabled=0,version=version+1,updated_at=? WHERE activation_id=?",
                (utc_timestamp(current), self.activation_id),
            )

    def status(self) -> Mapping[str, object]:
        state = self.state()
        with self.database.connection() as connection:
            scans = connection.execute(
                "SELECT phase,status,COUNT(*) AS count FROM runtime_activation_scans WHERE activation_id=? GROUP BY phase,status",
                (self.activation_id,),
            ).fetchall()
            quarantined = connection.execute(
                "SELECT COUNT(*) FROM runtime_activation_quarantine WHERE activation_id=? AND resolved_at IS NULL",
                (self.activation_id,),
            ).fetchone()
        return {
            "activation_id": state.activation_id,
            "status": state.status,
            "baseline_started_at": utc_timestamp(state.baseline_started_at)
            if state.baseline_started_at
            else None,
            "baseline_completed_at": utc_timestamp(state.baseline_completed_at)
            if state.baseline_completed_at
            else None,
            "activated_at": utc_timestamp(state.activated_at)
            if state.activated_at
            else None,
            "delivery_enabled": state.delivery_enabled,
            "ready": self.ready(),
            "quarantined": int(quarantined[0] if quarantined else 0),
            "scans": [
                {"phase": str(row[0]), "status": str(row[1]), "count": int(row[2])}
                for row in scans
            ],
        }


# Names used by composition code and integration tests during the package
# migration.  They are aliases, not separate implementations.
RuntimeActivation = DurablePlexActivation
PlexActivationRuntime = DurablePlexActivation
ActivationRepository = DurablePlexActivation

__all__ = [
    "ActivationBlocked",
    "ActivationRepository",
    "ActivationState",
    "DurablePlexActivation",
    "IdentityObservation",
    "LeaderFenced",
    "LibraryTarget",
    "PlexActivationRuntime",
    "RuntimeActivation",
    "RuntimeActivationError",
    "ScanIntegrityError",
    "ScanPage",
    "ScanResult",
]
