"""The checked-in production composition for Media Companion.

This module is deliberately boring at the edges: :mod:`media_companion.app`
constructs the durable authentication and policy dependencies and calls
``build_runtime`` with them.  The composition below wires those dependencies
to the real provider clients, the request repository, the bounded safe views,
and one SQLite-fenced worker.  It is also intentionally usable with injected
clients in integration tests; the injected objects still have to implement
the same narrow client methods as the real adapters.

There are no process-local sources of truth in this module.  Cursor snapshots,
Plex webhook observations, claims, activation state, notification groups, and
delivery state all live in the companion SQLite ledger.  A few small caches
(for example a last-read readiness timestamp) are only liveness hints and are
never used to decide whether an item is available or whether a notification
has been sent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import html
import inspect
import json
import re
import secrets
import time
from typing import Any, cast

from .auth import ConfirmationRecord, canonical_argument_hash
from .clients.plex import PlexClient
from .clients.radarr import FileSecretReader, RadarrClient
from .clients.sonarr import SonarrClient
from .clients.telegram import TelegramClient, TelegramError, TelegramErrorClass
from .cursors import CursorSigner, SnapshotRecord, SnapshotStore, binding_hash
from .db import ClaimToken, Database, LeaderLease, utc_timestamp
from .models import (
    MediaCandidate,
    MediaIdentity,
    MediaStatus,
    MediaType,
    PartialError,
    PlexItem,
    QueueItem,
    ServiceName,
)
from .operations import (
    DASHBOARD_OPERATION_SET,
    SHARED_TOOL_SET,
    CompanionRuntime,
    SQLiteMutationGuard,
)
from .plex_ingress import NormalizedPlexEvent
from .requests import (
    MovieProvider,
    PlexVisibilityProvider,
    RequestActor,
    RequestWorkflow,
    SQLiteRequestStore,
    SeriesProvider,
)
from .safe_views import (
    SafePage,
    SafeRequestStatus,
    SafeServiceHealth,
    SafeViewPaginator,
    sanitize_library_item,
    sanitize_media_candidate,
    sanitize_media_status,
    sanitize_partial_error,
    sanitize_queue_item,
    sanitize_request_status,
    sanitize_service_health,
)


UTC = timezone.utc
INCREMENTAL_SECONDS = 5 * 60
FULL_RECONCILIATION_SECONDS = 24 * 60 * 60
LEADER_LEASE_SECONDS = 300
CLAIM_LEASE_SECONDS = 300
RETENTION_SECONDS = 60 * 24 * 60 * 60
QUARANTINE_ALERT_SECONDS = 30 * 24 * 60 * 60
MAX_INBOX_BATCH = 100
MAX_SWEEP_ITEMS = 5_000
MAX_DELIVERIES_BATCH = 100


class ProductionRuntimeError(RuntimeError):
    """A required production dependency or invariant is unavailable."""


class WorkerNotLeader(ProductionRuntimeError):
    """The current worker does not hold the durable fencing lease."""


class WorkerNotReady(ProductionRuntimeError):
    """A worker operation was requested before the leader/sweep boundary."""


class ProviderRetryable(ProductionRuntimeError):
    """A provider observation can be retried without changing ledger truth."""


class ProviderQuarantine(ProductionRuntimeError):
    """A provider observation is inconsistent and must be quarantined."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return utc_timestamp(value or _now())


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _bounded_json(value: object, *, depth: int = 0) -> object:
    """Copy a small, redacted JSON-shaped value for durable operational rows."""

    if depth > 6:
        return "<bounded>"
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str):
            return value[:4_096]
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in list(value.items())[:128]:
            if not isinstance(key, str):
                continue
            lowered = key.lower()
            if any(
                word in lowered
                for word in ("token", "secret", "password", "api_key", "path", "raw")
            ):
                continue
            result[key[:128]] = _bounded_json(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_json(item, depth=depth + 1) for item in list(value)[:128]]
    return str(type(value).__name__)


def _json(value: object) -> str:
    encoded = json.dumps(
        _bounded_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return encoded[: 64 * 1024]


def _positive_id(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _actor_values(
    claims: object | None,
    arguments: Mapping[str, object] | None = None,
    *,
    allow_argument_fallback: bool = False,
) -> tuple[int, int, str | None, int | None, str]:
    """Extract trusted assertion fields.

    Production handlers deliberately do not infer identity from model input.
    The optional fallback exists only for narrowly-scoped direct unit tests and
    is never enabled by ``ProductionOperations``.
    """

    source = claims
    user_id = getattr(source, "user_id", None) if source is not None else None
    chat_id = getattr(source, "chat_id", None) if source is not None else None
    username = getattr(source, "username", None) if source is not None else None
    update_id = getattr(source, "update_id", None) if source is not None else None
    chat_type = (
        getattr(source, "chat_type", "private") if source is not None else "private"
    )
    fingerprint = (
        getattr(source, "allowlist_fingerprint", None) if source is not None else None
    )
    if isinstance(source, Mapping):
        user_id = source.get("user_id", user_id)
        chat_id = source.get("chat_id", chat_id)
        username = source.get("username", username)
        update_id = source.get("update_id", update_id)
        chat_type = source.get("chat_type", chat_type)
        fingerprint = source.get("allowlist_fingerprint", fingerprint)
    if (
        allow_argument_fallback
        and not isinstance(user_id, int)
        and arguments is not None
    ):
        user_id = arguments.get("requested_by_user_id", arguments.get("user_id"))
    if (
        allow_argument_fallback
        and not isinstance(chat_id, int)
        and arguments is not None
    ):
        chat_id = arguments.get("requested_by_chat_id", arguments.get("chat_id"))
    if (
        not isinstance(user_id, int)
        or user_id <= 0
        or not isinstance(chat_id, int)
        or chat_id == 0
    ):
        raise ProductionRuntimeError("trusted actor identity is unavailable")
    return (
        user_id,
        chat_id,
        username if isinstance(username, str) else None,
        update_id if isinstance(update_id, int) else None,
        str(fingerprint or ""),
    )


def _actor_provenance(claims: object | None) -> tuple[str, str]:
    """Return provenance carried by the trusted actor assertion."""

    if claims is None:
        raise ProductionRuntimeError("trusted actor provenance is unavailable")
    if isinstance(claims, Mapping):
        chat_type = claims.get("chat_type", "private")
        update_type = claims.get("update_type", "message")
    else:
        chat_type = getattr(claims, "chat_type", "private")
        update_type = getattr(claims, "update_type", "message")
    if not isinstance(chat_type, str) or not chat_type.strip():
        raise ProductionRuntimeError("trusted chat provenance is invalid")
    if not isinstance(update_type, str) or not update_type.strip():
        raise ProductionRuntimeError("trusted update provenance is invalid")
    return chat_type.strip(), update_type.strip()


def _call(function: object, *args: object, **kwargs: object) -> object:
    """Call a narrow fake or real client while tolerating optional keyword differences."""

    if not callable(function):
        raise ProductionRuntimeError("dependency method is unavailable")
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args)  # type: ignore[misc]
    params = signature.parameters
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return function(*args, **kwargs)  # type: ignore[misc]
    accepted = {key: value for key, value in kwargs.items() if key in params}
    return function(*args, **accepted)  # type: ignore[misc]


def _value(obj: object, *names: str, default: object = None) -> object:
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if value is not None:
            return value
    return default


class SQLitePlexRateLimiter:
    """Durable ingress limiter using the existing idempotency ledger."""

    def __init__(
        self, database: Database, *, per_minute: int = 120, burst: int = 240
    ) -> None:
        self.database = database
        self.per_minute = per_minute
        self.burst = burst

    def allow(self, *args: object, **kwargs: object) -> bool:
        now = int(time.time())
        minute = now // 60
        burst_start = now // 10
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    "DELETE FROM idempotency_keys WHERE scope IN ('plex_ingress_minute', 'plex_ingress_burst') AND expires_at <= ?",
                    (_iso(_now() - timedelta(minutes=2)),),
                )
                for scope, key, limit, expiry in (
                    (
                        "plex_ingress_minute",
                        str(minute),
                        self.per_minute,
                        _iso(_now() + timedelta(minutes=2)),
                    ),
                    (
                        "plex_ingress_burst",
                        str(burst_start),
                        self.burst,
                        _iso(_now() + timedelta(minutes=1)),
                    ),
                ):
                    row = connection.execute(
                        "SELECT COUNT(*) FROM idempotency_keys WHERE scope = ? AND key LIKE ?",
                        (scope, f"{key}:%"),
                    ).fetchone()
                    count = int(row[0] or 0) if row is not None else 0
                    if count >= limit:
                        return False
                nonce = secrets.token_urlsafe(18)
                connection.execute(
                    "INSERT INTO idempotency_keys(scope, key, status, expires_at) VALUES (?, ?, 'accepted', ?)",
                    (
                        "plex_ingress_minute",
                        f"{minute}:{nonce}",
                        _iso(_now() + timedelta(minutes=2)),
                    ),
                )
                connection.execute(
                    "INSERT INTO idempotency_keys(scope, key, status, expires_at) VALUES (?, ?, 'accepted', ?)",
                    (
                        "plex_ingress_burst",
                        f"{burst_start}:{nonce}",
                        _iso(_now() + timedelta(minutes=1)),
                    ),
                )
                return True
        except Exception:
            # A limiter failure must fail closed at the ingress boundary.
            return False

    consume = allow


class DurablePlexInbox:
    """SQLite event inbox adapter for normalized webhook observations."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def persist_event(self, event: object) -> Mapping[str, object]:
        if not isinstance(event, NormalizedPlexEvent):
            raise TypeError("event must be a NormalizedPlexEvent")
        now = _iso()
        with self.database.transaction() as connection:
            previous = connection.execute(
                "SELECT MAX(tombstone_generation), lifecycle_status FROM plex_items WHERE server_uuid=? AND library_uuid=? AND rating_key=?",
                (event.server_uuid, event.library_uuid, event.rating_key),
            ).fetchone()
            generation = 0
            if (
                previous is not None
                and previous[0] is not None
                and str(previous[1]) in {"tombstone", "removed", "quarantined"}
            ):
                generation = int(previous[0]) + 1
            record = event.to_record(generation)
            result = connection.execute(
                """
                INSERT OR IGNORE INTO event_inbox(
                    event_key, source, event_type, server_uuid, library_uuid,
                    rating_key, tombstone_generation, payload_hash,
                    sanitized_payload_json, status, available_at, received_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 'received', ?, ?, ?)
                """,
                (
                    record["event_key"],
                    record["source"],
                    record["event_type"],
                    record["server_uuid"],
                    record["library_uuid"],
                    record["rating_key"],
                    record["payload_hash"],
                    record["sanitized_payload_json"],
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id, status, event_key FROM event_inbox WHERE event_key = ?",
                (record["event_key"],),
            ).fetchone()
        if row is None:
            raise ProductionRuntimeError("event inbox row was not persisted")
        return {
            "id": int(row[0]),
            "status": str(row[1]),
            "event_key": str(row[2]),
            "duplicate": result.rowcount != 1,
        }

    insert = persist_event

    def get(self, row_id: int) -> Mapping[str, object] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM event_inbox WHERE id = ?", (row_id,)
            ).fetchone()
        return None if row is None else dict(row)

    row = get

    def event_from_row(self, row: Mapping[str, object]) -> NormalizedPlexEvent:
        raw = row.get("sanitized_payload_json")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise ProviderQuarantine("inbox payload is invalid") from exc
        if not isinstance(payload, Mapping):
            raise ProviderQuarantine("inbox payload is invalid")
        event_type = payload.get(
            "event", payload.get("event_type", row.get("event_type", "library.new"))
        )
        return NormalizedPlexEvent(
            event_type=cast(Any, event_type),
            server_uuid=str(payload.get("server_uuid", row.get("server_uuid", ""))),
            machine_identifier=cast(str | None, payload.get("machine_identifier")),
            library_uuid=str(payload.get("library_uuid", row.get("library_uuid", ""))),
            library_name=cast(str | None, payload.get("library_name")),
            rating_key=str(payload.get("rating_key", row.get("rating_key", ""))),
            media_type=str(payload.get("media_type", "")),
            title=str(payload.get("title", "Untitled")),
            year=payload.get("year") if isinstance(payload.get("year"), int) else None,
            season_number=payload.get("season_number")
            if isinstance(payload.get("season_number"), int)
            else None,
            episode_number=payload.get("episode_number")
            if isinstance(payload.get("episode_number"), int)
            else None,
            parent_rating_key=cast(str | None, payload.get("parent_rating_key")),
            grandparent_rating_key=cast(
                str | None, payload.get("grandparent_rating_key")
            ),
            guid=cast(str | None, payload.get("guid")),
            parent_guid=cast(str | None, payload.get("parent_guid")),
            grandparent_guid=cast(str | None, payload.get("grandparent_guid")),
            added_at=_parse_time(payload.get("added_at")),
            poster_size=int(payload.get("poster_size", 0) or 0),
            poster_sha256=cast(str | None, payload.get("poster_sha256")),
            source=str(payload.get("source", row.get("source", "plex_webhook"))),
            observed_at=_parse_time(payload.get("observed_at")) or _now(),
        )

    decode = event_from_row


class DurableSnapshotStore(SnapshotStore):
    """A ``SnapshotStore``-compatible SQLite snapshot registry.

    ``SafeViewPaginator`` intentionally accepts the base class.  This class
    overrides every method that reads/writes records, so the inherited
    ``_records`` dictionary is never authoritative and continuation cursors
    survive process restart.
    """

    def __init__(
        self, signer: CursorSigner, database: Database, *, clock: object = time.time
    ) -> None:
        super().__init__(signer, clock=clock)
        self.database = database
        with database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS safe_snapshots(
                    snapshot_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    filter_hash TEXT NOT NULL,
                    issued_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    items_json TEXT NOT NULL,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    as_of TEXT,
                    total_count INTEGER,
                    partial_errors_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )

    def create(self, items: Iterable[Any], **kwargs: object) -> SnapshotRecord:
        user_id = kwargs.get("user_id", kwargs.get("actor_user_id"))
        chat_id = kwargs.get("chat_id", kwargs.get("actor_chat_id"))
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            raise ValueError("actor user_id and chat_id are required")
        tool = kwargs.get("tool")
        if not isinstance(tool, str):
            raise ValueError("snapshot tool is required")
        filter_hash = kwargs.get("filter_hash")
        if filter_hash is None:
            filter_hash = binding_hash(kwargs.get("query"))
        if not isinstance(filter_hash, str):
            raise ValueError("snapshot filter hash is invalid")
        now_value = kwargs.get("now")
        current = int(self.clock() if now_value is None else cast(float, now_value))
        selected = tuple(list(items)[: self.max_items])
        truncated = (
            bool(kwargs.get("truncated", False)) or len(selected) >= self.max_items
        )
        total = kwargs.get("total")
        if not isinstance(total, int):
            total = len(selected)
        if total > len(selected):
            truncated = True
        snapshot_id = self.signer._snapshot_id(None)
        as_of = kwargs.get("as_of")
        as_of_text = (
            as_of.isoformat().replace("+00:00", "Z")
            if isinstance(as_of, datetime)
            else _iso()
        )
        errors: tuple[object, ...] = tuple(
            cast(Iterable[object], kwargs.get("partial_errors", ()))
        )
        record = SnapshotRecord(
            snapshot_id=snapshot_id,
            user_id=user_id,
            chat_id=chat_id,
            tool=tool,
            filter_hash=filter_hash,
            issued_at=current,
            expires_at=current + self.signer.ttl,
            items=selected,
            truncated=truncated,
            as_of=as_of_text,
            total_count=min(5_000, max(total, len(selected))),
            partial_errors=errors,
        )
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM safe_snapshots WHERE expires_at <= ?", (current,)
            )
            connection.execute(
                """
                INSERT INTO safe_snapshots(
                    snapshot_id,user_id,chat_id,tool,filter_hash,issued_at,expires_at,
                    items_json,truncated,as_of,total_count,partial_errors_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.snapshot_id,
                    record.user_id,
                    record.chat_id,
                    record.tool,
                    record.filter_hash,
                    record.issued_at,
                    record.expires_at,
                    _json([self._serialize_item(item) for item in record.items]),
                    1 if record.truncated else 0,
                    as_of_text,
                    record.total_count,
                    _json(
                        [self._serialize_item(item) for item in record.partial_errors]
                    ),
                ),
            )
        return record

    create_snapshot = create

    @staticmethod
    def _serialize_item(item: object) -> object:
        if hasattr(item, "to_dict") and callable(getattr(item, "to_dict")):
            return item.to_dict()
        if isinstance(item, MediaCandidate):
            return {
                "_type": "MediaCandidate",
                "media_type": item.media_type.value,
                "provider_id": item.provider_id,
                "title": item.title,
                "year": item.year,
                "overview": item.overview,
                "candidate_handle": item.candidate_handle,
            }
        if isinstance(item, QueueItem):
            return {
                "_type": "QueueItem",
                "service": item.service.value,
                "title": item.title,
                "state": item.state.value,
                "progress_percent": item.progress_percent,
                "eta_seconds": item.eta_seconds,
                "error": item.error,
                "media_type": item.media_type.value if item.media_type else None,
            }
        if isinstance(item, SafeServiceHealth):
            return {
                "_type": "SafeServiceHealth",
                "service": item.service.value,
                "ok": item.ok,
                "version": item.version,
                "message": item.message,
            }
        if isinstance(item, SafeRequestStatus):
            return {
                "_type": "SafeRequestStatus",
                "title": item.title,
                "media_type": item.media_type.value,
                "status": item.status,
                "year": item.year,
                "progress_percent": item.progress_percent,
                "eta_seconds": item.eta_seconds,
                "quality": item.quality,
            }
        # Preserve the typed marker for records whose safe wire form does not
        # contain an unambiguous discriminator.  Reconstructing these values
        # with a plain mapping would make a continuation fail closed because
        # ``SafePage`` accepts exact normalized record classes only.
        from .safe_views import SafeLibraryItem

        if isinstance(item, SafeLibraryItem):
            return {
                "_type": "SafeLibraryItem",
                "media_type": item.media_type.value,
                "title": item.title,
                "year": item.year,
                "library_name": item.library_name,
                "show_title": item.show_title,
                "season_number": item.season_number,
                "episode_number": item.episode_number,
                "quality": item.quality,
                "added_at": item.added_at.isoformat().replace("+00:00", "Z")
                if item.added_at
                else None,
            }
        if isinstance(item, MediaStatus):
            return {
                "_type": "MediaStatus",
                "media_type": item.identity.media_type.value,
                "provider_id": item.identity.provider_id,
                "available": item.available,
                "title": item.title,
                "year": item.year,
                "quality": item.quality,
                "as_of": item.as_of.isoformat().replace("+00:00", "Z")
                if item.as_of
                else None,
            }
        if isinstance(item, PartialError):
            return {
                "_type": "PartialError",
                "service": item.service.value,
                "code": item.code,
                "message": item.message,
                "retryable": item.retryable,
            }
        # SafeLibraryItem and MediaStatus are reconstructed through their
        # canonical mapping forms.  Unknown values never enter this store.
        return _bounded_json(item)

    @staticmethod
    def _deserialize_item(item: object) -> object:
        if not isinstance(item, Mapping):
            return item
        kind = item.get("_type")
        if kind == "MediaCandidate":
            return sanitize_media_candidate(item)
        if kind == "QueueItem":
            return sanitize_queue_item(item)
        if kind == "SafeServiceHealth":
            return sanitize_service_health(item)
        if kind == "SafeRequestStatus":
            return sanitize_request_status(item)
        if kind == "PartialError":
            return sanitize_partial_error(item)
        if kind == "SafeLibraryItem":
            item = dict(item)
            item.pop("_type", None)
            # A persisted safe library record intentionally does not carry a
            # provider URL marker.  It remains a valid display record when
            # resumed, but links are omitted on continuation pages.
            item.pop("plex_url", None)
            return sanitize_library_item(item)
        if kind == "MediaStatus":
            item = dict(item)
            item.pop("_type", None)
            return sanitize_media_status(item)
        # A mapping produced by SafeLibraryItem.to_dict lacks a type marker.
        if "library_name" in item or "plex_url" in item:
            try:
                return sanitize_library_item(item)
            except Exception:
                pass
        return item

    def _record(self, row: Mapping[str, object]) -> SnapshotRecord:
        try:
            item_values = json.loads(str(row["items_json"]))
            error_values = json.loads(str(row["partial_errors_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProductionRuntimeError("durable snapshot is invalid") from exc
        items = (
            tuple(self._deserialize_item(item) for item in item_values)
            if isinstance(item_values, list)
            else ()
        )
        errors = (
            tuple(self._deserialize_item(item) for item in error_values)
            if isinstance(error_values, list)
            else ()
        )
        return SnapshotRecord(
            snapshot_id=str(row["snapshot_id"]),
            user_id=int(cast(Any, row["user_id"])),
            chat_id=int(cast(Any, row["chat_id"])),
            tool=str(row["tool"]),
            filter_hash=str(row["filter_hash"]),
            issued_at=int(cast(Any, row["issued_at"])),
            expires_at=int(cast(Any, row["expires_at"])),
            items=items,
            truncated=bool(row["truncated"]),
            as_of=row.get("as_of"),
            total_count=int(cast(Any, row["total_count"]))
            if row.get("total_count") is not None
            else None,
            partial_errors=errors,
        )

    def get(self, token: str, **kwargs: object) -> SnapshotRecord:
        claims = self.signer.verify(
            token,
            user_id=cast(
                int | None, kwargs.get("user_id", kwargs.get("actor_user_id"))
            ),
            chat_id=cast(
                int | None, kwargs.get("chat_id", kwargs.get("actor_chat_id"))
            ),
            expected_tool=cast(str | None, kwargs.get("expected_tool")),
            expected_filter_hash=cast(str | None, kwargs.get("expected_filter_hash")),
            query=kwargs.get("query", None),
            expected_page_size=cast(int | None, kwargs.get("expected_page_size")),
            now=cast(float | None, kwargs.get("now")),
        )
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM safe_snapshots WHERE snapshot_id = ?",
                (claims.snapshot_id,),
            ).fetchone()
        if row is None:
            from .cursors import CursorSnapshotNotFound

            raise CursorSnapshotNotFound("cursor snapshot is unavailable")
        record = self._record(dict(row))
        current = int(
            self.clock() if kwargs.get("now") is None else cast(float, kwargs["now"])
        )
        if record.expires_at <= current:
            from .cursors import CursorExpired

            raise CursorExpired("snapshot has expired")
        if (
            record.user_id != claims.user_id
            or record.chat_id != claims.chat_id
            or record.tool != claims.tool
            or record.filter_hash != claims.filter_hash
        ):
            from .cursors import CursorBindingError

            raise CursorBindingError("snapshot binding does not match cursor")
        if claims.offset > len(record.items):
            from .cursors import CursorBindingError

            raise CursorBindingError("cursor offset is outside the snapshot")
        return record

    resolve = get

    def delete(self, snapshot_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM safe_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            )


class ProductionOperations:
    """Typed safe handlers and dashboard operations."""

    def __init__(
        self,
        *,
        database: Database,
        plex: object,
        radarr: object | None,
        sonarr: object | None,
        telegram: object | None,
        policy: object,
        workflow: RequestWorkflow,
        worker: "DurableMediaWorker | None",
        cursor_key: bytes | str,
        upstream: object | None = None,
    ) -> None:
        self.database = database
        self.plex = plex
        self.radarr = radarr
        self.sonarr = sonarr
        self.telegram = telegram
        self.policy = policy
        self.workflow = workflow
        self.worker = worker
        self.upstream = upstream
        signer = CursorSigner(cursor_key)
        self.snapshots = DurableSnapshotStore(signer, database)
        self.paginator = SafeViewPaginator(signer, snapshots=self.snapshots)

    def _page(
        self,
        items: Iterable[object],
        *,
        tool: str,
        arguments: Mapping[str, object],
        claims: object | None,
        normalize: Callable[[Any], Any],
        total: int | None = None,
        partial_errors: Iterable[PartialError | Mapping[str, Any]] = (),
    ) -> SafePage[Any]:
        user_id, chat_id, _username, _update_id, _fingerprint = _actor_values(
            claims, arguments
        )
        raw_limit = arguments.get("limit")
        raw_cursor = arguments.get("cursor")
        limit = raw_limit if isinstance(raw_limit, int) else None
        cursor = raw_cursor if isinstance(raw_cursor, str) else None
        return self.paginator.page(
            items,
            tool=tool,
            user_id=user_id,
            chat_id=chat_id,
            limit=limit,
            cursor=cursor,
            query=arguments.get("query", arguments.get("filter")),
            normalize=normalize,
            total=total,
            partial_errors=partial_errors,
        )

    def search_media(
        self,
        arguments: Mapping[str, object],
        *,
        claims: object | None = None,
        **_: object,
    ) -> SafePage[Any]:
        query = arguments.get("query", arguments.get("q"))
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query.encode("utf-8")) > 512
        ):
            raise ValueError("query must be bounded text")
        kind = arguments.get("media_type", arguments.get("type", "any"))
        if kind not in {"any", "movie", "series"}:
            raise ValueError("media_type is invalid")
        values: list[object] = []
        errors: list[PartialError] = []
        try:
            if kind in {"any", "movie"} and self.radarr is not None:
                result = _call(getattr(self.radarr, "search_movie"), query, limit=100)
                values.extend(
                    getattr(
                        result, "items", result if isinstance(result, Sequence) else ()
                    )
                )
            if kind in {"any", "series"} and self.sonarr is not None:
                result = _call(getattr(self.sonarr, "search_series"), query, limit=100)
                values.extend(
                    getattr(
                        result, "items", result if isinstance(result, Sequence) else ()
                    )
                )
            if not values:
                result = _call(
                    getattr(self.plex, "search"), query, media_type=kind, limit=100
                )
                values.extend(
                    getattr(
                        result, "items", result if isinstance(result, Sequence) else ()
                    )
                )
            # Persist an opaque candidate for every result.  The safe-view
            # serializer intentionally omits provider IDs; request handlers
            # accept only this actor/update/query-bound handle on mutation.
            try:
                user_id, chat_id, username, update_id, fingerprint = _actor_values(
                    claims, arguments
                )
                chat_type, update_type = _actor_provenance(claims)
                actor = RequestActor(
                    user_id,
                    chat_id,
                    username,
                    update_id,
                    chat_type,
                    update_type,
                    fingerprint or None,
                )
                query_text = query.strip()
                issued: list[object] = []
                for candidate in values:
                    if not isinstance(candidate, MediaCandidate):
                        continue
                    provider_text = str(candidate.provider_id)
                    if not provider_text.isascii() or not provider_text.isdigit():
                        continue
                    handle = self.workflow.issue_candidate(
                        actor=actor,
                        media_type=candidate.media_type,
                        provider_id=int(provider_text),
                        title=candidate.title,
                        query=query_text,
                        year=candidate.year,
                    )
                    issued.append(replace(candidate, candidate_handle=handle))
                # A provider result without a stable supported ID cannot be
                # safely requested and is therefore not exposed as an action.
                values = issued
            except Exception:
                # Candidate issuance is part of the request boundary.  A
                # failed candidate write must not turn provider search data
                # into an actionable response.
                values = []
        except Exception as exc:
            errors.append(
                PartialError(
                    ServiceName.PLEX, "search_failed", type(exc).__name__, True
                )
            )
        return self._page(
            values,
            tool="search_media",
            arguments=arguments,
            claims=claims,
            normalize=sanitize_media_candidate,
            total=len(values),
            partial_errors=errors,
        )

    def browse_library(
        self,
        arguments: Mapping[str, object],
        *,
        claims: object | None = None,
        **_: object,
    ) -> SafePage[Any]:
        values: list[object] = []
        libraries = _call(getattr(self.plex, "libraries"))
        selected = (
            arguments.get("library")
            or arguments.get("library_id")
            or arguments.get("library_name")
        )
        for library in libraries if isinstance(libraries, Sequence) else ():
            if selected is not None and str(selected) not in {
                str(_value(library, "key", "uuid", "title", default=""))
            }:
                continue
            iterator = _call(
                getattr(self.plex, "iter_library_items"),
                library,
                media_type=arguments.get("media_type"),
                page_size=100,
            )
            for item in (
                cast(Iterable[object], iterator) if iterator is not None else ()
            ):
                values.append(item)
                if len(values) >= 5_000:
                    break
        return self._page(
            values,
            tool="browse_library",
            arguments=arguments,
            claims=claims,
            normalize=sanitize_library_item,
            total=len(values),
        )

    def request_movie(
        self,
        arguments: Mapping[str, object],
        *,
        claims: object | None = None,
        **_: object,
    ) -> object:
        candidate_handle = arguments.get("candidate_handle")
        if not isinstance(candidate_handle, str) or not candidate_handle.strip():
            raise ValueError("candidate_handle is required")
        actor_user, actor_chat, username, update_id, fingerprint = _actor_values(
            claims, arguments
        )
        chat_type, update_type = _actor_provenance(claims)
        actor = RequestActor(
            actor_user,
            actor_chat,
            username,
            update_id,
            chat_type,
            update_type,
            fingerprint or None,
        )
        return _call(
            getattr(self.workflow, "request_movie"),
            actor=actor,
            idempotency_key=arguments.get("idempotency_key"),
            candidate_handle=candidate_handle,
        )

    def request_series(
        self,
        arguments: Mapping[str, object],
        *,
        claims: object | None = None,
        **_: object,
    ) -> object:
        candidate_handle = arguments.get("candidate_handle")
        if not isinstance(candidate_handle, str) or not candidate_handle.strip():
            raise ValueError("candidate_handle is required")
        raw_seasons = arguments.get("seasons", ())
        if (
            not isinstance(raw_seasons, Sequence)
            or isinstance(raw_seasons, (str, bytes, bytearray))
            or not raw_seasons
            or len(raw_seasons) > 50
        ):
            raise ValueError("seasons must contain 1-50 integers")
        seasons: list[int] = []
        for season in raw_seasons:
            if isinstance(season, bool) or not isinstance(season, int) or season < 0:
                raise ValueError("season is invalid")
            if season not in seasons:
                seasons.append(season)
        actor_user, actor_chat, username, update_id, fingerprint = _actor_values(
            claims, arguments
        )
        chat_type, update_type = _actor_provenance(claims)
        actor = RequestActor(
            actor_user,
            actor_chat,
            username,
            update_id,
            chat_type,
            update_type,
            fingerprint or None,
        )
        return _call(
            getattr(self.workflow, "request_series"),
            seasons=seasons,
            actor=actor,
            idempotency_key=arguments.get("idempotency_key"),
            candidate_handle=candidate_handle,
            anime=bool(arguments.get("anime", False)),
        )

    def request_status(
        self,
        arguments: Mapping[str, object],
        *,
        claims: object | None = None,
        **_: object,
    ) -> SafePage[Any]:
        user_id, chat_id, _username, _update, _fingerprint = _actor_values(
            claims, arguments
        )
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT title, media_type, status, year FROM requests WHERE requested_by_user_id = ? AND requested_by_chat_id = ? ORDER BY id DESC LIMIT 250",
                (user_id, chat_id),
            ).fetchall()
        values = [
            SafeRequestStatus(
                title=str(row[0]),
                media_type=MediaType(str(row[1])),
                status=str(row[2]),
                year=int(row[3]) if row[3] is not None else None,
            )
            for row in rows
        ]
        return self._page(
            values,
            tool="request_status",
            arguments=arguments,
            claims=claims,
            normalize=sanitize_request_status,
            total=len(values),
        )

    def download_status(
        self,
        arguments: Mapping[str, object],
        *,
        claims: object | None = None,
        **_: object,
    ) -> SafePage[Any]:
        service = arguments.get("service", "all")
        if service not in {"all", "radarr", "sonarr"}:
            raise ValueError("service is invalid")
        values: list[object] = []
        errors: list[PartialError] = []
        for name, client, enum_name in (
            ("radarr", self.radarr, ServiceName.RADARR),
            ("sonarr", self.sonarr, ServiceName.SONARR),
        ):
            if service not in {"all", name} or client is None:
                continue
            try:
                result = _call(getattr(client, "queue"), page=1, page_size=250)
                values.extend(
                    result
                    if isinstance(result, Sequence)
                    else getattr(result, "items", ())
                )
            except Exception as exc:
                errors.append(
                    PartialError(
                        enum_name, "queue_unavailable", type(exc).__name__, True
                    )
                )
        return self._page(
            values,
            tool="download_status",
            arguments=arguments,
            claims=claims,
            normalize=sanitize_queue_item,
            total=len(values),
            partial_errors=errors,
        )

    def media_status(
        self,
        arguments: Mapping[str, object],
        *,
        claims: object | None = None,
        **_: object,
    ) -> SafePage[Any]:
        """Return credential-free Radarr and Sonarr connectivity.

        The frozen shared-tool schema intentionally accepts no arguments for
        ``media_status``.  Availability for a selected title is handled by the
        search/request workflow; this endpoint reports only whether the two
        Arr dependencies can answer their bounded system-status calls.
        """

        values: list[object] = []
        for client, service in (
            (self.radarr, ServiceName.RADARR),
            (self.sonarr, ServiceName.SONARR),
        ):
            if client is None:
                values.append(
                    SafeServiceHealth(service, False, message="not configured")
                )
                continue
            try:
                status = _call(getattr(client, "system_status"))
                if not isinstance(status, Mapping):
                    raise ValueError("service status is not typed")
                raw_ok = status.get("is_up", True)
                ok = raw_ok if isinstance(raw_ok, bool) else False
                raw_version = status.get("version")
                version = raw_version if isinstance(raw_version, str) else None
                values.append(
                    SafeServiceHealth(
                        service,
                        ok,
                        version=version,
                        message=None if ok else "unavailable",
                    )
                )
            except Exception:
                values.append(SafeServiceHealth(service, False, message="unavailable"))
        return self._page(
            values,
            tool="media_status",
            arguments=arguments,
            claims=claims,
            normalize=sanitize_service_health,
            total=len(values),
        )

    def repair_blocked_imports(
        self,
        arguments: Mapping[str, object] | None = None,
        *,
        claims: object | None = None,
        **_: object,
    ) -> Mapping[str, object]:
        """Repair only one bounded, provider-confirmed import candidate.

        The operation is intentionally an admin confirmation target, not a
        shared safe handler.  Typed provider adapters may expose the reviewed
        ``repair_blocked_imports`` seam; older adapters can use the pinned
        upstream broker only when that exact operation is explicitly present
        in its registered inventory.  Raw provider responses never cross this
        method's boundary.
        """

        del claims
        args = arguments or {}
        query = args.get("query")
        if query is not None and (
            not isinstance(query, str) or len(query.encode("utf-8")) > 256
        ):
            raise ValueError("query is invalid")
        media_type = args.get("media_type", "any")
        if media_type not in {"movie", "series", "any"}:
            raise ValueError("media_type is invalid")
        providers: list[tuple[str, object]] = []
        if media_type in {"series", "any"} and self.sonarr is not None:
            providers.append(("sonarr", self.sonarr))
        if media_type in {"movie", "any"} and self.radarr is not None:
            providers.append(("radarr", self.radarr))
        items: list[object] = []
        failures: list[PartialError] = []
        for name, provider in providers:
            method = getattr(provider, "repair_blocked_imports", None)
            if not callable(method):
                continue
            try:
                result = _call(
                    method,
                    query=query,
                    media_type=("series" if name == "sonarr" else "movie"),
                )
                if isinstance(result, Mapping):
                    provider_items = result.get("items", ())
                    if isinstance(provider_items, Sequence) and not isinstance(
                        provider_items, (str, bytes, bytearray)
                    ):
                        items.extend(
                            _bounded_json(item) for item in provider_items[:100]
                        )
                    elif (
                        result.get("status") is not None
                        or result.get("message") is not None
                    ):
                        items.append(_bounded_json(result))
                elif isinstance(result, Sequence) and not isinstance(
                    result, (str, bytes, bytearray)
                ):
                    items.extend(_bounded_json(item) for item in result[:100])
            except Exception as exc:
                failures.append(
                    PartialError(
                        ServiceName.SONARR if name == "sonarr" else ServiceName.RADARR,
                        "repair_unavailable",
                        type(exc).__name__,
                        True,
                    )
                )
        if not providers or not items and not failures:
            # The existing upstream broker is a bounded compatibility seam;
            # it is used only if it advertises this reviewed companion action.
            registered = getattr(
                self.upstream,
                "registered_tools",
                getattr(self.upstream, "tool_names", ()),
            )
            if self.upstream is not None and "repair_blocked_imports" in set(
                registered or ()
            ):
                try:
                    result = _call(
                        getattr(self.upstream, "call_tool"),
                        "repair_blocked_imports",
                        {"query": query, "media_type": media_type},
                    )
                    if isinstance(result, Mapping):
                        items.extend(
                            cast(
                                Iterable[object], _bounded_json(result.get("items", ()))
                            )
                        )
                except Exception as exc:
                    failures.append(
                        PartialError(
                            ServiceName.UPSTREAM,
                            "repair_unavailable",
                            type(exc).__name__,
                            True,
                        )
                    )
        return {
            "ok": not failures,
            "status": "repaired"
            if items and not failures
            else "unavailable"
            if failures
            else "no_matches",
            "items": items[:100],
            "count": len(items[:100]),
            "partial_errors": tuple(failures),
        }

    # Dashboard read operations are deliberately small typed mappings.  They
    # are consumed by the dashboard API, never exposed through shared tools.
    def health(self, **_: object) -> Mapping[str, object]:
        return {
            "ready": bool(self.worker and self.worker.ready()),
            "worker": self.worker.status() if self.worker else {"ready": False},
        }

    def users(self, **_: object) -> Mapping[str, object]:
        result = (
            _call(getattr(self.policy, "current_users"))
            if callable(getattr(self.policy, "current_users", None))
            else {"users": []}
        )
        return (
            cast(Mapping[str, object], _bounded_json(result))
            if isinstance(result, Mapping)
            else {"users": []}
        )

    def users_resolve(
        self, arguments: Mapping[str, object] | None = None, **_: object
    ) -> Mapping[str, object]:
        user_id = _positive_id((arguments or {}).get("user_id"), field="user_id")
        result = _call(getattr(self.policy, "resolve_identity"), user_id=user_id)
        return (
            cast(Mapping[str, object], _bounded_json(result))
            if isinstance(result, Mapping)
            else {"user_id": user_id}
        )

    def blocked(
        self, arguments: Mapping[str, object] | None = None, **_: object
    ) -> Mapping[str, object]:
        result = _call(
            getattr(self.policy, "blocked_contacts"),
            limit=min(int(cast(Any, (arguments or {}).get("limit", 50))), 50),
        )
        return {
            "items": [_bounded_json(item) for item in cast(Iterable[object], result)]
        }

    def subscriptions(self, **_: object) -> Mapping[str, object]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id,user_id,chat_id,media_type,provider_id,season_number,status,generation FROM subscriptions ORDER BY id DESC LIMIT 250"
            ).fetchall()
        return {"items": [_bounded_json(dict(row)) for row in rows]}

    def deliveries(self, **_: object) -> Mapping[str, object]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id,destination,chat_id,status,attempts,possible_duplicate,unknown_at,error_class FROM deliveries ORDER BY id DESC LIMIT 250"
            ).fetchall()
        return {"items": [_bounded_json(dict(row)) for row in rows]}

    def quarantine(self, **_: object) -> Mapping[str, object]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id,record_type,reason,status,created_at FROM quarantined_records ORDER BY id DESC LIMIT 250"
            ).fetchall()
        return {"items": [_bounded_json(dict(row)) for row in rows]}

    def oracle(self, **_: object) -> Mapping[str, object]:
        return (
            self.worker.oracle() if self.worker else {"ready": False, "residual": None}
        )

    def audit(self, **_: object) -> Mapping[str, object]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id,action,outcome,actor_user_id,actor_chat_id,created_at FROM audit_events ORDER BY id DESC LIMIT 250"
            ).fetchall()
        return {"items": [_bounded_json(dict(row)) for row in rows]}

    def users_add(
        self, arguments: Mapping[str, object] | None = None, **_: object
    ) -> Mapping[str, object]:
        args = arguments or {}
        fingerprint = args.get("expected_fingerprint", args.get("fingerprint"))
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("fingerprint is required")
        result = _call(
            getattr(self.policy, "add_user"),
            user_id=_positive_id(args.get("user_id"), field="user_id"),
            expected_fingerprint=fingerprint,
        )
        return (
            cast(Mapping[str, object], _bounded_json(result))
            if isinstance(result, Mapping)
            else {"ok": True}
        )

    def users_remove(
        self, arguments: Mapping[str, object] | None = None, **_: object
    ) -> Mapping[str, object]:
        args = arguments or {}
        fingerprint = args.get("expected_fingerprint", args.get("fingerprint"))
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("fingerprint is required")
        result = _call(
            getattr(self.policy, "remove_user"),
            user_id=_positive_id(args.get("user_id"), field="user_id"),
            expected_fingerprint=fingerprint,
        )
        return (
            cast(Mapping[str, object], _bounded_json(result))
            if isinstance(result, Mapping)
            else {"ok": True}
        )

    def delivery_retry_once(
        self, arguments: Mapping[str, object] | None = None, **_: object
    ) -> Mapping[str, object]:
        return (
            self.worker.recover_delivery("retry", arguments or {})
            if self.worker
            else {"ok": False}
        )

    def delivery_mark_abandoned(
        self, arguments: Mapping[str, object] | None = None, **_: object
    ) -> Mapping[str, object]:
        return (
            self.worker.recover_delivery("abandon", arguments or {})
            if self.worker
            else {"ok": False}
        )

    def delivery_assume_sent(
        self, arguments: Mapping[str, object] | None = None, **_: object
    ) -> Mapping[str, object]:
        return (
            self.worker.recover_delivery("assume_sent", arguments or {})
            if self.worker
            else {"ok": False}
        )

    def delivery_resend_once(
        self, arguments: Mapping[str, object] | None = None, **_: object
    ) -> Mapping[str, object]:
        return (
            self.worker.recover_delivery("resend", arguments or {})
            if self.worker
            else {"ok": False}
        )


class SQLiteConfirmationArgumentsStore:
    """Durable exact-argument binding used by the callback executor.

    The auth capability table deliberately keeps only an argument digest.  A
    separate SQLite row holds the bounded canonical arguments until the
    trusted callback atomically consumes them.  Plaintext capability tokens
    are never stored here.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS production_confirmation_arguments (
                    token_hash TEXT PRIMARY KEY,
                    tool TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    argument_hash TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT
                )
                """
            )

    def put(
        self,
        *,
        token_hash: str,
        tool: str,
        argument_hash: str,
        arguments: Mapping[str, object],
        expires_at: int,
    ) -> bool:
        if not isinstance(token_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", token_hash
        ):
            raise ProductionRuntimeError("confirmation argument token hash is invalid")
        if not isinstance(tool, str) or not tool or len(tool) > 128:
            raise ProductionRuntimeError("confirmation argument tool is invalid")
        if not isinstance(argument_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", argument_hash
        ):
            raise ProductionRuntimeError("confirmation argument digest is invalid")
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at <= int(time.time())
        ):
            raise ProductionRuntimeError("confirmation argument expiry is invalid")
        encoded = _json(arguments)
        if canonical_argument_hash(arguments) != argument_hash:
            raise ProductionRuntimeError("confirmation argument digest does not match")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT tool,arguments_json,argument_hash,consumed_at FROM production_confirmation_arguments WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is not None:
                if row[3] is not None or (str(row[0]), str(row[1]), str(row[2])) != (
                    tool,
                    encoded,
                    argument_hash,
                ):
                    raise ProductionRuntimeError(
                        "confirmation argument binding conflicts"
                    )
                return True
            connection.execute(
                "INSERT INTO production_confirmation_arguments(token_hash,tool,arguments_json,argument_hash,expires_at,created_at,consumed_at) VALUES(?,?,?,?,?,?,NULL)",
                (token_hash, tool, encoded, argument_hash, expires_at, _iso()),
            )
        return True

    def consume(
        self,
        *,
        token_hash: str,
        tool: str,
        argument_hash: str,
        record: object | None = None,
    ) -> Mapping[str, object] | None:
        del record
        if not isinstance(token_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", token_hash
        ):
            return None
        now_epoch = int(time.time())
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT tool,arguments_json,argument_hash,expires_at,consumed_at FROM production_confirmation_arguments WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is None or row[4] is not None or int(row[3]) <= now_epoch:
                return None
            if str(row[0]) != tool or str(row[2]) != argument_hash:
                return None
            changed = connection.execute(
                "UPDATE production_confirmation_arguments SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL AND expires_at>?",
                (_iso(), token_hash, now_epoch),
            ).rowcount
            if changed != 1:
                return None
            try:
                parsed = json.loads(str(row[1]))
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if not isinstance(parsed, Mapping):
                return None
            return {
                "tool": str(row[0]),
                "argument_hash": str(row[2]),
                "arguments": dict(parsed),
            }

    def lookup(
        self, *, token_hash: str, tool: str, argument_hash: str
    ) -> Mapping[str, object] | None:
        """Read the exact row for the executor after callback consumption.

        This is intentionally not exposed through the application boundary;
        it only lets the already-authorized executor use the durable bytes
        that were atomically consumed by ``consume``.
        """

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT tool,arguments_json,argument_hash,expires_at FROM production_confirmation_arguments WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
        if row is None or str(row[0]) != tool or str(row[2]) != argument_hash:
            return None
        if int(row[3]) <= int(time.time()):
            return None
        try:
            parsed = json.loads(str(row[1]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return (
            cast(Mapping[str, object], parsed) if isinstance(parsed, Mapping) else None
        )


class ProductionConfirmationBridge:
    """Leave preview delivery and binding to the trusted Hermes extension.

    Hermes owns the native Telegram bot and the callback-data lifecycle.  The
    companion must therefore never send a second preview or bind a message on
    its own.  ``arguments`` is accepted as an optional compatibility field so
    the application can persist the exact callback target through the paired
    executor without placing it in a model-visible response.
    """

    def __init__(self, executor: "ProductionConfirmationExecutor") -> None:
        self.executor = executor

    def __call__(self, **kwargs: object) -> object:
        token = kwargs.get("token") or kwargs.get("capability")
        preview = kwargs.get("preview")
        if not isinstance(token, str) or not isinstance(preview, str):
            raise ProductionRuntimeError("confirmation bridge arguments are incomplete")
        arguments = kwargs.get("arguments")
        if isinstance(arguments, Mapping):
            self.executor.record_arguments(
                token,
                arguments,
                tool=str(kwargs.get("tool", "unknown")),
                expires_at=kwargs.get("expires_at"),
            )
        # The plaintext token is deliberately not returned.  The surrounding
        # Hermes adapter receives the private confirmation envelope and owns
        # exact preview delivery plus confirmation_bind.
        return {
            "ok": True,
            "delegated": "hermes",
            "token_hash": hashlib.sha256(token.encode("ascii", "strict")).hexdigest(),
        }


class ProductionConfirmationExecutor:
    """Execute only exact arguments retained in the durable ledger."""

    def __init__(
        self,
        operations: ProductionOperations,
        database: Database | None = None,
        arguments_store: SQLiteConfirmationArgumentsStore | None = None,
        resolver: Callable[[ConfirmationRecord], Mapping[str, object] | None]
        | None = None,
    ) -> None:
        self.operations = operations
        self.database = database
        self.arguments_store = arguments_store
        self.resolver = resolver

    def record_arguments(
        self,
        token_or_hash: str,
        arguments: Mapping[str, object],
        *,
        tool: str = "unknown",
        expires_at: object | None = None,
    ) -> None:
        """Persist the exact, bounded arguments for one confirmation token.

        Callers may pass either the one-time plaintext token (immediately
        after ``create``) or its SHA-256 hash.  Only the hash and canonical
        JSON are retained.  The explicit method lets ``app.py`` record the
        original request without weakening the hash-only auth capability row.
        """

        if not isinstance(token_or_hash, str) or not token_or_hash:
            raise ProductionRuntimeError("confirmation token is unavailable")
        if re.fullmatch(r"[0-9a-f]{64}", token_or_hash):
            token_hash = token_or_hash
        else:
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token_or_hash):
                raise ProductionRuntimeError("confirmation token is invalid")
            token_hash = hashlib.sha256(token_or_hash.encode("ascii")).hexdigest()
        if not isinstance(arguments, Mapping):
            raise ProductionRuntimeError("confirmation arguments are invalid")
        encoded = _json(arguments)
        expiry: object = expires_at
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            expiry = int(time.time()) + 300
        expiry_int = int(expiry)
        if expiry_int <= int(time.time()):
            raise ProductionRuntimeError("confirmation arguments have expired")
        if self.database is None:
            raise ProductionRuntimeError("confirmation argument ledger is unavailable")
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO production_confirmation_arguments(
                    token_hash, tool, arguments_json, argument_hash, expires_at,
                    created_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(token_hash) DO UPDATE SET
                    tool=excluded.tool,
                    arguments_json=excluded.arguments_json,
                    argument_hash=excluded.argument_hash,
                    expires_at=excluded.expires_at
                """,
                (
                    token_hash,
                    tool,
                    encoded,
                    canonical_argument_hash(arguments),
                    expiry_int,
                    _iso(),
                ),
            )

    def _durable_arguments(
        self, record: ConfirmationRecord
    ) -> Mapping[str, object] | None:
        if self.database is None:
            return None
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT arguments_json, argument_hash, expires_at FROM production_confirmation_arguments WHERE token_hash=?",
                (record.token_hash,),
            ).fetchone()
        if row is None or int(row[2]) <= int(time.time()):
            return None
        if str(row[1]) != record.argument_hash:
            return None
        try:
            parsed = json.loads(str(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return (
            cast(Mapping[str, object], parsed) if isinstance(parsed, Mapping) else None
        )

    def __call__(self, record: ConfirmationRecord, **kwargs: object) -> object:
        arguments = (
            self.arguments_store.lookup(
                token_hash=record.token_hash,
                tool=record.tool,
                argument_hash=record.argument_hash,
            )
            if self.arguments_store is not None
            else None
        )
        if arguments is None and self.resolver is not None:
            arguments = self.resolver(record)
        supplied = kwargs.get("arguments")
        if (
            arguments is None
            and isinstance(supplied, Mapping)
            and self.arguments_store is None
        ):
            arguments = cast(Mapping[str, object], supplied)
        if arguments is None:
            arguments = self._durable_arguments(record)
        if arguments is None:
            # Never guess a destructive target from a preview or model text.
            return {
                "ok": False,
                "status": "consumed",
                "reason": "confirmation_arguments_unavailable",
                "tool": record.tool,
            }
        handler = getattr(self.operations, record.tool, None)
        if not callable(handler):
            raise ProductionRuntimeError("confirmed tool is not executable")
        if canonical_argument_hash(arguments) != record.argument_hash:
            raise ProductionRuntimeError("confirmed arguments do not match capability")
        result = _call(
            handler, arguments, claims=kwargs.get("claims"), policy=kwargs.get("policy")
        )
        if self.database is not None:
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE production_confirmation_arguments SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL",
                    (_iso(), record.token_hash),
                )
        return result


class HermesTelegramBridge:
    """Narrow notification seam when Hermes owns the canonical bot token.

    The companion never reads Hermes' broad ``.env``.  The pinned Hermes
    extension exposes one authenticated, typed ``send_notification`` method
    on the policy-helper client.  This adapter keeps that private method shape
    small and presents the same ``send_message`` result/error contract used by
    :class:`TelegramClient`.
    """

    def __init__(self, helper: object) -> None:
        self.helper = helper

    def send_message(
        self, chat_id: int, text: str, *, parse_mode: str = "HTML", **kwargs: object
    ) -> object:
        del kwargs
        method = getattr(self.helper, "send_notification", None)
        if not callable(method):
            raise ProductionRuntimeError(
                "Hermes notification helper route is unavailable"
            )
        result = _call(method, chat_id=chat_id, text=text, parse_mode=parse_mode)
        status = _value(result, "status")
        if status == "sent":
            return result
        if status in {"retryable-pretransmission", "retryable"}:
            retry_after = _value(result, "retry_after")
            bounded_retry_after = (
                retry_after
                if isinstance(retry_after, int) and retry_after >= 0
                else None
            )
            raise TelegramError(
                TelegramErrorClass.RETRYABLE,
                "Hermes notification was not transmitted",
                retry_after=bounded_retry_after,
                transmitted=False,
            )
        if status == "ambiguous":
            raise TelegramError(
                TelegramErrorClass.AMBIGUOUS,
                "Hermes notification delivery is ambiguous",
                transmitted=True,
            )
        if status == "permanent":
            raise TelegramError(
                TelegramErrorClass.APPLICATION,
                "Hermes notification delivery is permanently unavailable",
                transmitted=bool(_value(result, "transmitted")),
            )
        raise ProductionRuntimeError("Hermes notification response is invalid")


class DurableMediaWorker:
    """One restart-safe leader-fenced Plex reconciliation and delivery worker."""

    def __init__(
        self,
        *,
        database: Database,
        plex: object,
        radarr: object | None = None,
        sonarr: object | None = None,
        telegram: object | None = None,
        policy: object | None = None,
        workflow: RequestWorkflow | None = None,
        inbox: DurablePlexInbox | None = None,
        worker_id: str | None = None,
        activation_id: str = "media-companion",
        leader_lease_seconds: int = LEADER_LEASE_SECONDS,
        incremental_interval: int = INCREMENTAL_SECONDS,
        full_interval: int = FULL_RECONCILIATION_SECONDS,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.database = database
        self.plex = plex
        self.radarr = radarr
        self.sonarr = sonarr
        self.telegram = telegram
        self.policy = policy
        self.workflow = workflow
        self.inbox = inbox or DurablePlexInbox(database)
        self.worker_id = worker_id or f"companion-{secrets.token_urlsafe(10)}"
        self.activation_id = activation_id
        self.leader_lease_seconds = leader_lease_seconds
        self.incremental_interval = incremental_interval
        self.full_interval = full_interval
        self.clock = clock
        self.leader: LeaderLease | None = None
        self._task: asyncio.Task[object] | None = None
        self._stop = asyncio.Event()
        self._ready = False
        self._last_incremental: datetime | None = None
        self._last_full: datetime | None = None
        self._last_error: str | None = None
        self._full_complete = False
        self._cycle_lock = False
        # Set only while a bounded full sweep is traversing Plex.  This keeps
        # webhook processing between the two activation passes durable (the
        # timestamp rule below still classifies imports made in that window).
        self._active_sweep_phase: str | None = None
        self._ensure_activation()

    def _ensure_activation(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO activation(activation_id,status,delivery_enabled,version) VALUES (?, 'pending', 0, 0)",
                (self.activation_id,),
            )
            # The canonical activation table intentionally keeps the public
            # lifecycle small.  These two private ledger tables retain the
            # independently traversed identity sets needed to make a restart
            # safe pass-one/pass-two activation decision.  They are created
            # here (rather than in an in-memory cache) so an upgraded process
            # can resume an interrupted bounded sweep.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS production_activation_sweeps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activation_id TEXT NOT NULL REFERENCES activation(activation_id) ON DELETE CASCADE,
                    phase TEXT NOT NULL CHECK (phase IN ('baseline', 'pass2')),
                    server_uuid TEXT NOT NULL,
                    library_uuid TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
                    last_rating_key TEXT,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    UNIQUE(activation_id, phase, server_uuid, library_uuid, generation)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS production_activation_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sweep_id INTEGER NOT NULL REFERENCES production_activation_sweeps(id) ON DELETE CASCADE,
                    logical_key TEXT NOT NULL,
                    rating_key TEXT NOT NULL,
                    added_at TEXT,
                    UNIQUE(sweep_id, logical_key)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_production_activation_sweeps_phase ON production_activation_sweeps(activation_id, phase, status, library_uuid)"
            )

    def start(self) -> asyncio.Task[object]:
        if self._task is not None and not self._task.done():
            return self._task
        self.leader = self.database.acquire_leader(
            self.worker_id, lease_name="media", lease_seconds=self.leader_lease_seconds
        )
        if self.leader is None:
            raise WorkerNotLeader("another worker currently owns the media lease")
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self.run(), name="media-companion-worker")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None and not task.done() and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        self._task = None
        if self.leader is not None:
            self.database.release_leader(self.leader)
            self.leader = None
        self._ready = False

    shutdown = stop
    close = stop

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except (
                Exception
            ) as exc:  # pragma: no cover - exercised by service integration
                self._last_error = type(exc).__name__
                self._ready = False
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=min(self.incremental_interval, 30)
                )
            except asyncio.TimeoutError:
                continue

    def _assert_leader(self) -> LeaderLease:
        lease = self.leader
        if lease is None:
            raise WorkerNotLeader("worker has no leader lease")
        renewed = self.database.renew_leader(
            lease, lease_seconds=self.leader_lease_seconds
        )
        if renewed is None:
            self.leader = None
            self._ready = False
            raise WorkerNotLeader("leader lease was fenced")
        self.leader = renewed
        return renewed

    def run_cycle(self, *, force_full: bool = False) -> Mapping[str, int]:
        if self.leader is None:
            self.leader = self.database.acquire_leader(
                self.worker_id,
                lease_name="media",
                lease_seconds=self.leader_lease_seconds,
            )
        lease = self._assert_leader()
        self.database.observe_clock(self.clock())
        processed = self.drain_inbox(lease)
        now = self.clock()
        full_due = (
            force_full
            or self._last_full is None
            or (now - self._last_full).total_seconds() >= self.full_interval
        )
        if full_due:
            swept = self.run_full_reconciliation(lease)
            if self._full_complete:
                self._last_full = now
        else:
            swept = 0
        self.run_incremental_reconciliation(lease)
        planned = self.plan_notifications()
        delivered = self.deliver_pending(lease)
        self.cleanup()
        self._last_incremental = now
        # A full sweep is the readiness linearization point.  Pending
        # activation remains delivery-disabled until the sweep has recorded a
        # complete identity set.
        self._ready = self._durable_ready()
        return {
            "events": processed,
            "swept": swept,
            "planned": planned,
            "delivered": delivered,
        }

    def drain_inbox(
        self, lease: LeaderLease | None = None, *, limit: int = MAX_INBOX_BATCH
    ) -> int:
        active = lease or self._assert_leader()
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM event_inbox WHERE status IN ('received','queued','retry_wait','ready') AND available_at <= ? ORDER BY id LIMIT ?",
                (_iso(), limit),
            ).fetchall()
        count = 0
        for row in rows:
            claim = self.database.claim_event(
                int(row[0]),
                lease_seconds=CLAIM_LEASE_SECONDS,
                worker_id=self.worker_id,
                leader_epoch=active.epoch,
            )
            if claim is None:
                continue
            try:
                inbox_row = self.inbox.get(int(row[0]))
                if inbox_row is None:
                    raise ProviderQuarantine("inbox row disappeared")
                event = self.inbox.event_from_row(inbox_row)
                self.process_event(event, event_row=inbox_row, claim=claim)
                self.database.complete_claim(
                    "event_inbox", int(row[0]), claim, status="handled"
                )
                count += 1
            except ProviderRetryable as exc:
                self.database.release_claim(
                    "event_inbox",
                    int(row[0]),
                    claim,
                    status="retry_wait",
                    error=cast(str, exc),
                    retry_at=_now() + timedelta(minutes=1),
                )
            except Exception as exc:
                self._quarantine("event_inbox", int(row[0]), type(exc).__name__)
                self.database.complete_claim(
                    "event_inbox",
                    int(row[0]),
                    claim,
                    status="quarantined",
                    updates={"error_class": type(exc).__name__},
                )
        return count

    def _quarantine(self, record_type: str, record_id: int | str, reason: str) -> None:
        now = _iso()
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO quarantined_records(
                        source, source_name, source_table, source_id, source_row_id,
                        record_type, reason_code, reason, detail_json, payload_json,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                    """,
                    (
                        "media_companion",
                        "production_worker",
                        record_type,
                        str(record_id),
                        str(record_id),
                        record_type,
                        reason[:128],
                        reason[:512],
                        _json({"worker_id": self.worker_id}),
                        _json(
                            {"record_type": record_type, "record_id": str(record_id)}
                        ),
                        now,
                        now,
                    ),
                )
        except Exception as exc:
            # A quarantine write is itself a safety boundary.  Preserve a
            # durable audit row if a deployment has an older quarantine
            # schema, and surface the failure to the caller rather than
            # silently converting an inconsistent provider record to handled.
            try:
                with self.database.transaction() as connection:
                    connection.execute(
                        "INSERT INTO audit_events(action,outcome,actor_user_id,actor_chat_id,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            "production.quarantine_failed",
                            type(exc).__name__,
                            None,
                            None,
                            _json(
                                {
                                    "record_type": record_type,
                                    "record_id": str(record_id),
                                    "reason": reason[:256],
                                }
                            ),
                            now,
                        ),
                    )
            except Exception:
                # There is no safe in-process fallback for a missing ledger;
                # retain the original exception context so the worker remains
                # unready and retries rather than acknowledging the row.
                raise ProductionRuntimeError("durable quarantine failed") from exc
            raise ProductionRuntimeError("durable quarantine failed") from exc

    def process_event(
        self,
        event: NormalizedPlexEvent,
        *,
        event_row: Mapping[str, object] | None = None,
        claim: ClaimToken | None = None,
    ) -> int:
        if event.media_type not in {"movie", "episode"}:
            return 0
        if self._is_library_allowed(event) is False:
            return 0
        try:
            metadata = _call(getattr(self.plex, "get_metadata"), event.rating_key)
        except Exception as exc:
            raise ProviderRetryable("Plex metadata fetch failed") from exc
        item, playable = self._verified_item(metadata, event)
        if not playable:
            raise ProviderRetryable("Plex metadata has no playable file evidence")
        item_id = self.persist_verified_item(item, event=event)
        if self._activation_status() != "active":
            # During activation every observed identity is recorded in the
            # durable pass ledger.  A strict added_at comparison protects
            # against historical rows being replayed as new; an import made
            # after the baseline boundary is retained as a pass-two/new
            # member even when its webhook is drained before the second full
            # sweep starts.
            self._record_activation_member(item_id, item, event)
            return item_id
        self.associate_subscriptions(item_id, item)
        return item_id

    def _activation_status(self) -> str:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT status FROM activation WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
        return "pending" if row is None else str(row[0])

    def _record_activation_member(
        self,
        item_id: int,
        item: PlexItem,
        event: NormalizedPlexEvent,
        *,
        classification: str | None = None,
    ) -> None:
        del item_id
        logical_key = f"{event.server_uuid}:{event.library_uuid}:{item.rating_key}:0"
        pass_number = 1
        if classification is None:
            with self.database.connection() as connection:
                activation = connection.execute(
                    "SELECT status,baseline_started_at FROM activation WHERE activation_id=?",
                    (self.activation_id,),
                ).fetchone()
            baseline = (
                _parse_time(activation[1])
                if activation is not None and activation[1]
                else None
            )
            added = item.added_at
            is_new = (
                activation is not None
                and str(activation[0]) == "baseline"
                and baseline is not None
                and added is not None
                and added > baseline
                and self._active_sweep_phase in {"pass2", None}
            )
            pass_number = 2 if is_new else 1
            classification = "new" if is_new else "historical"
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO activation_members(activation_id,logical_key,server_uuid,library_uuid,rating_key,tombstone_generation,pass_number,added_at,classification) VALUES(?,?,?,?,?,?,?, ?, ?)",
                (
                    self.activation_id,
                    logical_key,
                    event.server_uuid,
                    event.library_uuid,
                    item.rating_key,
                    0,
                    pass_number,
                    _iso(item.added_at) if item.added_at else None,
                    classification,
                ),
            )

    def _is_library_allowed(self, event: NormalizedPlexEvent) -> bool:
        configured_ids = getattr(self.plex, "allowed_library_keys", ()) or ()
        configured_names = getattr(self.plex, "allowed_library_names", ()) or ()
        if not configured_ids and not configured_names:
            return True
        return event.library_uuid in {str(value) for value in configured_ids} or (
            event.library_name is not None
            and event.library_name in {str(value) for value in configured_names}
        )

    def _verified_item(
        self, metadata: object, event: NormalizedPlexEvent
    ) -> tuple[PlexItem, bool]:
        item = _value(metadata, "item")
        if not isinstance(item, PlexItem):
            raise ProviderQuarantine("Plex metadata was not typed")
        if (
            item.rating_key != event.rating_key
            or item.media_type.value != event.media_type
        ):
            raise ProviderQuarantine("Plex metadata identity mismatch")
        playable = bool(_value(metadata, "snapshot_verified", default=False)) and bool(
            _value(metadata, "playable", default=False)
        )
        snapshot = _value(metadata, "snapshot")
        if snapshot is not None:
            playable = playable and bool(_value(snapshot, "playable", default=False))
        if item.provider_identity is None:
            provider_identity = _value(metadata, "provider_identity")
            if isinstance(provider_identity, MediaIdentity):
                object.__setattr__(item, "provider_identity", provider_identity)
        return item, playable

    def _next_generation(
        self, connection: Any, event: NormalizedPlexEvent, item: PlexItem
    ) -> int:
        row = connection.execute(
            "SELECT tombstone_generation, lifecycle_status, added_at FROM plex_items WHERE server_uuid = ? AND library_uuid = ? AND rating_key = ? ORDER BY tombstone_generation DESC LIMIT 1",
            (event.server_uuid, event.library_uuid, event.rating_key),
        ).fetchone()
        if row is None:
            return 0
        generation = int(row[0])
        if str(row[1]) in {"tombstone", "removed", "quarantined"}:
            previous_added = _parse_time(row[2])
            if item.added_at is None or (
                previous_added is not None and item.added_at <= previous_added
            ):
                raise ProviderQuarantine("tombstone generation has no newer added_at")
            return generation + 1
        return generation

    def persist_verified_item(
        self, item: PlexItem, *, event: NormalizedPlexEvent
    ) -> int:
        if item.provider_identity is None:
            raise ProviderQuarantine("Plex metadata has no verified provider identity")
        now = _iso()
        generation: int
        with self.database.transaction() as connection:
            generation = self._next_generation(connection, event, item)
            identity = item.provider_identity
            provider_json = _json(
                {
                    "provider_id": identity.provider_id,
                    "tmdb_id": identity.tmdb_id,
                    "tvdb_id": identity.tvdb_id,
                    "imdb_id": identity.imdb_id,
                }
            )
            connection.execute(
                """
                INSERT INTO plex_items(
                    server_uuid,library_uuid,machine_identifier,rating_key,
                    tombstone_generation,media_type,title,year,show_title,
                    season_number,episode_number,library_key,library_name,
                    tmdb_id,tvdb_id,imdb_id,provider_guid_json,quality,plex_url,
                    poster_hash,added_at,visible_in_plex_at,fingerprint,
                    lifecycle_status,payload_json,version,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(server_uuid,library_uuid,rating_key,tombstone_generation)
                DO UPDATE SET title=excluded.title, year=excluded.year,
                    show_title=excluded.show_title, season_number=excluded.season_number,
                    episode_number=excluded.episode_number, tmdb_id=excluded.tmdb_id,
                    tvdb_id=excluded.tvdb_id, imdb_id=excluded.imdb_id,
                    provider_guid_json=excluded.provider_guid_json, quality=excluded.quality,
                    plex_url=excluded.plex_url, added_at=COALESCE(plex_items.added_at,excluded.added_at),
                    visible_in_plex_at=COALESCE(plex_items.visible_in_plex_at,excluded.visible_in_plex_at),
                    lifecycle_status='active', payload_json=excluded.payload_json,
                    version=plex_items.version+1, updated_at=excluded.updated_at
                """,
                (
                    event.server_uuid,
                    event.library_uuid,
                    item.machine_identifier,
                    item.rating_key,
                    generation,
                    item.media_type.value,
                    item.title,
                    item.year,
                    item.show_title,
                    item.season_number,
                    item.episode_number,
                    item.library_key,
                    item.library_name or event.library_name,
                    identity.tmdb_id,
                    identity.tvdb_id,
                    identity.imdb_id,
                    provider_json,
                    item.quality,
                    item.plex_url,
                    event.poster_sha256,
                    _iso(item.added_at)
                    if item.added_at
                    else (_iso(event.added_at) if event.added_at else None),
                    now,
                    hashlib.sha256(provider_json.encode()).hexdigest(),
                    "active",
                    _json(
                        {
                            "source": event.source,
                            "event_key": event.event_key(generation),
                        }
                    ),
                    0,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM plex_items WHERE server_uuid=? AND library_uuid=? AND rating_key=? AND tombstone_generation=?",
                (event.server_uuid, event.library_uuid, item.rating_key, generation),
            ).fetchone()
            if row is None:
                raise ProductionRuntimeError("verified Plex item was not persisted")
            item_id = int(row[0])
            for provider, provider_id in (
                ("tmdb", identity.tmdb_id),
                ("tvdb", identity.tvdb_id),
                ("imdb", identity.imdb_id),
            ):
                if provider_id is None:
                    continue
                connection.execute(
                    "INSERT INTO plex_crosswalks(plex_item_id,provider,provider_id,verified,evidence_json) VALUES (?,?,?,?,?) ON CONFLICT(plex_item_id,provider,provider_id) DO UPDATE SET verified=1, evidence_json=excluded.evidence_json, updated_at=excluded.updated_at",
                    (
                        item_id,
                        provider,
                        str(provider_id),
                        1,
                        _json({"source": event.source, "playable": True}),
                    ),
                )
        return item_id

    def associate_subscriptions(self, plex_item_id: int, item: PlexItem) -> int:
        identity = item.provider_identity
        if identity is None:
            return 0
        matched = 0
        with self.database.transaction() as connection:
            if item.media_type is MediaType.MOVIE:
                rows = connection.execute(
                    "SELECT s.id,s.request_id,u.id,u.version FROM subscriptions s JOIN subscription_units u ON u.subscription_id=s.id WHERE s.status IN ('active','fulfilled') AND s.media_type='movie' AND s.provider_id = ? AND u.unit_type='movie'",
                    (str(identity.tmdb_id or identity.provider_id),),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT s.id,s.request_id,u.id,u.version FROM subscriptions s JOIN subscription_units u ON u.subscription_id=s.id WHERE s.status IN ('active','fulfilled') AND s.media_type='series' AND s.provider_id = ? AND u.season_number = ? AND u.episode_number = ?",
                    (
                        str(identity.tvdb_id or identity.provider_id),
                        item.season_number,
                        item.episode_number,
                    ),
                ).fetchall()
            for row in rows:
                changed = connection.execute(
                    "UPDATE subscription_units SET status='available',visible_in_plex_at=COALESCE(visible_in_plex_at,?),plex_item_id=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                    (_iso(), plex_item_id, _iso(), int(row[2]), int(row[3])),
                ).rowcount
                matched += int(changed)
                if changed and row[1] is not None:
                    connection.execute(
                        "UPDATE requests SET status = CASE WHEN status IN ('requested','accepted','downloading','imported_to_arr') THEN 'visible_in_plex' ELSE status END, updated_at=? WHERE id=?",
                        (_iso(), int(row[1])),
                    )
        return matched

    def run_incremental_reconciliation(self, lease: LeaderLease | None = None) -> int:
        # Webhook inbox is the cursor for incremental work; claiming it again
        # is harmless because handled rows are terminal.  A bounded Plex
        # identity check catches an event received before persistence.
        return self.drain_inbox(lease or self._assert_leader(), limit=MAX_INBOX_BATCH)

    def run_full_reconciliation(self, lease: LeaderLease | None = None) -> int:
        active = lease or self._assert_leader()
        self._full_complete = False
        self._begin_activation_baseline()
        with self.database.connection() as connection:
            activation_row = connection.execute(
                "SELECT status,baseline_completed_at FROM activation WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
        activation_status = (
            "pending" if activation_row is None else str(activation_row[0])
        )
        # Pending -> baseline is the independent first pass.  A later full
        # sweep while still baseline is pass two; only that second complete
        # identity set can enable delivery.
        baseline_complete = bool(activation_row is not None and activation_row[1])
        self._active_sweep_phase = (
            "baseline"
            if activation_status == "baseline" and not baseline_complete
            else "pass2"
            if activation_status == "baseline"
            else None
        )
        if activation_status == "active":
            self._active_sweep_phase = "active"
        count = 0
        libraries = _call(getattr(self.plex, "libraries"))
        all_complete = True
        for library in libraries if isinstance(libraries, Sequence) else ():
            library_uuid = str(_value(library, "uuid", "key", default=""))
            cursor_now = _iso()
            seen: set[str] = set()
            try:
                iterator = _call(
                    getattr(self.plex, "iter_library_items"), library, page_size=100
                )
                for item in (
                    cast(Iterable[object], iterator) if iterator is not None else ()
                ):
                    if count >= MAX_SWEEP_ITEMS:
                        raise ProviderRetryable("Plex full sweep exceeded bound")
                    if isinstance(item, PlexItem):
                        seen.add(item.rating_key)
                        # Full sweep items are already typed but may not carry
                        # playable evidence; re-fetch is the authority.
                        event = NormalizedPlexEvent(
                            event_type="library.new",
                            server_uuid=str(getattr(self.plex, "server_uuid", "")),
                            machine_identifier=item.machine_identifier,
                            library_uuid=library_uuid,
                            library_name=str(
                                _value(library, "title", default="Library")
                            ),
                            rating_key=item.rating_key,
                            media_type=item.media_type.value,
                            title=item.title,
                            year=item.year,
                            season_number=item.season_number,
                            episode_number=item.episode_number,
                            added_at=item.added_at,
                            source="plex_reconciliation",
                        )
                        try:
                            self.process_event(event)
                            count += 1
                        except ProviderRetryable:
                            all_complete = False
                            continue
                if all_complete:
                    self._mark_missing_tombstones(library_uuid, seen)
                self._record_sweep(
                    library_uuid, cursor_now, full=all_complete, epoch=active.epoch
                )
            except Exception:
                all_complete = False
                self._record_sweep(
                    library_uuid, cursor_now, full=False, epoch=active.epoch
                )
        if all_complete:
            self._activate_if_complete()
            self._full_complete = self._durable_scan_complete()
        self._active_sweep_phase = None
        return count

    def _begin_activation_baseline(self) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status,baseline_started_at FROM activation WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
            if row is not None and str(row[0]) == "pending":
                connection.execute(
                    "UPDATE activation SET status='baseline',baseline_started_at=COALESCE(baseline_started_at,?),version=version+1,updated_at=? WHERE activation_id=?",
                    (_iso(), _iso(), self.activation_id),
                )

    def _mark_missing_tombstones(self, library_uuid: str, seen: set[str]) -> None:
        server_uuid = str(getattr(self.plex, "server_uuid", ""))
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id,rating_key,tombstone_generation FROM plex_items WHERE server_uuid=? AND library_uuid=? AND lifecycle_status='active'",
                (server_uuid, library_uuid),
            ).fetchall()
            for row in rows:
                if str(row[1]) in seen:
                    continue
                connection.execute(
                    "UPDATE plex_items SET lifecycle_status='tombstone',visible_in_plex_at=NULL,version=version+1,updated_at=? WHERE id=? AND lifecycle_status='active'",
                    (_iso(), int(row[0])),
                )

    def _durable_scan_complete(self) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*),SUM(CASE WHEN status='complete' AND last_full_sweep_at IS NOT NULL THEN 1 ELSE 0 END) FROM activation_cursors WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
        return bool(
            row and int(row[0] or 0) > 0 and int(row[0] or 0) == int(row[1] or 0)
        )

    def _record_sweep(
        self, library_uuid: str, observed_at: str, *, full: bool, epoch: int
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO activation_cursors(activation_id,server_uuid,library_uuid,scan_generation,last_incremental_at,last_full_sweep_at,status,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(activation_id,server_uuid,library_uuid) DO UPDATE SET scan_generation=activation_cursors.scan_generation+1,last_incremental_at=excluded.last_incremental_at,last_full_sweep_at=CASE WHEN ? THEN excluded.last_full_sweep_at ELSE activation_cursors.last_full_sweep_at END,status=excluded.status,updated_at=excluded.updated_at",
                (
                    self.activation_id,
                    str(getattr(self.plex, "server_uuid", "")),
                    library_uuid,
                    epoch,
                    observed_at,
                    observed_at if full else None,
                    "complete" if full else "partial",
                    observed_at,
                    1 if full else 0,
                ),
            )

    def _activate_if_complete(self) -> None:
        now = _iso()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*), SUM(CASE WHEN status='complete' AND last_full_sweep_at IS NOT NULL THEN 1 ELSE 0 END) FROM activation_cursors WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
            if (
                row is None
                or int(row[0] or 0) == 0
                or int(row[1] or 0) < int(row[0] or 0)
            ):
                return
            state = connection.execute(
                "SELECT status FROM activation WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
            if state is None or str(state[0]) == "active":
                return
            # The first independent identity set establishes the baseline;
            # it is never allowed to activate by itself.  Its completion is
            # persisted before the process returns so a restart resumes at
            # pass two rather than synthesizing a second set from the same
            # rows.
            if self._active_sweep_phase == "baseline" or str(state[0]) == "pending":
                connection.execute(
                    "UPDATE activation SET status='baseline',baseline_completed_at=COALESCE(baseline_completed_at,?),version=version+1,updated_at=? WHERE activation_id=?",
                    (now, now, self.activation_id),
                )
                return
            connection.execute(
                "UPDATE activation SET status='active',delivery_enabled=1,baseline_started_at=COALESCE(baseline_started_at,?),baseline_completed_at=COALESCE(baseline_completed_at,?),activated_at=COALESCE(activated_at,?),version=version+1,updated_at=? WHERE activation_id=?",
                (now, now, now, now, self.activation_id),
            )

    def _durable_ready(self) -> bool:
        try:
            leader = self.database.current_leader("media")
            return bool(
                leader
                and self.leader
                and leader.owner == self.worker_id
                and self._durable_scan_complete()
                and self._activation_status() == "active"
            )
        except Exception:
            return False

    def plan_notifications(self) -> int:
        """Materialize planner groups/deliveries at obligation grain."""
        admin_destinations = self._admin_destinations()
        with self.database.connection() as connection:
            activation = connection.execute(
                "SELECT status,delivery_enabled FROM activation WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
            if (
                activation is None
                or str(activation[0]) != "active"
                or not bool(activation[1])
            ):
                return 0
            units = connection.execute(
                "SELECT u.id,u.subscription_id,u.logical_unit_key,u.unit_type,u.season_number,u.episode_number,u.visible_in_plex_at,u.plex_item_id,s.user_id,s.chat_id,s.destination,s.notification_class,s.provider_id,s.generation,s.media_type,s.status FROM subscription_units u JOIN subscriptions s ON s.id=u.subscription_id WHERE u.status='available' AND u.visible_in_plex_at IS NOT NULL AND s.status='active' AND NOT EXISTS (SELECT 1 FROM activation_members am WHERE am.activation_id=? AND am.server_uuid=(SELECT server_uuid FROM plex_items WHERE id=u.plex_item_id) AND am.library_uuid=(SELECT library_uuid FROM plex_items WHERE id=u.plex_item_id) AND am.rating_key=(SELECT rating_key FROM plex_items WHERE id=u.plex_item_id) AND am.classification='historical') ORDER BY u.visible_in_plex_at,u.id LIMIT 5000",
                (self.activation_id,),
            ).fetchall()
            count = 0
            for unit in units:
                item = connection.execute(
                    "SELECT * FROM plex_items WHERE id=?", (int(unit[7]),)
                ).fetchone()
                if item is None:
                    continue
                # Keep this projection aligned with the explicit SELECT
                # above: [8] user, [9] chat, [10] destination, [11] class,
                # [12] provider, [13] generation, [14] media type.
                destination = str(unit[10])
                chat_id = int(unit[9])
                notification_class = str(unit[11])
                provider_id = str(unit[12])
                generation = int(unit[13])
                obligation = f"{int(unit[1])}:{int(unit[0])}:{generation}:{notification_class}:{chat_id}"
                existing = connection.execute(
                    "SELECT delivery_id FROM delivery_memberships "
                    "WHERE subscription_id=? AND subscription_generation=? AND unit_id=?",
                    (int(unit[1]), generation, int(unit[0])),
                ).fetchone()
                if existing is not None:
                    continue
                group_key = hashlib.sha256(
                    f"{destination}:{chat_id}:{notification_class}:{unit[4] or 0}:{generation}".encode()
                ).hexdigest()
                first_seen = str(unit[6])
                due = _iso((_parse_time(first_seen) or _now()) + timedelta(minutes=5))
                connection.execute(
                    "INSERT OR IGNORE INTO notification_groups(group_key,destination,chat_id,notification_class,canonical_show_identity,season_number,window_generation,first_seen_at,due_at,status,payload_json) VALUES(?,?,?,?,?,?,1,?,?, 'ready', ?)",
                    (
                        group_key,
                        destination,
                        chat_id,
                        notification_class,
                        provider_id,
                        unit[4],
                        first_seen,
                        due,
                        _json(
                            {
                                "title": item[7],
                                "year": item[8],
                                "plex_url": item[19],
                                "season_number": item[10],
                                "episode_number": item[11],
                            }
                        ),
                    ),
                )
                group = connection.execute(
                    "SELECT id FROM notification_groups WHERE group_key=?", (group_key,)
                ).fetchone()
                if group is None:
                    continue
                payload = {
                    "text": self._delivery_text(item),
                    "title": item[7],
                    "plex_url": item[19],
                }
                delivery_row = connection.execute(
                    "SELECT id FROM deliveries WHERE group_id=? AND destination=? "
                    "AND chat_id=? AND notification_class=? ORDER BY id LIMIT 1",
                    (int(group[0]), destination, chat_id, notification_class),
                ).fetchone()
                delivery = 0
                if delivery_row is None:
                    delivery = connection.execute(
                        "INSERT OR IGNORE INTO deliveries(group_id,destination,chat_id,notification_class,event_key,subscription_generation,idempotency_key,status,obligation_key,chunk_ordinal,chunk_count) VALUES(?,?,?,?,?,?,?,'pending',?,1,1)",
                        (
                            int(group[0]),
                            destination,
                            chat_id,
                            notification_class,
                            str(item[3]),
                            generation,
                            hashlib.sha256(obligation.encode()).hexdigest(),
                            obligation,
                        ),
                    ).rowcount
                    delivery_row = connection.execute(
                        "SELECT id FROM deliveries WHERE obligation_key=?",
                        (obligation,),
                    ).fetchone()
                if delivery_row is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO delivery_memberships(delivery_id,subscription_id,subscription_generation,unit_id,status) VALUES(?,?,?,?,'eligible')",
                        (int(delivery_row[0]), int(unit[1]), generation, int(unit[0])),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO delivery_chunks(delivery_id,ordinal,chunk_count,stable_key,payload_json,status) VALUES(?,?,?,?,?,'pending')",
                        (
                            int(delivery_row[0]),
                            1,
                            1,
                            hashlib.sha256((obligation + ":1").encode()).hexdigest(),
                            _json(payload),
                        ),
                    )
                    if (
                        str(unit[3]) == "episode"
                        and str(unit[15]) == "season_completion"
                    ):
                        show = html.escape(str(item[9] or item[7])[:512], quote=True)
                        season = (
                            int(item[10]) if item[10] is not None else int(unit[4] or 0)
                        )
                        link = (
                            html.escape(str(item[19]), quote=True) if item[19] else ""
                        )
                        summary = f"<b>{show}</b> — Season {season} is available"
                        if link:
                            summary += f'\n<a href="{link}">Open in Plex</a>'
                        connection.execute(
                            "UPDATE delivery_chunks SET payload_json=? WHERE delivery_id=? AND ordinal=1 AND status='pending'",
                            (
                                _json(
                                    {
                                        "text": summary,
                                        "title": item[9] or item[7],
                                        "plex_url": item[19],
                                    }
                                ),
                                int(delivery_row[0]),
                            ),
                        )
                count += int(delivery > 0)
            # Every eligible post-activation Plex-visible unit is also an
            # admin obligation.  This covers manual/import-list additions
            # which have no requester subscription.
            activation_row = connection.execute(
                "SELECT activated_at FROM activation WHERE activation_id=?",
                (self.activation_id,),
            ).fetchone()
            activated_at = (
                str(activation_row[0]) if activation_row and activation_row[0] else ""
            )
            if admin_destinations and activated_at:
                admin_rows = connection.execute(
                    """
                    SELECT DISTINCT p.*
                    FROM plex_items p
                    LEFT JOIN activation_members am
                      ON am.activation_id=? AND am.server_uuid=p.server_uuid
                     AND am.library_uuid=p.library_uuid AND am.rating_key=p.rating_key
                     AND am.tombstone_generation=p.tombstone_generation
                    WHERE p.lifecycle_status='active' AND p.visible_in_plex_at IS NOT NULL
                      AND (p.visible_in_plex_at > ? OR am.classification='new')
                    ORDER BY p.visible_in_plex_at,p.id LIMIT 5000
                    """,
                    (self.activation_id, activated_at),
                ).fetchall()
                for item in admin_rows:
                    for admin_chat in admin_destinations:
                        same_chat = connection.execute(
                            """
                            SELECT d.id,d.group_id
                            FROM deliveries d
                            JOIN delivery_memberships dm ON dm.delivery_id=d.id
                            JOIN subscription_units u ON u.id=dm.unit_id
                            WHERE d.chat_id=? AND u.plex_item_id=?
                              AND d.status IN ('pending','ready','retry_wait')
                            ORDER BY CASE d.notification_class WHEN 'admin' THEN 0 ELSE 1 END,d.id
                            LIMIT 1
                            """,
                            (admin_chat, int(item[0])),
                        ).fetchone()
                        if same_chat is not None:
                            connection.execute(
                                "UPDATE deliveries SET notification_class='admin',version=version+1 WHERE id=?",
                                (int(same_chat[0]),),
                            )
                            connection.execute(
                                "UPDATE notification_groups SET notification_class='admin',version=version+1 WHERE id=?",
                                (int(same_chat[1]),),
                            )
                            continue
                        obligation = f"admin:{admin_chat}:{int(item[0])}:{int(item[5])}"
                        if (
                            connection.execute(
                                "SELECT id FROM deliveries WHERE obligation_key=?",
                                (obligation,),
                            ).fetchone()
                            is not None
                        ):
                            continue
                        first_seen = str(item[22] or item[21] or activated_at)
                        window = (
                            int((_parse_time(first_seen) or _now()).timestamp()) // 300
                        )
                        group_key = hashlib.sha256(
                            f"admin:{admin_chat}:{window}".encode()
                        ).hexdigest()
                        due = _iso(
                            (_parse_time(first_seen) or _now()) + timedelta(minutes=5)
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO notification_groups(group_key,destination,chat_id,notification_class,canonical_show_identity,season_number,window_generation,first_seen_at,due_at,status,payload_json) VALUES(?,?,?,?,?,?,1,?,?, 'ready', ?)",
                            (
                                group_key,
                                str(admin_chat),
                                admin_chat,
                                "admin",
                                str(item[14] or item[15] or item[16] or item[3]),
                                item[10],
                                first_seen,
                                due,
                                _json(
                                    {
                                        "title": item[7],
                                        "year": item[8],
                                        "plex_url": item[19],
                                        "season_number": item[10],
                                        "episode_number": item[11],
                                    }
                                ),
                            ),
                        )
                        group = connection.execute(
                            "SELECT id FROM notification_groups WHERE group_key=?",
                            (group_key,),
                        ).fetchone()
                        if group is None:
                            continue
                        connection.execute(
                            "INSERT OR IGNORE INTO deliveries(group_id,destination,chat_id,notification_class,event_key,subscription_generation,idempotency_key,status,obligation_key,chunk_ordinal,chunk_count) VALUES(?,?,?,?,?,?,?,'pending',?,1,1)",
                            (
                                int(group[0]),
                                str(admin_chat),
                                admin_chat,
                                "admin",
                                f"plex:{item[0]}",
                                None,
                                hashlib.sha256(obligation.encode()).hexdigest(),
                                obligation,
                            ),
                        )
                        delivery_row = connection.execute(
                            "SELECT id FROM deliveries WHERE obligation_key=?",
                            (obligation,),
                        ).fetchone()
                        if delivery_row is not None:
                            connection.execute(
                                "INSERT OR IGNORE INTO delivery_chunks(delivery_id,ordinal,chunk_count,stable_key,payload_json,status) VALUES(?,?,?,?,?,'pending')",
                                (
                                    int(delivery_row[0]),
                                    1,
                                    1,
                                    hashlib.sha256(
                                        (obligation + ":1").encode()
                                    ).hexdigest(),
                                    _json(
                                        {
                                            "text": self._delivery_text(item),
                                            "title": item[7],
                                            "plex_url": item[19],
                                        }
                                    ),
                                ),
                            )
                            count += 1
        return count

    def _admin_destinations(self) -> tuple[int, ...]:
        if self.policy is None:
            return ()
        current_users = getattr(self.policy, "current_users", None)
        resolver = getattr(self.policy, "resolve_identity", None)
        if not callable(current_users) or not callable(resolver):
            return ()
        try:
            current = _call(current_users)
            users = current.get("users", ()) if isinstance(current, Mapping) else ()
            result: list[int] = []
            for user in users if isinstance(users, Sequence) else ():
                if not isinstance(user, Mapping) or user.get("role") != "admin":
                    continue
                resolved = _call(resolver, user_id=user.get("user_id"))
                chat_id = _value(resolved, "chat_id")
                if isinstance(chat_id, int) and chat_id != 0 and chat_id not in result:
                    result.append(chat_id)
            return tuple(result[:16])
        except Exception:
            return ()

    @staticmethod
    def _delivery_text(item: object) -> str:
        row = cast(Sequence[object], item)
        title = html.escape(str(row[7])[:512], quote=True)
        year = f" ({int(cast(Any, row[8]))})" if row[8] is not None else ""
        episode = ""
        if row[10] is not None and row[11] is not None:
            episode = f" — S{int(cast(Any, row[10])):02d}E{int(cast(Any, row[11])):02d}"
        link = html.escape(str(row[19]), quote=True) if row[19] else ""
        return f"<b>{title}</b>{year}{episode}" + (
            f'\n<a href="{link}">Open in Plex</a>' if link else ""
        )

    def deliver_pending(
        self, lease: LeaderLease | None = None, *, limit: int = MAX_DELIVERIES_BATCH
    ) -> int:
        active = lease or self._assert_leader()
        if self.telegram is None:
            return 0
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT d.id FROM deliveries d LEFT JOIN notification_groups g ON g.id=d.group_id WHERE d.status IN ('pending','ready','retry_wait') AND (d.retry_due_at IS NULL OR d.retry_due_at <= ?) AND (g.due_at IS NULL OR g.due_at <= ?) ORDER BY d.id LIMIT ?",
                (_iso(), _iso(), limit),
            ).fetchall()
        sent = 0
        for row in rows:
            delivery_id = int(row[0])
            claim = self.database.claim_delivery(
                delivery_id,
                lease_seconds=CLAIM_LEASE_SECONDS,
                worker_id=self.worker_id,
                leader_epoch=active.epoch,
            )
            if claim is None:
                continue
            with self.database.connection() as connection:
                delivery = connection.execute(
                    "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
                ).fetchone()
                chunk = connection.execute(
                    "SELECT * FROM delivery_chunks WHERE delivery_id=? ORDER BY ordinal LIMIT 1",
                    (delivery_id,),
                ).fetchone()
            if delivery is None or chunk is None:
                self.database.release_claim(
                    "deliveries",
                    delivery_id,
                    claim,
                    status="failed",
                    error="delivery payload missing",
                )
                continue
            text = ""
            try:
                payload = json.loads(str(chunk["payload_json"]))
                text = (
                    str(payload.get("text", "")) if isinstance(payload, Mapping) else ""
                )
                # Keep the parent in ``claimed`` until the transport outcome
                # is known.  This is the only parent state from which both a
                # retryable pre-transmission error and a sent outcome are
                # legal.  The chunk records the in-flight boundary, while
                # the parent claim remains the fencing token for the whole
                # operation.
                chunk_claimed = self.database.compare_and_swap(
                    "delivery_chunks",
                    int(chunk["id"]),
                    expected={"status": "pending"},
                    updates={"status": "claimed"},
                )
                chunk_sending = chunk_claimed and self.database.compare_and_swap(
                    "delivery_chunks",
                    int(chunk["id"]),
                    expected={"status": "claimed"},
                    updates={"status": "sending", "sending_started_at": _iso()},
                )
                if not chunk_sending:
                    self.database.release_claim(
                        "deliveries",
                        delivery_id,
                        claim,
                        status="failed",
                        error="delivery chunk claim lost",
                    )
                    continue
                result = _call(
                    getattr(self.telegram, "send_message"),
                    int(delivery["chat_id"]),
                    text,
                    parse_mode="HTML",
                )
                message_id = _value(result, "message_id")
                parent_sending = self.database.compare_and_swap(
                    "deliveries",
                    delivery_id,
                    expected={"claim_token": claim},
                    updates={"status": "sending", "sending_started_at": _iso()},
                )
                if not parent_sending:
                    # A lost claim is deliberately left for reconciliation;
                    # never acknowledge a message without a fenced parent.
                    self.database.compare_and_swap(
                        "delivery_chunks",
                        int(chunk["id"]),
                        expected={"status": "sending"},
                        updates={"status": "unknown", "unknown_at": _iso()},
                    )
                    continue
                completed = self.database.complete_claim(
                    "deliveries",
                    delivery_id,
                    claim,
                    status="sent",
                    updates={
                        "telegram_message_id": message_id
                        if isinstance(message_id, int)
                        else None,
                        "sent_at": _iso(),
                    },
                )
                chunk_completed = self.database.compare_and_swap(
                    "delivery_chunks",
                    int(chunk["id"]),
                    expected={"status": "sending"},
                    updates={
                        "status": "sent",
                        "telegram_message_id": message_id
                        if isinstance(message_id, int)
                        else None,
                        "sent_at": _iso(),
                    },
                )
                sent += int(completed and chunk_completed)
            except TelegramError as exc:
                if exc.error_class is TelegramErrorClass.AMBIGUOUS or exc.transmitted:
                    self.database.compare_and_swap(
                        "delivery_chunks",
                        int(chunk["id"]),
                        expected={"status": "sending"},
                        updates={"status": "unknown", "unknown_at": _iso()},
                    )
                    self.database.release_claim(
                        "deliveries",
                        delivery_id,
                        claim,
                        status="unknown",
                        error=cast(str, exc),
                    )
                elif exc.pre_transmission:
                    retry_at = _now() + timedelta(seconds=exc.retry_after or 60)
                    # Chunk triggers intentionally require an explicit
                    # failed->pending recovery; the parent carries the
                    # retry_wait schedule.
                    if self.database.compare_and_swap(
                        "delivery_chunks",
                        int(chunk["id"]),
                        expected={"status": "sending"},
                        updates={"status": "failed"},
                    ):
                        self.database.compare_and_swap(
                            "delivery_chunks",
                            int(chunk["id"]),
                            expected={"status": "failed"},
                            updates={"status": "pending"},
                        )
                    self.database.release_claim(
                        "deliveries",
                        delivery_id,
                        claim,
                        status="retry_wait",
                        error=cast(str, exc),
                        retry_at=retry_at,
                    )
                else:
                    self.database.compare_and_swap(
                        "delivery_chunks",
                        int(chunk["id"]),
                        expected={"status": "sending"},
                        updates={"status": "failed"},
                    )
                    self.database.release_claim(
                        "deliveries",
                        delivery_id,
                        claim,
                        status="failed",
                        error=cast(str, exc),
                    )
            except Exception as exc:
                if self.database.compare_and_swap(
                    "delivery_chunks",
                    int(chunk["id"]),
                    expected={"status": "sending"},
                    updates={"status": "failed"},
                ):
                    self.database.compare_and_swap(
                        "delivery_chunks",
                        int(chunk["id"]),
                        expected={"status": "failed"},
                        updates={"status": "pending"},
                    )
                self.database.release_claim(
                    "deliveries",
                    delivery_id,
                    claim,
                    status="retry_wait",
                    error=cast(str, exc),
                    retry_at=_now() + timedelta(minutes=1),
                )
        return sent

    def cleanup(self) -> int:
        cutoff = _iso(_now() - timedelta(seconds=RETENTION_SECONDS))
        with self.database.transaction() as connection:
            total = 0
            # Expiry columns intentionally use their native representation;
            # mixing epoch integers and ISO text can retain private data or
            # delete live capabilities after a restart.
            for table, column, value in (
                ("safe_snapshots", "expires_at", int(time.time())),
                ("request_candidates", "expires_at", cutoff),
                ("actor_nonces", "expires_at", int(time.time())),
                ("confirmation_capabilities", "expires_at", int(time.time())),
                ("dashboard_confirmations", "expires_at", cutoff),
                ("audit_events", "created_at", cutoff),
            ):
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                if column not in columns:
                    continue
                result = connection.execute(
                    f"DELETE FROM {table} WHERE {column} <= ?", (value,)
                )
                total += int(result.rowcount)
            # Terminal delivery records retain only bounded accounting rows;
            # memberships/chunks are retained while the parent is retained.
            result = connection.execute(
                "DELETE FROM deliveries WHERE status IN ('sent','assumed_sent','abandoned') AND COALESCE(sent_at,abandoned_at,terminal_at) <= ?",
                (cutoff,),
            )
            total += int(result.rowcount)
            return total

    def recover_delivery(
        self, operation: str, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        delivery_id = _positive_id(
            arguments.get("delivery_id", arguments.get("id")), field="delivery_id"
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status,version FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            if row is None:
                return {"ok": False, "status": "not_found"}
            status = str(row[0])
            if operation == "retry" and status in {"failed", "retry_wait"}:
                connection.execute(
                    "UPDATE deliveries SET status='pending',retry_due_at=NULL,version=version+1,updated_at=? WHERE id=? AND version=?",
                    (_iso(), delivery_id, int(row[1])),
                )
            elif operation == "abandon" and status in {
                "failed",
                "unknown",
                "retry_wait",
            }:
                connection.execute(
                    "UPDATE deliveries SET status='abandoned',abandoned_at=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                    (_iso(), _iso(), delivery_id, int(row[1])),
                )
            elif operation == "assume_sent" and status == "unknown":
                connection.execute(
                    "UPDATE deliveries SET status='assumed_sent',unknown_resolved=1,version=version+1,updated_at=? WHERE id=? AND version=?",
                    (_iso(), delivery_id, int(row[1])),
                )
            elif operation == "resend" and status == "unknown":
                connection.execute(
                    "UPDATE deliveries SET status='pending',resend_generation=resend_generation+1,unknown_resolved=1,version=version+1,updated_at=? WHERE id=? AND version=?",
                    (_iso(), delivery_id, int(row[1])),
                )
            else:
                return {"ok": False, "status": status}
        return {"ok": True, "status": operation, "delivery_id": delivery_id}

    def oracle(self) -> Mapping[str, object]:
        with self.database.connection() as connection:
            obligations = connection.execute(
                "SELECT COUNT(*) FROM subscription_units u JOIN subscriptions s ON s.id=u.subscription_id WHERE u.status='available' AND u.visible_in_plex_at IS NOT NULL AND s.status='active'"
            ).fetchone()
            accounted = connection.execute(
                "SELECT COUNT(DISTINCT m.unit_id) FROM delivery_memberships m JOIN deliveries d ON d.id=m.delivery_id WHERE m.unit_id IS NOT NULL AND d.status NOT IN ('canceled','cancelled','superseded')"
            ).fetchone()
            pending = connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE status IN ('pending','ready','claimed','sending','retry_wait','failed','unknown','sent','assumed_sent','abandoned','delivery_blocked')"
            ).fetchone()
            unresolved = connection.execute(
                "SELECT COUNT(*) FROM quarantined_records WHERE status='open'"
            ).fetchone()
        total = int((obligations[0] if obligations else 0) or 0)
        covered = int((accounted[0] if accounted else 0) or 0)
        residual = max(0, total - covered)
        return {
            "ready": self._durable_ready(),
            "residual": residual,
            "obligations": total,
            "accounted": covered,
            "deliveries": int((pending[0] if pending else 0) or 0),
            "quarantined": int((unresolved[0] if unresolved else 0) or 0),
        }

    def status(self) -> Mapping[str, object]:
        return {
            "worker_id": self.worker_id,
            "leader_epoch": self.leader.epoch if self.leader else None,
            "ready": self._ready,
            "last_incremental_at": _iso(self._last_incremental)
            if self._last_incremental
            else None,
            "last_full_sweep_at": _iso(self._last_full) if self._last_full else None,
            "last_error": self._last_error,
        }

    def ready(self) -> bool:
        try:
            if self.leader is None:
                return False
            current = self.database.current_leader("media")
            return bool(
                current and current.owner == self.worker_id and self._durable_ready()
            )
        except Exception:
            return False

    is_ready = ready


def _build_clients(
    config: object,
    *,
    plex: object | None,
    radarr: object | None,
    sonarr: object | None,
    telegram: object | None,
    notification_helper: object | None,
    allowed_library_names: Sequence[str],
) -> tuple[object, object | None, object | None, object | None]:
    reader = FileSecretReader()
    if plex is None:
        endpoint = getattr(config, "plex_url", None)
        token = getattr(config, "plex_token_file", None)
        if endpoint is None or token is None:
            raise ProductionRuntimeError("Plex URL and token file are required")
        plex = PlexClient(
            endpoint,
            token,
            config=config,
            secret_reader=reader,
            server_uuid=getattr(config, "plex_server_uuid", None),
            machine_identifier=getattr(config, "plex_machine_identifier", None),
            allowed_library_names=allowed_library_names,
        )
    if radarr is None and getattr(config, "radarr_url", None) is not None:
        key = getattr(config, "radarr_api_key_file", None)
        if key is None:
            raise ProductionRuntimeError("Radarr API key file is required")
        radarr = RadarrClient(
            getattr(config, "radarr_url"), key, config=config, secret_reader=reader
        )
    if sonarr is None and getattr(config, "sonarr_url", None) is not None:
        key = getattr(config, "sonarr_api_key_file", None)
        if key is None:
            raise ProductionRuntimeError("Sonarr API key file is required")
        sonarr = SonarrClient(
            getattr(config, "sonarr_url"), key, config=config, secret_reader=reader
        )
    if telegram is None and notification_helper is not None:
        telegram = HermesTelegramBridge(notification_helper)
    if telegram is None:
        token = getattr(config, "telegram_bot_token_file", None)
        if token is None:
            raise ProductionRuntimeError("Telegram bot token file is required")
        telegram = TelegramClient(token, config=config, secret_reader=reader)
    return plex, radarr, sonarr, telegram


def build_runtime(
    *,
    config: object,
    database: Database,
    rate_limiter: object,
    nonce_store: object,
    confirmation_store: object,
    actor_verifier: object,
    policy: object,
    upstream: object,
    dashboard_api_key: bytes | str,
    helper_key: bytes | str,
    plex_capability: object,
    expected_server_uuid: str | None = None,
    allowed_server_uuids: Sequence[str] = (),
    allowed_library_ids: Sequence[str] = (),
    allowed_library_names: Sequence[str] = (),
    trusted_ingress_peers: Sequence[str] = (),
    readiness: Callable[[], object] | None = None,
    plex: object | None = None,
    radarr: object | None = None,
    sonarr: object | None = None,
    telegram: object | None = None,
    notification_helper: object | None = None,
    require_candidate_context: bool = True,
    worker: DurableMediaWorker | None = None,
    **_: object,
) -> CompanionRuntime:
    """Construct the real production runtime used by ``app.py``."""

    if not isinstance(database, Database):
        raise ProductionRuntimeError("production requires the canonical Database")
    helper = notification_helper
    if helper is None and callable(getattr(policy, "send_notification", None)):
        helper = policy
    plex, radarr, sonarr, telegram = _build_clients(
        config,
        plex=plex,
        radarr=radarr,
        sonarr=sonarr,
        telegram=telegram,
        notification_helper=helper,
        allowed_library_names=allowed_library_names,
    )
    request_store = SQLiteRequestStore(database, migrate=False)
    workflow = RequestWorkflow(
        store=request_store,
        radarr=cast(MovieProvider | None, radarr),
        sonarr=cast(SeriesProvider | None, sonarr),
        plex=cast(PlexVisibilityProvider | None, plex),
        require_candidate_context=require_candidate_context,
    )
    inbox = DurablePlexInbox(database)
    if worker is None:
        worker = DurableMediaWorker(
            database=database,
            plex=plex,
            radarr=radarr,
            sonarr=sonarr,
            telegram=telegram,
            policy=policy,
            workflow=workflow,
            inbox=inbox,
        )
    operations = ProductionOperations(
        database=database,
        plex=plex,
        radarr=radarr,
        sonarr=sonarr,
        telegram=telegram,
        policy=policy,
        workflow=workflow,
        worker=worker,
        cursor_key=helper_key,
        upstream=upstream,
    )
    safe_handlers = {name: getattr(operations, name) for name in SHARED_TOOL_SET}
    dashboard_handlers = {
        name: getattr(operations, name.replace(".", "_"))
        for name in DASHBOARD_OPERATION_SET
    }

    # The dashboard's browser/session actor is intentionally an opaque label
    # (normally ``dashboard-admin``).  Resolve it through Hermes' current
    # admin inventory, then enrich the selected numeric identity with the
    # current semantic fingerprint/version used for CAS and confirmation.
    def _current_admin_identity() -> Mapping[str, object]:
        current = _call(getattr(policy, "current_users"))
        if not isinstance(current, Mapping):
            raise ProductionRuntimeError("policy helper current users are unavailable")
        users = current.get("users", current.get("items", ()))
        admins = (
            [
                item
                for item in users
                if isinstance(item, Mapping)
                and item.get("role") == "admin"
                and isinstance(item.get("user_id"), int)
            ]
            if isinstance(users, Sequence)
            else []
        )
        if not admins:
            raise ProductionRuntimeError("no configured dashboard administrator")
        selected = min(admins, key=lambda item: int(cast(int, item["user_id"])))
        user_id = _positive_id(selected.get("user_id"), field="user_id")
        resolved = _call(getattr(policy, "resolve_identity"), user_id=user_id)
        if not isinstance(resolved, Mapping):
            raise ProductionRuntimeError(
                "policy helper identity resolution is unavailable"
            )
        result = dict(resolved)
        result.update(
            {
                "actor": "dashboard-admin",
                "allowed": True,
                "user_id": user_id,
                "role": "admin",
                "fingerprint": str(current.get("fingerprint", "")),
                "version": str(current.get("version", "")),
            }
        )
        if not result["fingerprint"] or not result["version"]:
            raise ProductionRuntimeError(
                "policy helper identity fingerprint is unavailable"
            )
        if not isinstance(result.get("chat_id"), int) or result["chat_id"] == 0:
            raise ProductionRuntimeError("policy helper chat identity is unavailable")
        return result

    def dashboard_identity(actor: str) -> object:
        # Dashboard actors are labels, never identities.  The configured API
        # layer must provide numeric IDs; accepting ``user:<id>`` is useful
        # for the loopback dashboard and remains strictly parseable.
        if actor == "dashboard-admin":
            return _current_admin_identity()
        if not isinstance(actor, str) or not actor.startswith("user:"):
            raise ProductionRuntimeError("dashboard actor identity is unresolved")
        try:
            user_id = _positive_id(int(actor[5:]), field="user_id")
        except (TypeError, ValueError) as exc:
            raise ProductionRuntimeError(
                "dashboard actor identity is unresolved"
            ) from exc
        current = _call(getattr(policy, "current_users"))
        if not isinstance(current, Mapping):
            raise ProductionRuntimeError("policy helper current users are unavailable")
        users = current.get("users", ())
        selected = (
            next(
                (
                    item
                    for item in users
                    if isinstance(item, Mapping) and item.get("user_id") == user_id
                ),
                None,
            )
            if isinstance(users, Sequence)
            else None
        )
        if not isinstance(selected, Mapping) or selected.get("role") != "admin":
            raise ProductionRuntimeError("dashboard administrator policy denied")
        resolved = _call(getattr(policy, "resolve_identity"), user_id=user_id)
        if not isinstance(resolved, Mapping):
            raise ProductionRuntimeError(
                "policy helper identity resolution is unavailable"
            )
        result = dict(resolved)
        result.update(
            {
                "actor": "dashboard-admin",
                "allowed": True,
                "user_id": user_id,
                "role": "admin",
                "fingerprint": str(current.get("fingerprint", "")),
                "version": str(current.get("version", "")),
            }
        )
        return result

    def dashboard_policy(
        *, actor: str, identity: object, operation: str, arguments: Mapping[str, object]
    ) -> object:
        del actor, operation, arguments
        if (
            not isinstance(identity, Mapping)
            or str(identity.get("role", "")) != "admin"
        ):
            raise ProductionRuntimeError("dashboard admin policy denied")
        return identity

    def dashboard_mutation(
        *, actor: str, identity: object, operation: str, arguments: Mapping[str, object]
    ) -> object:
        del actor, operation, arguments
        if (
            not isinstance(identity, Mapping)
            or str(identity.get("role", "")) != "admin"
        ):
            raise ProductionRuntimeError("dashboard mutation policy denied")
        return True

    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS production_confirmation_arguments(
                token_hash TEXT PRIMARY KEY,
                tool TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                argument_hash TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                consumed_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_confirmations(
                token_hash TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                operation TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                preview TEXT NOT NULL,
                preview_digest TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                policy_fingerprint TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                version INTEGER NOT NULL DEFAULT 0
            )
            """
        )

    def dashboard_confirmation_issuer(**kwargs: object) -> Mapping[str, object]:
        actor = str(kwargs.get("actor", ""))
        operation = str(kwargs.get("operation", ""))
        arguments = kwargs.get("arguments", kwargs.get("payload", {}))
        preview = kwargs.get("preview")
        preview_digest = kwargs.get("preview_digest")
        identity = kwargs.get("identity")
        if (
            not isinstance(arguments, Mapping)
            or not isinstance(preview, str)
            or not isinstance(preview_digest, str)
            or not isinstance(identity, Mapping)
        ):
            raise ProductionRuntimeError(
                "dashboard confirmation arguments are incomplete"
            )
        user_id = _positive_id(identity.get("user_id"), field="user_id")
        chat_id = identity.get("chat_id")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
            raise ProductionRuntimeError(
                "dashboard confirmation chat identity is invalid"
            )
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        expires = _now() + timedelta(minutes=5)
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO dashboard_confirmations(token_hash,actor,operation,arguments_json,preview,preview_digest,user_id,chat_id,policy_fingerprint,policy_version,state,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                (
                    token_hash,
                    actor,
                    operation,
                    _json(arguments),
                    preview,
                    preview_digest,
                    user_id,
                    chat_id,
                    str(identity.get("fingerprint", "")),
                    str(identity.get("version", "")),
                    _iso(expires),
                ),
            )
        return {
            "confirmation_capability": token,
            "expires_at": int(expires.timestamp()),
        }

    def dashboard_confirmation_guard(**kwargs: object) -> object:
        token = kwargs.get("confirmation")
        actor = str(kwargs.get("actor", ""))
        operation = str(kwargs.get("operation", ""))
        arguments = kwargs.get("arguments", kwargs.get("payload", {}))
        preview = kwargs.get("preview")
        preview_digest = kwargs.get("preview_digest")
        identity = kwargs.get("identity")
        if (
            not isinstance(token, str)
            or not isinstance(arguments, Mapping)
            or not isinstance(preview, str)
            or not isinstance(preview_digest, str)
            or not isinstance(identity, Mapping)
        ):
            return False
        try:
            token_hash = hashlib.sha256(token.encode("ascii", "strict")).hexdigest()
        except UnicodeError:
            return False
        now = _iso()
        with database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM dashboard_confirmations WHERE token_hash=? AND state='pending' AND expires_at>?",
                (token_hash, now),
            ).fetchone()
            if row is None:
                return False
            expected = (
                str(row["actor"]),
                str(row["operation"]),
                str(row["arguments_json"]),
                str(row["preview"]),
                str(row["preview_digest"]),
                int(row["user_id"]),
                int(row["chat_id"]),
                str(row["policy_fingerprint"]),
                str(row["policy_version"]),
            )
            actual = (
                actor,
                operation,
                _json(arguments),
                preview,
                preview_digest,
                int(identity.get("user_id", 0)),
                int(identity.get("chat_id", 0)),
                str(identity.get("fingerprint", "")),
                str(identity.get("version", "")),
            )
            if expected != actual:
                return False
            changed = connection.execute(
                "UPDATE dashboard_confirmations SET state='consumed',consumed_at=?,version=version+1 WHERE token_hash=? AND state='pending' AND version=?",
                (now, token_hash, int(row["version"])),
            ).rowcount
            return bool(changed)

    def target_state(tool: str, arguments: Mapping[str, object]) -> str:
        return hashlib.sha256(
            _json({"tool": tool, "arguments": arguments}).encode()
        ).hexdigest()

    def revalidate(*args: object, **kwargs: object) -> bool:
        record = kwargs.get("record")
        policy_now = kwargs.get("policy")
        if record is not None and policy_now is not None:
            expected = str(getattr(record, "policy_version", ""))
            current = (
                str(getattr(policy_now, "version", ""))
                if not isinstance(policy_now, Mapping)
                else str(policy_now.get("version", ""))
            )
            if expected and current and expected != current:
                return False
        return True

    argument_store = SQLiteConfirmationArgumentsStore(database)
    executor = ProductionConfirmationExecutor(
        operations, database=database, arguments_store=argument_store
    )
    # Hermes owns preview delivery/binding in production.  The bridge remains
    # a typed seam for explicit non-production callers and never sends a
    # second Telegram message in this runtime.
    bridge = ProductionConfirmationBridge(executor)
    plex_limiter = SQLitePlexRateLimiter(database)

    def migration_ready() -> bool:
        return True

    return CompanionRuntime(
        actor_verifier=cast(Any, actor_verifier),
        confirmation_store=cast(Any, confirmation_store),
        nonce_store=nonce_store,
        policy=policy,
        safe_handlers=safe_handlers,
        upstream=upstream,
        confirmation_executor=executor,
        confirmation_bridge=bridge,
        confirmation_delivery_owner="hermes",
        confirmation_arguments_store=argument_store,
        helper_key=helper_key,
        dashboard_api_key=dashboard_api_key,
        dashboard_handlers=dashboard_handlers,
        dashboard_identity_resolver=dashboard_identity,
        dashboard_policy_recheck=dashboard_policy,
        dashboard_mutation_guard=dashboard_mutation,
        dashboard_confirmation_guard=dashboard_confirmation_guard,
        dashboard_confirmation_issuer=dashboard_confirmation_issuer,
        operations=operations,
        database=database,
        mutation_guard=SQLiteMutationGuard(database),
        rate_limiter=cast(Any, rate_limiter),
        target_state_callback=target_state,
        event_inbox=inbox,
        persist_event=inbox.persist_event,
        plex_capability=plex_capability,
        expected_server_uuid=expected_server_uuid,
        allowed_server_uuids=allowed_server_uuids
        or ((expected_server_uuid,) if expected_server_uuid else ()),
        allowed_library_ids=allowed_library_ids,
        allowed_library_names=allowed_library_names,
        plex_rate_limiter=plex_limiter,
        trusted_ingress_peers=trusted_ingress_peers,
        revalidate_confirmation=revalidate,
        migrations_ready=migration_ready,
        worker=worker,
        readiness=readiness,
        production=True,
    )


production_runtime_factory = build_runtime
build_production_runtime = build_runtime
create_production_runtime = build_runtime
MediaCompanionWorker = DurableMediaWorker
ProductionWorker = DurableMediaWorker


__all__ = [
    "DurableMediaWorker",
    "MediaCompanionWorker",
    "ProductionWorker",
    "DurablePlexInbox",
    "DurableSnapshotStore",
    "SQLitePlexRateLimiter",
    "SQLiteConfirmationArgumentsStore",
    "ProductionOperations",
    "ProductionConfirmationBridge",
    "ProductionConfirmationExecutor",
    "ProductionRuntimeError",
    "build_runtime",
    "build_production_runtime",
    "production_runtime_factory",
    "create_production_runtime",
]
