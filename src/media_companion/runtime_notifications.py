"""Durable notification planning and delivery seam.

The planner and delivery modules are intentionally pure.  This module is the
small adapter used by the worker: it takes verified :class:`CanonicalUnit`
records, writes the planner's obligations/groups in one SQLite transaction,
and drives the canonical ``deliveries``/``delivery_chunks`` tables with the
database claim and compare-and-swap primitives.

There are two useful properties of this seam:

* planning is idempotent.  A process may stop after any individual insert and
  a later invocation converges to the same groups and obligations;
* a Telegram call is made only after both the parent and its chunk have been
  claimed and their ``sending`` state has been CASed.  A stale worker cannot
  complete a newer claim.

The existing migration predates grouped messages and has no separate
obligation table.  The small additive ``runtime_notification_*`` tables below
retain the exact four-part obligation key and the logical retry state while
the canonical parent/chunk rows remain the source of delivery claims.  This
keeps old dashboards and cleanup code able to see ordinary ``deliveries``
without encoding a group of obligations into a lossy string.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import html
import ipaddress
import json
import sqlite3
from typing import cast
from urllib.parse import urlsplit

from .db import ClaimToken, Database, utc_now, utc_timestamp
from .delivery import (
    DEFAULT_SEND_DEADLINE_SECONDS,
    DEFAULT_SEND_GRACE_SECONDS,
    FAILURE_ALERT_DELAYS_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    RETRY_DELAYS_SECONDS,
    TelegramFailureClass,
)
from .models import MediaType, NotificationClass, RequestMode
from .planner import (
    CanonicalUnit,
    Obligation,
    ObligationKey,
    ObligationState,
    NotificationGroup,
    OracleResult,
    Subscription,
    assemble_completed_seasons,
    build_obligations,
    evaluate_oracle,
    plan_groups,
    render_group_chunks,
)


UTC = timezone.utc
MAX_UNITS = 5_000
MAX_GROUPS = 5_000
MAX_RENDER_BYTES = 4_096
MAX_ERROR_BYTES = 500
DEFAULT_LEASE_SECONDS = 300


class NotificationRuntimeError(RuntimeError):
    """Base class for durable notification adapter errors."""


class NotificationClaimError(NotificationRuntimeError):
    """A parent/chunk claim was fenced or could not be acquired."""


class NotificationNotDue(NotificationRuntimeError):
    """A caller attempted to send before the fixed group window elapsed."""


class DeliveryOutcome(str, Enum):
    SENT = "sent"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DELIVERY_BLOCKED = "delivery_blocked"
    GLOBAL_CIRCUIT = "global_circuit"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class RuntimePlan:
    """Result of one durable planning transaction."""

    groups: tuple[NotificationGroup, ...]
    obligations: tuple[Obligation, ...]
    oracle: OracleResult
    delivery_ids: tuple[int, ...] = ()

    @property
    def accounting(self) -> OracleResult:
        return self.oracle


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    """Bounded result of one parent delivery attempt."""

    delivery_id: int
    outcome: DeliveryOutcome
    chunks_sent: int = 0
    message_ids: tuple[int, ...] = ()
    error_class: str | None = None
    retry_due_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RuntimeDelivery:
    """A read-only delivery view with logical retry state."""

    delivery_id: int
    group_key: str
    destination: str
    chat_id: int
    notification_class: str
    status: str
    logical_status: str
    attempts: int
    retry_due_at: datetime | None
    claim_expires_at: datetime | None
    chunk_count: int
    possible_duplicate: bool


@dataclass(frozen=True, slots=True)
class FailureInfo:
    classification: TelegramFailureClass
    error: str
    retry_after: int | None = None
    transmitted: bool = False


def _now(clock: Callable[[], datetime], explicit: datetime | None = None) -> datetime:
    value = explicit if explicit is not None else clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise NotificationRuntimeError("notification clock must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return utc_timestamp(value)


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
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


def _safe_error(value: object) -> str:
    text = str(value).replace("\x00", " ").replace("\n", " ").strip()
    if not text:
        return "notification delivery failed"
    raw = text.encode("utf-8", "replace")
    if len(raw) <= MAX_ERROR_BYTES:
        return text
    return raw[:MAX_ERROR_BYTES].decode("utf-8", "ignore")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _key_json(key: ObligationKey) -> str:
    """Stable storage form for ``(logical unit,destination,class,generation)``."""

    return _json([key[0], key[1], key[2], key[3]])


def _key_from_json(value: object) -> ObligationKey:
    try:
        raw = json.loads(str(value)) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise NotificationRuntimeError("stored obligation key is invalid") from exc
    if not isinstance(raw, list) or len(raw) != 4:
        raise NotificationRuntimeError("stored obligation key is invalid")
    if (
        not isinstance(raw[0], str)
        or not isinstance(raw[1], str)
        or not isinstance(raw[2], str)
    ):
        raise NotificationRuntimeError("stored obligation key is invalid")
    generation = raw[3]
    if generation is not None and (
        isinstance(generation, bool) or not isinstance(generation, (str, int))
    ):
        raise NotificationRuntimeError("stored obligation generation is invalid")
    return raw[0], raw[1], raw[2], generation


def _chat_id(value: object) -> int:
    if isinstance(value, bool):
        raise NotificationRuntimeError("Telegram chat id cannot be boolean")
    if isinstance(value, int):
        if value == 0:
            raise NotificationRuntimeError("Telegram chat id cannot be zero")
        return value
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise NotificationRuntimeError("Telegram chat id is not numeric") from exc
        if parsed == 0:
            raise NotificationRuntimeError("Telegram chat id cannot be zero")
        return parsed
    raise NotificationRuntimeError("Telegram chat id is invalid")


def _safe_url(value: object) -> str | None:
    """Allow only credential-free, public HTTP(S) Plex links."""

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw.encode("utf-8")) > 2_048:
        return None
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        private = False
        if host:
            try:
                address = ipaddress.ip_address(host)
                private = bool(
                    address.is_private
                    or address.is_loopback
                    or address.is_link_local
                    or address.is_unspecified
                )
            except ValueError:
                private = host.lower() in {"localhost"} or host.lower().endswith(
                    ".local"
                )
        if parsed.scheme.lower() not in {"http", "https"} or not host or private:
            return None
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        lowered = raw.lower()
        if any(
            marker in lowered
            for marker in ("token=", "access_token", "bot_token", "%3f", "%23")
        ):
            return None
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
            return None
        _ = parsed.port
    except ValueError:
        return None
    return html.escape(raw, quote=True)


def _unit_record(unit: CanonicalUnit) -> dict[str, object]:
    return {
        "unit_id": unit.unit_id,
        "media_type": (
            unit.media_type.value
            if isinstance(unit.media_type, MediaType)
            else str(unit.media_type)
        ),
        "visible_in_plex_at": _timestamp(unit.visible_in_plex_at),
        "title": unit.title,
        "year": unit.year,
        "show_identity": unit.show_identity,
        "season_number": unit.season_number,
        "episode_number": unit.episode_number,
        "quality": unit.quality,
        "plex_url": unit.plex_url,
        "logical_identity": unit.logical_identity,
        "mode": unit.mode.value if isinstance(unit.mode, RequestMode) else unit.mode,
        "provider_identity": unit.provider_identity,
        "server_uuid": unit.server_uuid,
        "library_uuid": unit.library_uuid,
        "snapshot_verified": unit.snapshot_verified,
        "playable": unit.playable,
        "library_priority": unit.library_priority,
        "resolution": unit.resolution,
        "bitrate": unit.bitrate,
        "tombstone_generation": unit.tombstone_generation,
    }


def _group_payload(
    group: NotificationGroup,
    units: Mapping[str, CanonicalUnit],
    chunks: Sequence[str] = (),
) -> dict[str, object]:
    selected = [units[key] for key in group.unit_keys if key in units]
    return {
        "unit_keys": list(group.unit_keys),
        "obligation_keys": [_key_json(key) for key in group.obligation_keys],
        "requester_detail": group.requester_detail,
        "completion_ready": group.completion_ready,
        "source_group_keys": list(group.source_group_keys),
        "unit_records": [_unit_record(unit) for unit in selected],
        "chunks": list(chunks),
    }


def _group_key(group: NotificationGroup) -> str:
    return hashlib.sha256(
        _json(
            {
                "destination": group.destination,
                "class": _class(group.notification_class),
                "show": group.show_identity,
                "season": group.season_number,
                # The pure planner numbers its first generation at zero;
                # the checked-in SQLite scope trigger intentionally starts at
                # one.  Normalize only the durable identity so a restart sees
                # the same key for either representation.
                "generation": group.window_generation,
            }
        ).encode("utf-8")
    ).hexdigest()


def _class(value: NotificationClass | str) -> str:
    return value.value if isinstance(value, NotificationClass) else str(value)


def _unit_from_record(value: object) -> CanonicalUnit:
    return CanonicalUnit.from_record(cast(Mapping[str, object], value))


def _failure_from_exception(error: BaseException, *, sending: bool) -> FailureInfo:
    """Normalize Telegram adapters and small test doubles to one taxonomy."""

    # Import lazily: the runtime seam is also usable with a Hermes bridge and
    # must not require the concrete HTTP client at import time.
    error_class = getattr(error, "error_class", getattr(error, "classification", None))
    raw = getattr(error_class, "value", error_class)
    retry_after = getattr(
        error, "retry_after", getattr(error, "retry_after_seconds", None)
    )
    retry_after_int = (
        retry_after
        if isinstance(retry_after, int) and not isinstance(retry_after, bool)
        else None
    )
    transmitted = bool(getattr(error, "transmitted", False))
    text = _safe_error(error)
    if isinstance(raw, str):
        lowered = raw.lower()
        if lowered in {
            "terminal_recipient",
            "destination_blocked",
            "recipient_blocked",
            "bot_blocked",
            "bot_kicked",
            "chat_not_found",
            "user_deactivated",
        }:
            return FailureInfo(
                TelegramFailureClass.DESTINATION_BLOCKED, text, transmitted=transmitted
            )
        if lowered in {"authentication", "auth", "unauthorized", "invalid_token"}:
            return FailureInfo(
                TelegramFailureClass.AUTHENTICATION, text, transmitted=transmitted
            )
        if lowered in {"rate_limited", "too_many_requests"}:
            return FailureInfo(
                TelegramFailureClass.RATE_LIMITED,
                text,
                retry_after=retry_after_int,
                transmitted=transmitted,
            )
        if lowered in {"retryable", "pre_transmission", "transport", "timeout"}:
            return FailureInfo(
                TelegramFailureClass.PRE_TRANSMISSION,
                text,
                retry_after=retry_after_int,
                transmitted=transmitted,
            )
        if lowered in {"ambiguous", "unknown"}:
            return FailureInfo(
                TelegramFailureClass.AMBIGUOUS,
                text,
                retry_after=retry_after_int,
                transmitted=True,
            )
        if lowered in {"application", "invalid"}:
            return FailureInfo(
                TelegramFailureClass.APPLICATION, text, transmitted=transmitted
            )
    # A timeout after the CASed sending transition is ambiguous.  A failure
    # before that transition is safe to retry at the first backoff interval.
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return FailureInfo(
            TelegramFailureClass.AMBIGUOUS
            if sending
            else TelegramFailureClass.PRE_TRANSMISSION,
            text,
            transmitted=sending or transmitted,
        )
    return FailureInfo(
        TelegramFailureClass.AMBIGUOUS
        if sending and transmitted
        else TelegramFailureClass.APPLICATION,
        text,
        transmitted=transmitted,
    )


def _failure_from_result(result: object) -> FailureInfo | None:
    if result is None:
        return None
    ok = getattr(result, "ok", None)
    if isinstance(result, Mapping):
        ok = result.get("ok", ok)
    if ok is not False:
        return None
    error_class = getattr(result, "error_class", None)
    if isinstance(result, Mapping):
        error_class = result.get(
            "error_class", result.get("classification", error_class)
        )
    raw = getattr(error_class, "value", error_class)
    retry_after = getattr(result, "retry_after", None)
    if isinstance(result, Mapping):
        retry_after = result.get("retry_after", retry_after)
    error = getattr(result, "description", "Telegram send failed")
    if isinstance(result, Mapping):
        error = result.get("description", result.get("error", error))
    fake = RuntimeError(str(error))
    setattr(fake, "error_class", raw)  # type: ignore[attr-defined]
    setattr(fake, "retry_after", retry_after)  # type: ignore[attr-defined]
    return _failure_from_exception(fake, sending=True)


class DurableNotificationRepository:
    """SQLite repository for planner output and the Telegram outbox."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] = utc_now,
        message_limit: int = MAX_RENDER_BYTES,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        send_deadline_seconds: int = DEFAULT_SEND_DEADLINE_SECONDS,
        send_grace_seconds: int = DEFAULT_SEND_GRACE_SECONDS,
    ) -> None:
        if (
            isinstance(message_limit, bool)
            or not isinstance(message_limit, int)
            or message_limit <= 0
            or message_limit > MAX_RENDER_BYTES
        ):
            raise ValueError("message_limit must be between 1 and 4096")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
            or lease_seconds > 300
        ):
            raise ValueError("lease_seconds must be between 1 and 300")
        if (
            isinstance(send_deadline_seconds, bool)
            or not isinstance(send_deadline_seconds, int)
            or send_deadline_seconds <= 0
        ):
            raise ValueError("send_deadline_seconds must be positive")
        if (
            isinstance(send_grace_seconds, bool)
            or not isinstance(send_grace_seconds, int)
            or send_grace_seconds < 0
        ):
            raise ValueError("send_grace_seconds must be non-negative")
        self.database = database
        self.clock = clock
        self.message_limit = message_limit
        self.lease_seconds = lease_seconds
        self.send_deadline_seconds = send_deadline_seconds
        self.send_grace_seconds = send_grace_seconds
        self._ensure_runtime_tables()

    def _ensure_runtime_tables(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_notification_obligations (
                    obligation_key TEXT PRIMARY KEY,
                    logical_unit_key TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    notification_class TEXT NOT NULL,
                    generation TEXT,
                    state TEXT NOT NULL,
                    paired_obligation_key TEXT,
                    group_key TEXT,
                    delivery_id INTEGER REFERENCES deliveries(id) ON DELETE SET NULL,
                    membership_status TEXT NOT NULL DEFAULT 'eligible',
                    reason TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_notification_delivery (
                    delivery_id INTEGER PRIMARY KEY REFERENCES deliveries(id) ON DELETE CASCADE,
                    group_key TEXT NOT NULL,
                    logical_state TEXT NOT NULL DEFAULT 'pending',
                    pretransmission_failures INTEGER NOT NULL DEFAULT 0,
                    recovery_attempted INTEGER NOT NULL DEFAULT 0,
                    possible_duplicate INTEGER NOT NULL DEFAULT 0,
                    retry_due_at TEXT,
                    circuit_until TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_notification_circuit (
                    circuit_name TEXT PRIMARY KEY,
                    open_until TEXT,
                    reason TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_notification_due "
                "ON runtime_notification_delivery(logical_state, retry_due_at, delivery_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_notification_group "
                "ON runtime_notification_obligations(group_key, state)"
            )

    def _current(self, value: datetime | None = None) -> datetime:
        current = _now(self.clock, value)
        # The database guard is durable, unlike a process-local monotonic
        # timestamp.  It fails closed on rollback and is a no-op on old test
        # databases that have not yet applied the hardening migration.
        self.database.observe_clock(current)
        return current

    @staticmethod
    def _existing_group_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                "SELECT * FROM notification_groups WHERE status IN ('open','ready') ORDER BY first_seen_at,id"
            ).fetchall()
        )

    @staticmethod
    def _group_from_row(row: Mapping[str, object]) -> NotificationGroup | None:
        status = str(row.get("status", "open"))
        if status not in {"open", "ready"}:
            return None
        raw_payload = row.get("payload_json")
        try:
            payload = (
                json.loads(raw_payload)
                if isinstance(raw_payload, str) and raw_payload
                else {}
            )
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, Mapping):
            payload = {}
        raw_keys = payload.get("obligation_keys", ())
        keys: list[ObligationKey] = []
        if isinstance(raw_keys, Sequence) and not isinstance(
            raw_keys, (str, bytes, bytearray)
        ):
            for item in raw_keys:
                try:
                    keys.append(_key_from_json(item))
                except NotificationRuntimeError:
                    continue
        unit_keys = payload.get("unit_keys", ())
        units = (
            tuple(str(item) for item in unit_keys)
            if isinstance(unit_keys, Sequence)
            and not isinstance(unit_keys, (str, bytes, bytearray))
            else ()
        )
        source = payload.get("source_group_keys", ())
        source_keys = (
            tuple(str(item) for item in source)
            if isinstance(source, Sequence)
            and not isinstance(source, (str, bytes, bytearray))
            else ()
        )
        first = _parse_time(row.get("first_seen_at"))
        due = _parse_time(row.get("due_at"))
        if first is None or due is None:
            return None
        try:
            return NotificationGroup(
                destination=str(row.get("destination", "")),
                notification_class=str(
                    row.get("notification_class", NotificationClass.REQUESTER.value)
                ),
                show_identity=cast(str | None, row.get("canonical_show_identity")),
                season_number=int(cast(str | int, row["season_number"]))
                if row.get("season_number") is not None
                else None,
                window_generation=max(
                    0,
                    int(cast(str | int, row.get("window_generation", 1))) - 1,
                ),
                first_seen_at=first,
                due_at=due,
                unit_keys=units,
                obligation_keys=tuple(keys),
                state="ready" if status == "ready" else "open",
                requester_detail=bool(payload.get("requester_detail", False)),
                completion_ready=bool(payload.get("completion_ready", False)),
                source_group_keys=source_keys,
            )
        except (TypeError, ValueError):
            return None

    def _load_groups(
        self, connection: sqlite3.Connection
    ) -> tuple[NotificationGroup, ...]:
        groups: list[NotificationGroup] = []
        for row in self._existing_group_rows(connection):
            group = self._group_from_row(dict(row))
            if group is not None:
                groups.append(group)
        return tuple(groups)

    @staticmethod
    def _load_unit_map(
        groups: Iterable[NotificationGroup], connection: sqlite3.Connection
    ) -> dict[str, CanonicalUnit]:
        result: dict[str, CanonicalUnit] = {}
        for group in groups:
            row = connection.execute(
                "SELECT payload_json FROM notification_groups WHERE destination=? AND notification_class=? AND COALESCE(canonical_show_identity,'')=COALESCE(?, '') AND COALESCE(season_number,-1)=COALESCE(?,-1) AND window_generation=? AND status IN ('open','ready') ORDER BY id DESC LIMIT 1",
                (
                    group.destination,
                    _class(group.notification_class),
                    group.show_identity,
                    group.season_number,
                    group.window_generation + 1,
                ),
            ).fetchone()
            if row is None:
                continue
            try:
                payload = json.loads(str(row[0]))
            except (json.JSONDecodeError, TypeError):
                continue
            records = (
                payload.get("unit_records", ()) if isinstance(payload, Mapping) else ()
            )
            if not isinstance(records, Sequence) or isinstance(
                records, (str, bytes, bytearray)
            ):
                continue
            for record in records:
                if isinstance(record, Mapping):
                    try:
                        unit = _unit_from_record(record)
                    except Exception:
                        continue
                    result[unit.unit_id] = unit
        return result

    def _persist_group(
        self,
        connection: sqlite3.Connection,
        group: NotificationGroup,
        units: Mapping[str, CanonicalUnit],
        *,
        now: datetime,
    ) -> str:
        key = _group_key(group)
        payload = _group_payload(group, units)
        first = _timestamp(group.first_seen_at)
        due = _timestamp(group.due_at)
        destination = group.destination
        chat_id = _chat_id(destination)
        durable_generation = group.window_generation + 1
        row = connection.execute(
            "SELECT id,status,version,payload_json FROM notification_groups WHERE group_key=?",
            (key,),
        ).fetchone()
        if row is None:
            # The migration's open-generation index makes the base unique.  A
            # concurrent planner may have won the insert with another key;
            # converge on that row rather than manufacturing a duplicate.
            try:
                connection.execute(
                    """
                    INSERT INTO notification_groups(
                        group_key,destination,chat_id,notification_class,
                        canonical_show_identity,season_number,window_generation,
                        first_seen_at,due_at,status,payload_json,version,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,'open',?,0,?,?)
                    """,
                    (
                        key,
                        destination,
                        chat_id,
                        _class(group.notification_class),
                        group.show_identity,
                        group.season_number,
                        durable_generation,
                        first,
                        due,
                        _json(payload),
                        _timestamp(now),
                        _timestamp(now),
                    ),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT id,status,version FROM notification_groups WHERE destination=? AND chat_id=? AND notification_class=? AND COALESCE(canonical_show_identity,'')=COALESCE(?, '') AND COALESCE(season_number,-1)=COALESCE(?,-1) AND status IN ('open','ready') ORDER BY window_generation DESC,id DESC LIMIT 1",
                    (
                        destination,
                        chat_id,
                        _class(group.notification_class),
                        group.show_identity,
                        group.season_number,
                    ),
                ).fetchone()
                if row is None:
                    raise
                key = str(
                    connection.execute(
                        "SELECT group_key FROM notification_groups WHERE id=?",
                        (int(row[0]),),
                    ).fetchone()[0]
                )
        else:
            row_status = str(row[1])
            if row_status == "open":
                connection.execute(
                    "UPDATE notification_groups SET first_seen_at=?,due_at=?,payload_json=?,version=version+1,updated_at=? WHERE group_key=? AND version=? AND status='open'",
                    (first, due, _json(payload), _timestamp(now), key, int(row[2])),
                )
        return key

    def _persist_obligation(
        self,
        connection: sqlite3.Connection,
        obligation: Obligation,
        *,
        group_key: str | None,
        now: datetime,
    ) -> None:
        key = _key_json(obligation.key)
        generation = obligation.accounting_generation
        generation_text = None if generation is None else str(generation)
        existing = connection.execute(
            "SELECT state,version FROM runtime_notification_obligations WHERE obligation_key=?",
            (key,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO runtime_notification_obligations(
                    obligation_key,logical_unit_key,destination,notification_class,
                    generation,state,paired_obligation_key,group_key,membership_status,
                    reason,version,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key,
                    obligation.unit_key,
                    obligation.destination,
                    _class(obligation.notification_class),
                    generation_text,
                    obligation.state,
                    _key_json(obligation.paired_obligation)
                    if obligation.paired_obligation
                    else None,
                    group_key,
                    "suppressed"
                    if obligation.state == ObligationState.SUPPRESSED.value
                    else "eligible",
                    obligation.reason,
                    0,
                    _timestamp(now),
                    _timestamp(now),
                ),
            )
            return
        current_state = str(existing[0])
        # Terminal outcomes and a manually resolved unknown are immutable from
        # the planner.  A planner retry must never resurrect a sent message.
        terminal = {
            "sent",
            "assumed_sent",
            "failed",
            "unknown",
            "abandoned",
            "delivery_blocked",
            "canceled",
            "superseded",
            "quarantined",
        }
        if (
            current_state in terminal
            and current_state != ObligationState.SUPPRESSED.value
        ):
            return
        connection.execute(
            "UPDATE runtime_notification_obligations SET group_key=COALESCE(group_key,?),paired_obligation_key=COALESCE(paired_obligation_key,?),reason=COALESCE(reason,?),version=version+1,updated_at=? WHERE obligation_key=? AND version=?",
            (
                group_key,
                _key_json(obligation.paired_obligation)
                if obligation.paired_obligation
                else None,
                obligation.reason,
                _timestamp(now),
                key,
                int(existing[1]),
            ),
        )

    def _runtime_obligation_rows(
        self, connection: sqlite3.Connection, keys: Iterable[str] | None = None
    ) -> list[sqlite3.Row]:
        if keys is None:
            return list(
                connection.execute(
                    "SELECT * FROM runtime_notification_obligations ORDER BY logical_unit_key,destination,notification_class,generation,obligation_key"
                ).fetchall()
            )
        selected = tuple(keys)
        if not selected:
            return []
        placeholders = ",".join("?" for _ in selected)
        return list(
            connection.execute(
                f"SELECT * FROM runtime_notification_obligations WHERE obligation_key IN ({placeholders}) ORDER BY obligation_key",
                selected,
            ).fetchall()
        )

    def plan(
        self,
        units: Iterable[CanonicalUnit | object],
        *,
        subscriptions: Iterable[Subscription | object] = (),
        admin_destinations: Iterable[str | int] = (),
        activation_started_at: datetime | None = None,
        historical_unit_keys: Iterable[str] = (),
        now: datetime | None = None,
        scan_complete: bool = False,
        full_sweep_complete: bool = False,
        scans_fresh: bool = False,
        materialize_due: bool = True,
    ) -> RuntimePlan:
        """Plan and persist obligations/groups in one restart-safe transaction.

        ``activation_started_at`` is deliberately strict: units at or before
        that timestamp are historical and cannot produce an admin obligation.
        The caller may use a separate, explicit requester baseline pass when it
        wants to fulfill old subscriptions.
        """

        current = self._current(now)
        historical = {str(key) for key in historical_unit_keys}
        normalized: list[CanonicalUnit] = []
        for value in units:
            unit = (
                value
                if isinstance(value, CanonicalUnit)
                else CanonicalUnit.from_record(value)
            )
            if unit.unit_id in historical:
                continue
            if unit.visible_in_plex_at > current:
                # A future/airing item is not Plex-visible yet.  It can be
                # planned by the next reconciliation after Plex exposes it.
                continue
            if activation_started_at is not None:
                activation = (
                    activation_started_at.astimezone(UTC)
                    if activation_started_at.tzinfo is not None
                    and activation_started_at.utcoffset() is not None
                    else None
                )
                if activation is None:
                    raise NotificationRuntimeError(
                        "activation_started_at must be timezone-aware"
                    )
                if unit.visible_in_plex_at <= activation:
                    continue
            normalized.append(unit)
        if len(normalized) > MAX_UNITS:
            raise NotificationRuntimeError("notification planning unit bound exceeded")
        normalized.sort(key=lambda item: (item.visible_in_plex_at, item.unit_id))
        normalized_map = {unit.unit_id: unit for unit in normalized}
        obligations = build_obligations(
            normalized,
            subscriptions=subscriptions,
            admin_destinations=admin_destinations,
        )
        with self.database.transaction() as connection:
            existing_rows = self._runtime_obligation_rows(connection)
            existing_keys = {str(row["obligation_key"]) for row in existing_rows}
            # Do not ask plan_groups to reopen already materialized groups.  It
            # receives only unseen obligations while its existing open groups
            # provide the five-minute window anchor for genuinely new rows.
            unseen = tuple(
                obligation
                for obligation in obligations
                if _key_json(obligation.key) not in existing_keys
            )
            existing_groups = self._load_groups(connection)
            groups_for_planner = tuple(
                group for group in existing_groups if group.state == "open"
            )
            unseen_units = {obligation.unit_key for obligation in unseen}
            unseen_unit_records = tuple(
                unit for unit in normalized if unit.unit_id in unseen_units
            )
            planned_groups = plan_groups(
                unseen_unit_records, unseen, existing_groups=groups_for_planner
            )
            # Include persisted groups in the return value.  Existing ready
            # groups remain immutable and are not re-planned.
            merged: dict[str, NotificationGroup] = {}
            for group in existing_groups:
                merged[_group_key(group)] = group
            for group in planned_groups:
                merged[_group_key(group)] = group
            final_groups = tuple(
                sorted(
                    merged.values(),
                    key=lambda group: (
                        group.first_seen_at,
                        group.destination,
                        group.window_generation,
                        group.idempotency_key,
                    ),
                )
            )
            if len(final_groups) > MAX_GROUPS:
                raise NotificationRuntimeError("notification group bound exceeded")
            group_for_obligation: dict[ObligationKey, str] = {}
            for group in final_groups:
                for key in group.obligation_keys:
                    group_for_obligation[key] = _group_key(group)
            for group in planned_groups:
                group_key = self._persist_group(
                    connection, group, normalized_map, now=current
                )
                for key in group.obligation_keys:
                    group_for_obligation[key] = group_key
            for obligation in obligations:
                self._persist_obligation(
                    connection,
                    obligation,
                    group_key=group_for_obligation.get(obligation.key),
                    now=current,
                )
        delivery_ids: tuple[int, ...] = ()
        if materialize_due:
            delivery_ids = self.materialize_due(now=current)
        oracle = self.oracle(
            scan_complete=scan_complete,
            full_sweep_complete=full_sweep_complete,
            scans_fresh=scans_fresh,
            keys=(_key_json(obligation.key) for obligation in obligations),
        )
        return RuntimePlan(
            groups=final_groups,
            obligations=obligations,
            oracle=oracle,
            delivery_ids=delivery_ids,
        )

    plan_notifications = plan
    persist_plan = plan

    def materialize_due(
        self, *, now: datetime | None = None, limit: int = MAX_GROUPS
    ) -> tuple[int, ...]:
        """Close due windows, render bounded chunks, and create outbox rows."""

        current = self._current(now)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("materialize limit must be positive")
        created: list[int] = []
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM notification_groups WHERE status='open' AND due_at <= ? ORDER BY due_at,id LIMIT ?",
                (_timestamp(current), limit),
            ).fetchall()
            for row in rows:
                group = self._group_from_row(dict(row))
                if group is None:
                    continue
                payload = self._payload_for_group(row)
                units = self._units_for_payload(payload)
                try:
                    chunks = render_group_chunks(
                        group, units, max_bytes=self.message_limit
                    )
                except Exception as exc:
                    self._transition_group(
                        connection,
                        int(row["id"]),
                        str(row["status"]),
                        "failed",
                        current,
                        reason=_safe_error(exc),
                    )
                    self._set_group_obligations(
                        connection,
                        _group_key(group),
                        "failed",
                        membership="failed",
                        now=current,
                    )
                    continue
                merged_payload = dict(payload)
                merged_payload["chunks"] = list(chunks)
                connection.execute(
                    "UPDATE notification_groups SET status='ready',payload_json=?,version=version+1,updated_at=? WHERE id=? AND status='open' AND version=?",
                    (
                        _json(merged_payload),
                        _timestamp(current),
                        int(row["id"]),
                        int(row["version"]),
                    ),
                )
            # Season-completion groups that close in one five-minute window are
            # assembled into one destination-level envelope.  Membership keys
            # remain in their source groups and are merely moved to the
            # synthetic delivery below.
            ready_rows = connection.execute(
                "SELECT * FROM notification_groups WHERE status='ready' AND due_at <= ? ORDER BY due_at,id LIMIT ?",
                (_timestamp(current), limit),
            ).fetchall()
            ready_groups = tuple(
                group
                for row in ready_rows
                if (group := self._group_from_row(dict(row))) is not None
            )
            assembled = assemble_completed_seasons(ready_groups)
            planner_group_map = {group.idempotency_key: group for group in ready_groups}
            for group in assembled:
                if group.source_group_keys:
                    # ``assemble_completed_seasons`` deliberately keeps the
                    # source planner keys rather than database row IDs.  Make
                    # a durable season=None envelope and carry the source
                    # units into its payload; source groups remain immutable
                    # accounting envelopes and are not sent independently.
                    source_units: dict[str, CanonicalUnit] = {}
                    for source_key in group.source_group_keys:
                        source = planner_group_map.get(source_key)
                        if source is None:
                            continue
                        source_row = connection.execute(
                            "SELECT payload_json FROM notification_groups WHERE destination=? AND notification_class=? AND COALESCE(canonical_show_identity,'')=COALESCE(?, '') AND COALESCE(season_number,-1)=COALESCE(?,-1) AND window_generation=? AND status='ready' ORDER BY id DESC LIMIT 1",
                            (
                                source.destination,
                                _class(source.notification_class),
                                source.show_identity,
                                source.season_number,
                                source.window_generation + 1,
                            ),
                        ).fetchone()
                        if source_row is not None:
                            source_units.update(
                                {
                                    unit.unit_id: unit
                                    for unit in self._units_for_payload(
                                        self._payload_for_group(source_row)
                                    )
                                }
                            )
                    synthetic_key = _group_key(group)
                    existing_synthetic = connection.execute(
                        "SELECT id FROM notification_groups WHERE group_key=?",
                        (synthetic_key,),
                    ).fetchone()
                    if existing_synthetic is None:
                        payload = _group_payload(group, source_units)
                        chunks = render_group_chunks(
                            group,
                            tuple(source_units.values()),
                            max_bytes=self.message_limit,
                        )
                        payload["chunks"] = list(chunks)
                        connection.execute(
                            "INSERT INTO notification_groups(group_key,destination,chat_id,notification_class,canonical_show_identity,season_number,window_generation,first_seen_at,due_at,status,payload_json,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?, ?,?,'ready',?,0,?,?)",
                            (
                                synthetic_key,
                                group.destination,
                                _chat_id(group.destination),
                                _class(group.notification_class),
                                group.show_identity,
                                None,
                                group.window_generation + 1,
                                _timestamp(group.first_seen_at),
                                _timestamp(group.due_at),
                                _json(payload),
                                _timestamp(current),
                                _timestamp(current),
                            ),
                        )
                self._materialize_group_delivery(connection, group, current, created)
        return tuple(created)

    def _payload_for_group(self, row: Mapping[str, object]) -> dict[str, object]:
        raw_payload: object = "{}"
        try:
            raw_payload = row["payload_json"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            pass
        try:
            raw = json.loads(str(raw_payload))
        except (TypeError, json.JSONDecodeError):
            raw = {}
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _units_for_payload(
        self, payload: Mapping[str, object]
    ) -> tuple[CanonicalUnit, ...]:
        records = payload.get("unit_records", ())
        if not isinstance(records, Sequence) or isinstance(
            records, (str, bytes, bytearray)
        ):
            return ()
        result: list[CanonicalUnit] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            try:
                result.append(_unit_from_record(record))
            except Exception:
                continue
        return tuple(result)

    def _materialize_group_delivery(
        self,
        connection: sqlite3.Connection,
        group: NotificationGroup,
        now: datetime,
        created: list[int],
    ) -> None:
        if not group.obligation_keys:
            return
        group_key = _group_key(group)
        payload_row = connection.execute(
            "SELECT payload_json FROM notification_groups WHERE group_key=?",
            (group_key,),
        ).fetchone()
        payload: dict[str, object] = {}
        if payload_row is not None:
            payload = self._payload_for_group(payload_row)
        units = self._units_for_payload(payload)
        chunks_raw = payload.get("chunks", ())
        chunks = (
            tuple(str(value) for value in chunks_raw)
            if isinstance(chunks_raw, Sequence)
            and not isinstance(chunks_raw, (str, bytes, bytearray))
            else ()
        )
        if not chunks:
            try:
                chunks = render_group_chunks(group, units, max_bytes=self.message_limit)
            except Exception:
                self._set_group_obligations(
                    connection, group_key, "failed", membership="failed", now=now
                )
                return
        active_keys = [
            key
            for key in group.obligation_keys
            if self._obligation_state(connection, key)
            not in {
                ObligationState.SUPPRESSED.value,
                "sent",
                "assumed_sent",
                "failed",
                "abandoned",
                "delivery_blocked",
                "canceled",
                "superseded",
                "quarantined",
            }
        ]
        if not active_keys:
            return
        anchor = sorted(
            active_keys, key=lambda key: (key[0], key[1], key[2], str(key[3]))
        )[0]
        anchor_json = _key_json(anchor)
        idempotency = hashlib.sha256(
            ("runtime-group:" + group_key).encode()
        ).hexdigest()
        existing = connection.execute(
            "SELECT id,status FROM deliveries WHERE idempotency_key=?", (idempotency,)
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO deliveries(
                    group_id,destination,chat_id,notification_class,event_key,
                    subscription_generation,idempotency_key,status,obligation_key,
                    chunk_ordinal,chunk_count,version,created_at,updated_at
                ) VALUES((SELECT id FROM notification_groups WHERE group_key=?),?,?,?,?,?,?,'pending',?,1,?,0,?,?)
                """,
                (
                    group_key,
                    group.destination,
                    _chat_id(group.destination),
                    _class(group.notification_class),
                    group_key,
                    anchor[3] if isinstance(anchor[3], int) else None,
                    idempotency,
                    anchor_json,
                    len(chunks),
                    _timestamp(now),
                    _timestamp(now),
                ),
            )
            delivery_row = connection.execute(
                "SELECT id FROM deliveries WHERE idempotency_key=?", (idempotency,)
            ).fetchone()
            if delivery_row is None:
                raise NotificationRuntimeError("delivery row was not persisted")
            delivery_id = int(delivery_row[0])
            connection.execute(
                "INSERT INTO runtime_notification_delivery(delivery_id,group_key,logical_state,updated_at) VALUES(?,?, 'pending',?)",
                (delivery_id, group_key, _timestamp(now)),
            )
            for ordinal, text in enumerate(chunks, start=1):
                stable = hashlib.sha256(
                    f"{idempotency}:{ordinal}:{text}".encode()
                ).hexdigest()
                connection.execute(
                    "INSERT INTO delivery_chunks(delivery_id,ordinal,chunk_count,stable_key,payload_json,status,version,created_at,updated_at) VALUES(?,?,?,?,?,'pending',0,?,?)",
                    (
                        delivery_id,
                        ordinal,
                        len(chunks),
                        stable,
                        _json({"text": text}),
                        _timestamp(now),
                        _timestamp(now),
                    ),
                )
            created.append(delivery_id)
        else:
            delivery_id = int(existing[0])
            connection.execute(
                "UPDATE runtime_notification_delivery SET group_key=?,updated_at=? WHERE delivery_id=?",
                (group_key, _timestamp(now), delivery_id),
            )
        for key in group.obligation_keys:
            key_json = _key_json(key)
            connection.execute(
                "UPDATE runtime_notification_obligations SET delivery_id=COALESCE(delivery_id,?),group_key=COALESCE(group_key,?),updated_at=?,version=version+1 WHERE obligation_key=?",
                (delivery_id, group_key, _timestamp(now), key_json),
            )

    def _obligation_state(
        self, connection: sqlite3.Connection, key: ObligationKey
    ) -> str | None:
        row = connection.execute(
            "SELECT state FROM runtime_notification_obligations WHERE obligation_key=?",
            (_key_json(key),),
        ).fetchone()
        return None if row is None else str(row[0])

    def _set_group_obligations(
        self,
        connection: sqlite3.Connection,
        group_key: str,
        state: str,
        *,
        membership: str,
        now: datetime,
    ) -> None:
        connection.execute(
            "UPDATE runtime_notification_obligations SET state=?,membership_status=?,updated_at=?,version=version+1 WHERE group_key=? AND state NOT IN ('sent','assumed_sent','superseded','canceled','quarantined')",
            (state, membership, _timestamp(now), group_key),
        )

    @staticmethod
    def _transition_group(
        connection: sqlite3.Connection,
        row_id: int,
        old: str,
        new: str,
        now: datetime,
        *,
        reason: str | None = None,
    ) -> None:
        if old == new:
            return
        allowed: dict[str, tuple[str, ...]] = {
            "open": ("ready", "claimed", "canceled", "superseded", "blocked"),
            "ready": ("claimed", "canceled", "superseded", "blocked"),
            "claimed": (
                "sending",
                "retry_wait",
                "failed",
                "unknown",
                "abandoned",
                "canceled",
            ),
            "sending": ("sent", "unknown", "failed"),
            "retry_wait": ("open", "ready", "claimed", "failed", "abandoned"),
            "failed": ("open", "ready", "abandoned"),
            "unknown": ("sent", "assumed_sent", "open", "superseded"),
            "sent": ("closed",),
            "assumed_sent": ("closed",),
        }
        if new not in allowed.get(old, ()):
            # The canonical triggers are authoritative.  A stale row is left
            # alone instead of weakening them with a direct trigger bypass.
            return
        connection.execute(
            "UPDATE notification_groups SET status=?,version=version+1,updated_at=? WHERE id=? AND status=?",
            (new, _timestamp(now), row_id, old),
        )

    def _claim_parent_and_chunk(
        self, delivery_id: int, *, worker_id: str, leader_epoch: int, now: datetime
    ) -> tuple[ClaimToken, ClaimToken, sqlite3.Row] | None:
        parent = self.database.claim_delivery(
            delivery_id,
            lease_seconds=self.lease_seconds,
            worker_id=worker_id,
            leader_epoch=leader_epoch,
            now=now,
        )
        if parent is None:
            return None
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            if row is None:
                return None
            chunk = connection.execute(
                "SELECT * FROM delivery_chunks WHERE delivery_id=? AND status IN ('pending','ready','retry_wait','claimed') ORDER BY ordinal LIMIT 1",
                (delivery_id,),
            ).fetchone()
        if chunk is None:
            return None
        chunk_claim = self.database.claim_delivery_chunk(
            int(chunk["id"]),
            lease_seconds=self.lease_seconds,
            worker_id=worker_id,
            leader_epoch=leader_epoch,
            now=now,
        )
        if chunk_claim is None:
            self.database.release_claim(
                "deliveries",
                delivery_id,
                parent,
                status="pending",
                retry_at=now,
                now=now,
            )
            return None
        return parent, chunk_claim, row

    def _begin_sending(
        self,
        delivery_id: int,
        chunk_id: int,
        parent: ClaimToken,
        chunk: ClaimToken,
        now: datetime,
    ) -> bool:
        deadline = _timestamp(now + timedelta(seconds=self.send_deadline_seconds))
        parent_ok = self.database.compare_and_swap(
            "deliveries",
            delivery_id,
            expected={
                "claim_token": parent,
                "claim_version": parent.version,
                "claim_epoch": parent.leader_epoch,
            },
            updates={
                "status": "sending",
                "sending_started_at": _timestamp(now),
                "send_deadline_at": deadline,
            },
            now=now,
        )
        if not parent_ok:
            return False
        chunk_ok = self.database.compare_and_swap(
            "delivery_chunks",
            chunk.row_id if isinstance(chunk.row_id, int) else int(chunk.row_id),
            expected={
                "claim_token": chunk,
                "claim_version": chunk.version,
                "claim_epoch": chunk.leader_epoch,
            },
            updates={
                "status": "sending",
                "sending_started_at": _timestamp(now),
                "send_deadline_at": deadline,
            },
            now=now,
        )
        return chunk_ok

    def _send_call(self, telegram: object, chat_id: int, text: str) -> object:
        method = getattr(telegram, "send_message", None)
        if not callable(method) and callable(telegram):
            method = telegram
        if not callable(method):
            raise NotificationRuntimeError(
                "Telegram send_message method is unavailable"
            )
        return method(chat_id, text, parse_mode="HTML")

    def _circuit_open(self, connection: sqlite3.Connection, now: datetime) -> bool:
        row = connection.execute(
            "SELECT open_until FROM runtime_notification_circuit WHERE circuit_name='telegram'"
        ).fetchone()
        expiry = _parse_time(None if row is None else row[0])
        return expiry is not None and expiry > now

    def _open_circuit(
        self,
        connection: sqlite3.Connection,
        now: datetime,
        *,
        seconds: int,
        reason: str,
    ) -> None:
        expiry = _timestamp(
            now + timedelta(seconds=max(1, min(seconds, MAX_RETRY_AFTER_SECONDS)))
        )
        connection.execute(
            "INSERT INTO runtime_notification_circuit(circuit_name,open_until,reason,version,updated_at) VALUES('telegram',?,?,0,?) ON CONFLICT(circuit_name) DO UPDATE SET open_until=excluded.open_until,reason=excluded.reason,version=runtime_notification_circuit.version+1,updated_at=excluded.updated_at",
            (expiry, _safe_error(reason), _timestamp(now)),
        )

    def _cas_unfenced_claim(
        self,
        table: str,
        row_id: int,
        token: ClaimToken,
        updates: Mapping[str, object],
        now: datetime,
    ) -> bool:
        """CAS a sending row after deadline, without requiring a live lease.

        The token, claim version, row version, and leader epoch are all still
        predicates.  This is the safe equivalent of ``expire_claim`` after a
        process restart, where the original lease may have elapsed.
        """

        with self.database.transaction() as connection:
            row = connection.execute(
                f"SELECT version,claim_version,claim_epoch,claim_token FROM {table} WHERE id=?",
                (row_id,),
            ).fetchone()
            if (
                row is None
                or str(row[3]) != token.token
                or (token.version is not None and int(row[1]) != token.version)
            ):
                return False
            assignments = [f"{column}=?" for column in updates]
            values = list(updates.values())
            assignments.extend(["version=version+1", "updated_at=?"])
            values.extend([_timestamp(now)])
            where = [
                "id=?",
                "claim_token=?",
                "claim_version=?",
                "COALESCE(claim_epoch,0)=?",
            ]
            values.extend([row_id, token.token, token.version, token.leader_epoch])
            result = connection.execute(
                f"UPDATE {table} SET {', '.join(assignments)} WHERE {' AND '.join(where)}",
                (*values,),
            )
            return result.rowcount == 1

    def _transition_after_failure(
        self,
        delivery_id: int,
        chunk_id: int,
        parent: ClaimToken,
        chunk: ClaimToken,
        info: FailureInfo,
        now: datetime,
    ) -> DeliveryAttempt:
        with self.database.connection() as connection:
            parent_row = connection.execute(
                "SELECT send_deadline_at,group_id FROM deliveries WHERE id=?",
                (delivery_id,),
            ).fetchone()
            deadline = _parse_time(None if parent_row is None else parent_row[0]) or now
            runtime = connection.execute(
                "SELECT pretransmission_failures,group_key FROM runtime_notification_delivery WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            failure_count = (
                int(runtime[0]) + 1
                if runtime is not None
                and info.classification
                in {
                    TelegramFailureClass.PRE_TRANSMISSION,
                    TelegramFailureClass.RATE_LIMITED,
                }
                else 0
            )
        if info.classification is TelegramFailureClass.AMBIGUOUS:
            if now < deadline + timedelta(seconds=self.send_grace_seconds):
                # Keep ``sending`` until the finite deadline+grace boundary;
                # another worker cannot claim it while the token is live.
                return DeliveryAttempt(
                    delivery_id, DeliveryOutcome.SKIPPED, error_class="ambiguous"
                )
            chunk_changed = self._cas_unfenced_claim(
                "delivery_chunks",
                chunk.row_id if isinstance(chunk.row_id, int) else int(chunk.row_id),
                chunk,
                {
                    "status": "unknown",
                    "unknown_at": _timestamp(now),
                    "unknown_reason": _safe_error(info.error),
                    "error_class": TelegramFailureClass.AMBIGUOUS.value,
                },
                now,
            )
            parent_changed = self._cas_unfenced_claim(
                "deliveries",
                delivery_id,
                parent,
                {
                    "status": "unknown",
                    "unknown_at": _timestamp(now),
                    "unknown_reason": _safe_error(info.error),
                    "last_error_class": TelegramFailureClass.AMBIGUOUS.value,
                },
                now,
            )
            if not chunk_changed or not parent_changed:
                return DeliveryAttempt(
                    delivery_id, DeliveryOutcome.SKIPPED, error_class="claim_conflict"
                )
            self._set_delivery_logical_state(delivery_id, "unknown", now=now)
            self._set_group_obligations_by_delivery(
                delivery_id, "unknown", "unknown", now
            )
            self._sync_group_delivery_state(delivery_id, "unknown", now)
            return DeliveryAttempt(
                delivery_id,
                DeliveryOutcome.UNKNOWN,
                error_class=info.classification.value,
            )
        if info.classification is TelegramFailureClass.DESTINATION_BLOCKED:
            # Sending -> failed -> delivery_blocked is the transition allowed
            # by the checked-in migration triggers.
            self._transition_claimed_rows(
                delivery_id,
                chunk.row_id if isinstance(chunk.row_id, int) else int(chunk.row_id),
                parent,
                chunk,
                "delivery_blocked",
                now,
                info.error,
            )
            self._set_delivery_logical_state(delivery_id, "delivery_blocked", now=now)
            self._set_group_obligations_by_delivery(
                delivery_id, "delivery_blocked", "blocked", now
            )
            self._sync_group_delivery_state(delivery_id, "failed", now)
            return DeliveryAttempt(
                delivery_id,
                DeliveryOutcome.DELIVERY_BLOCKED,
                error_class=info.classification.value,
            )
        if info.classification is TelegramFailureClass.AUTHENTICATION:
            delay = info.retry_after if info.retry_after is not None else 60
            self._transition_claimed_rows(
                delivery_id,
                chunk.row_id if isinstance(chunk.row_id, int) else int(chunk.row_id),
                parent,
                chunk,
                "failed",
                now,
                info.error,
            )
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE deliveries SET retry_due_at=?,last_error_class=?,error_text=?,updated_at=? WHERE id=? AND status='failed'",
                    (
                        _timestamp(now + timedelta(seconds=delay)),
                        info.classification.value,
                        _safe_error(info.error),
                        _timestamp(now),
                        delivery_id,
                    ),
                )
                connection.execute(
                    "UPDATE runtime_notification_delivery SET logical_state='retry_wait',retry_due_at=?,circuit_until=?,version=version+1,updated_at=? WHERE delivery_id=?",
                    (
                        _timestamp(now + timedelta(seconds=delay)),
                        _timestamp(now + timedelta(seconds=delay)),
                        _timestamp(now),
                        delivery_id,
                    ),
                )
                self._open_circuit(connection, now, seconds=delay, reason=info.error)
            self._set_group_obligations_by_delivery(
                delivery_id, "retry_wait", "eligible", now
            )
            return DeliveryAttempt(
                delivery_id,
                DeliveryOutcome.GLOBAL_CIRCUIT,
                retry_due_at=now + timedelta(seconds=delay),
                error_class=info.classification.value,
            )
        if info.classification in {
            TelegramFailureClass.PRE_TRANSMISSION,
            TelegramFailureClass.RATE_LIMITED,
        }:
            index = failure_count - 1
            if index < len(RETRY_DELAYS_SECONDS):
                delay = RETRY_DELAYS_SECONDS[index]
                if info.retry_after is not None:
                    delay = max(delay, min(info.retry_after, MAX_RETRY_AFTER_SECONDS))
                retry_at = now + timedelta(seconds=delay)
                self._transition_claimed_rows(
                    delivery_id,
                    chunk.row_id
                    if isinstance(chunk.row_id, int)
                    else int(chunk.row_id),
                    parent,
                    chunk,
                    "failed",
                    now,
                    info.error,
                )
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE deliveries SET retry_due_at=?,last_error_class=?,error_text=?,updated_at=? WHERE id=? AND status='failed'",
                        (
                            _timestamp(retry_at),
                            info.classification.value,
                            _safe_error(info.error),
                            _timestamp(now),
                            delivery_id,
                        ),
                    )
                    connection.execute(
                        "UPDATE delivery_chunks SET error_class=?,error_text=?,updated_at=? WHERE id=? AND status='failed'",
                        (
                            info.classification.value,
                            _safe_error(info.error),
                            _timestamp(now),
                            chunk.row_id,
                        ),
                    )
                    connection.execute(
                        "UPDATE runtime_notification_delivery SET logical_state='retry_wait',pretransmission_failures=?,retry_due_at=?,version=version+1,updated_at=? WHERE delivery_id=?",
                        (
                            failure_count,
                            _timestamp(retry_at),
                            _timestamp(now),
                            delivery_id,
                        ),
                    )
                self._set_group_obligations_by_delivery(
                    delivery_id, "retry_wait", "eligible", now
                )
                return DeliveryAttempt(
                    delivery_id,
                    DeliveryOutcome.RETRY_WAIT,
                    retry_due_at=retry_at,
                    error_class=info.classification.value,
                )
            self._transition_claimed_rows(
                delivery_id,
                chunk.row_id if isinstance(chunk.row_id, int) else int(chunk.row_id),
                parent,
                chunk,
                "failed",
                now,
                info.error,
            )
            self._set_delivery_logical_state(delivery_id, "failed", now=now)
            self._set_group_obligations_by_delivery(
                delivery_id, "failed", "failed", now
            )
            self._sync_group_delivery_state(delivery_id, "failed", now)
            return DeliveryAttempt(
                delivery_id,
                DeliveryOutcome.FAILED,
                error_class=info.classification.value,
            )
        self._transition_claimed_rows(
            delivery_id,
            chunk.row_id if isinstance(chunk.row_id, int) else int(chunk.row_id),
            parent,
            chunk,
            "failed",
            now,
            info.error,
        )
        self._set_delivery_logical_state(delivery_id, "failed", now=now)
        self._set_group_obligations_by_delivery(delivery_id, "failed", "failed", now)
        self._sync_group_delivery_state(delivery_id, "failed", now)
        return DeliveryAttempt(
            delivery_id, DeliveryOutcome.FAILED, error_class=info.classification.value
        )

    def _transition_claimed_rows(
        self,
        delivery_id: int,
        chunk_id: int,
        parent: ClaimToken,
        chunk: ClaimToken,
        target: str,
        now: datetime,
        error: str,
    ) -> None:
        # If a target is not directly allowed from sending, use failed as the
        # migration-approved intermediate state.
        if target == "delivery_blocked":
            for table, row_id, token in (
                ("delivery_chunks", chunk_id, chunk),
                ("deliveries", delivery_id, parent),
            ):
                error_column = (
                    "error_class" if table == "delivery_chunks" else "last_error_class"
                )
                self._cas_unfenced_claim(
                    table,
                    row_id,
                    token,
                    {
                        "status": "failed",
                        "error_text": _safe_error(error),
                        error_column: TelegramFailureClass.DESTINATION_BLOCKED.value,
                    },
                    now,
                )
                blocked_updates: dict[str, object] = {
                    "status": "delivery_blocked",
                    "error_text": _safe_error(error),
                    error_column: TelegramFailureClass.DESTINATION_BLOCKED.value,
                }
                if table == "deliveries":
                    blocked_updates["terminal_at"] = _timestamp(now)
                self._cas_unfenced_claim(table, row_id, token, blocked_updates, now)
            return
        for table, row_id, token in (
            ("delivery_chunks", chunk_id, chunk),
            ("deliveries", delivery_id, parent),
        ):
            error_column = (
                "error_class" if table == "delivery_chunks" else "last_error_class"
            )
            updates: dict[str, object] = {
                "status": target,
                "error_text": _safe_error(error),
                error_column: TelegramFailureClass.PRE_TRANSMISSION.value
                if target == "failed"
                else TelegramFailureClass.APPLICATION.value,
            }
            if target == "failed" and table == "deliveries":
                updates["terminal_at"] = _timestamp(now)
            self._cas_unfenced_claim(table, row_id, token, updates, now)

    def _set_delivery_logical_state(
        self, delivery_id: int, state: str, *, now: datetime
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE runtime_notification_delivery SET logical_state=?,updated_at=?,version=version+1 WHERE delivery_id=?",
                (state, _timestamp(now), delivery_id),
            )

    def _set_group_obligations_by_delivery(
        self, delivery_id: int, state: str, membership: str, now: datetime
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE runtime_notification_obligations SET state=?,membership_status=?,updated_at=?,version=version+1 WHERE delivery_id=? AND state NOT IN ('sent','assumed_sent','superseded','canceled','quarantined','suppressed')",
                (state, membership, _timestamp(now), delivery_id),
            )

    def _sync_group_delivery_state(
        self, delivery_id: int, target: str, now: datetime
    ) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT deliveries.group_id,notification_groups.status FROM notification_groups JOIN deliveries ON deliveries.group_id=notification_groups.id WHERE deliveries.id=?",
                (delivery_id,),
            ).fetchone()
            if row is None or row[0] is None:
                return
            current = str(row[1])
            if target == "sent":
                if current == "ready":
                    self._transition_group(
                        connection, int(row[0]), "ready", "claimed", now
                    )
                    current = "claimed"
                if current == "claimed":
                    self._transition_group(
                        connection, int(row[0]), "claimed", "sending", now
                    )
                    current = "sending"
                if current == "sending":
                    self._transition_group(
                        connection, int(row[0]), "sending", "sent", now
                    )
            elif target in {"failed", "unknown"}:
                if current == "ready":
                    self._transition_group(
                        connection, int(row[0]), "ready", "claimed", now
                    )
                    current = "claimed"
                if current == "claimed":
                    self._transition_group(
                        connection, int(row[0]), "claimed", "sending", now
                    )
                    current = "sending"
                if current == "sending":
                    self._transition_group(
                        connection, int(row[0]), "sending", target, now
                    )

    def _requeue_due(self, delivery_id: int, now: datetime) -> bool:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status,version FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            chunk = connection.execute(
                "SELECT id,status,version FROM delivery_chunks WHERE delivery_id=? AND status IN ('failed','pending','retry_wait') ORDER BY ordinal LIMIT 1",
                (delivery_id,),
            ).fetchone()
            runtime = connection.execute(
                "SELECT logical_state,retry_due_at FROM runtime_notification_delivery WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if (
                row is None
                or runtime is None
                or str(runtime[0]) not in {"retry_wait", "pending"}
            ):
                return False
            due = _parse_time(runtime[1])
            if due is not None and due > now:
                return False
            if str(row[0]) == "failed":
                connection.execute(
                    "UPDATE deliveries SET status='pending',retry_due_at=NULL,terminal_at=NULL,claim_token=NULL,claim_expires_at=NULL,claim_version=NULL,claim_epoch=NULL,claim_worker=NULL,claimed_at=NULL,version=version+1,updated_at=? WHERE id=? AND version=?",
                    (_timestamp(now), delivery_id, int(row[1])),
                )
            elif str(row[0]) not in {"pending", "ready"}:
                return False
            if chunk is not None and str(chunk[1]) == "failed":
                connection.execute(
                    "UPDATE delivery_chunks SET status='pending',claim_token=NULL,claim_expires_at=NULL,claim_version=NULL,claim_epoch=NULL,claim_worker=NULL,claimed_at=NULL,version=version+1,updated_at=? WHERE id=? AND version=?",
                    (_timestamp(now), int(chunk[0]), int(chunk[2])),
                )
            connection.execute(
                "UPDATE runtime_notification_delivery SET logical_state='pending',retry_due_at=NULL,updated_at=?,version=version+1 WHERE delivery_id=?",
                (_timestamp(now), delivery_id),
            )
            return True

    def deliver_due(
        self,
        telegram: object,
        *,
        worker_id: str = "media-notification-worker",
        leader_epoch: int | None = None,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[DeliveryAttempt, ...]:
        """Claim and transmit due parents; at most one worker can win each."""

        current = self._current(now)
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("delivery limit must be positive")
        epoch = leader_epoch
        if epoch is None:
            leader = self.database.current_leader("media", now=current)
            epoch = 0 if leader is None else leader.epoch
        if epoch < 0:
            raise ValueError("leader_epoch must be non-negative")
        self.materialize_due(now=current, limit=limit)
        with self.database.connection() as connection:
            if self._circuit_open(connection, current):
                return ()
            rows = connection.execute(
                """
                SELECT d.id FROM deliveries d
                JOIN runtime_notification_delivery r ON r.delivery_id=d.id
                JOIN notification_groups g ON g.id=d.group_id
                WHERE r.logical_state IN ('pending','retry_wait')
                  AND (r.retry_due_at IS NULL OR r.retry_due_at <= ?)
                  AND g.due_at <= ?
                  AND d.status IN ('pending','ready','failed')
                ORDER BY d.id LIMIT ?
                """,
                (_timestamp(current), _timestamp(current), limit),
            ).fetchall()
        results: list[DeliveryAttempt] = []
        for row in rows:
            delivery_id = int(row[0])
            if self._requeue_due(delivery_id, current) is False:
                # Pending rows return false because no requeue is needed; they
                # are still eligible for a claim.
                with self.database.connection() as connection:
                    state = connection.execute(
                        "SELECT logical_state FROM runtime_notification_delivery WHERE delivery_id=?",
                        (delivery_id,),
                    ).fetchone()
                if state is None or str(state[0]) not in {"pending", "retry_wait"}:
                    continue
            claimed = self._claim_parent_and_chunk(
                delivery_id, worker_id=worker_id, leader_epoch=epoch, now=current
            )
            if claimed is None:
                continue
            parent, chunk, parent_row = claimed
            chunk_id = int(chunk.row_id)
            if not self._begin_sending(delivery_id, chunk_id, parent, chunk, current):
                continue
            try:
                payload = self._chunk_payload(chunk_id)
                result = self._send_call(telegram, int(parent_row["chat_id"]), payload)
                attempt_now = current if now is not None else self._current()
                failure = _failure_from_result(result)
                if failure is not None:
                    raise _ClassifiedFailure(failure)
                message_id = self._message_id(result)
                send_deadline = _parse_time(parent_row["send_deadline_at"])
                if (
                    send_deadline is not None
                    and attempt_now
                    > send_deadline + timedelta(seconds=self.send_grace_seconds)
                ):
                    results.append(
                        self._transition_after_failure(
                            delivery_id,
                            chunk_id,
                            parent,
                            chunk,
                            FailureInfo(
                                TelegramFailureClass.AMBIGUOUS,
                                "send result arrived after the commit grace period",
                                transmitted=True,
                            ),
                            attempt_now,
                        )
                    )
                    continue
                if not self._complete_chunk_and_parent_if_done(
                    delivery_id, chunk_id, parent, chunk, message_id, attempt_now
                ):
                    results.append(
                        DeliveryAttempt(delivery_id, DeliveryOutcome.SKIPPED)
                    )
                    continue
                results.append(
                    DeliveryAttempt(
                        delivery_id,
                        DeliveryOutcome.SENT,
                        chunks_sent=1,
                        message_ids=(() if message_id is None else (message_id,)),
                    )
                )
            except _ClassifiedFailure as exc:
                attempt_now = current if now is not None else self._current()
                results.append(
                    self._transition_after_failure(
                        delivery_id, chunk_id, parent, chunk, exc.info, attempt_now
                    )
                )
            except BaseException as exc:
                info = _failure_from_exception(exc, sending=True)
                attempt_now = current if now is not None else self._current()
                results.append(
                    self._transition_after_failure(
                        delivery_id, chunk_id, parent, chunk, info, attempt_now
                    )
                )
        return tuple(results)

    deliver_pending = deliver_due

    def _chunk_payload(self, chunk_id: int) -> str:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM delivery_chunks WHERE id=?", (chunk_id,)
            ).fetchone()
        if row is None:
            raise NotificationRuntimeError("delivery chunk disappeared")
        try:
            payload = json.loads(str(row[0]))
        except json.JSONDecodeError as exc:
            raise NotificationRuntimeError("delivery payload is invalid") from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("text"), str):
            raise NotificationRuntimeError("delivery payload is invalid")
        text = str(payload["text"])
        if len(text.encode("utf-8")) > self.message_limit:
            raise NotificationRuntimeError("delivery payload exceeds message limit")
        return text

    @staticmethod
    def _message_id(result: object) -> int | None:
        value = getattr(result, "message_id", None)
        if isinstance(result, Mapping):
            value = result.get("message_id", value)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _complete_chunk_and_parent_if_done(
        self,
        delivery_id: int,
        chunk_id: int,
        parent: ClaimToken,
        chunk: ClaimToken,
        message_id: int | None,
        now: datetime,
    ) -> bool:
        if not self.database.complete_claim(
            "delivery_chunks",
            chunk_id,
            chunk,
            status="sent",
            updates={"telegram_message_id": message_id, "sent_at": _timestamp(now)},
            now=now,
        ):
            return False
        with self.database.connection() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM delivery_chunks WHERE delivery_id=? AND status NOT IN ('sent','assumed_sent')",
                (delivery_id,),
            ).fetchone()
        if remaining is not None and int(remaining[0]) > 0:
            return True
        if not self.database.complete_claim(
            "deliveries",
            delivery_id,
            parent,
            status="sent",
            updates={
                "telegram_message_id": message_id,
                "sent_at": _timestamp(now),
                "terminal_at": _timestamp(now),
            },
            now=now,
        ):
            return False
        self._set_delivery_logical_state(delivery_id, "sent", now=now)
        self._set_group_obligations_by_delivery(delivery_id, "sent", "fulfilled", now)
        self._sync_group_delivery_state(delivery_id, "sent", now)
        return True

    def expire_sending(self, *, now: datetime | None = None, limit: int = 100) -> int:
        """Convert only deadline+grace ``sending`` rows to ``unknown``."""

        current = self._current(now)
        count = 0
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM deliveries WHERE status='sending' AND send_deadline_at IS NOT NULL AND datetime(send_deadline_at) <= datetime(?) LIMIT ?",
                (
                    _timestamp(current - timedelta(seconds=self.send_grace_seconds)),
                    limit,
                ),
            ).fetchall()
        for row in rows:
            delivery_id = int(row[0])
            with self.database.connection() as connection:
                parent = connection.execute(
                    "SELECT claim_token,claim_version,claim_epoch,claim_expires_at FROM deliveries WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                chunk = connection.execute(
                    "SELECT id,claim_token,claim_version,claim_epoch,claim_expires_at FROM delivery_chunks WHERE delivery_id=? AND status='sending' ORDER BY ordinal LIMIT 1",
                    (delivery_id,),
                ).fetchone()
            if parent is None or chunk is None or parent[0] is None or chunk[1] is None:
                continue
            parent_token = ClaimToken(
                str(parent[0]),
                "deliveries",
                delivery_id,
                str(parent[3] or _timestamp(current)),
                int(parent[1]) if parent[1] is not None else None,
                int(parent[2] or 0),
            )
            chunk_token = ClaimToken(
                str(chunk[1]),
                "delivery_chunks",
                int(chunk[0]),
                str(chunk[4] or _timestamp(current)),
                int(chunk[2]) if chunk[2] is not None else None,
                int(chunk[3] or 0),
            )
            info = FailureInfo(
                TelegramFailureClass.AMBIGUOUS,
                "sending deadline expired after grace period",
                transmitted=True,
            )
            result = self._transition_after_failure(
                delivery_id, int(chunk[0]), parent_token, chunk_token, info, current
            )
            count += int(result.outcome is DeliveryOutcome.UNKNOWN)
        return count

    expire_claims = expire_sending

    def manual_action(
        self,
        delivery_id: int,
        action: str,
        *,
        confirmed: bool = False,
        now: datetime | None = None,
    ) -> Mapping[str, object]:
        """Apply one explicit dashboard recovery action."""

        if not confirmed:
            raise NotificationRuntimeError(
                "manual notification recovery requires confirmation"
            )
        current = self._current(now)
        normalized = action.strip().lower().replace(" ", "_")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            runtime = connection.execute(
                "SELECT * FROM runtime_notification_delivery WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
        if row is None or runtime is None:
            return {"ok": False, "status": "not_found", "delivery_id": delivery_id}
        logical = str(runtime["logical_state"])
        if normalized in {"retry", "retry_once"}:
            if logical != "failed" or bool(runtime["recovery_attempted"]):
                return {"ok": False, "status": logical, "delivery_id": delivery_id}
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE deliveries SET status='pending',retry_due_at=NULL,terminal_at=NULL,claim_token=NULL,claim_expires_at=NULL,claim_version=NULL,claim_epoch=NULL,claim_worker=NULL,claimed_at=NULL,version=version+1,updated_at=? WHERE id=? AND status='failed' AND version=?",
                    (_timestamp(current), delivery_id, int(row["version"])),
                )
                connection.execute(
                    "UPDATE delivery_chunks SET status='pending',claim_token=NULL,claim_expires_at=NULL,claim_version=NULL,claim_epoch=NULL,claim_worker=NULL,claimed_at=NULL,version=version+1,updated_at=? WHERE delivery_id=? AND status='failed'",
                    (_timestamp(current), delivery_id),
                )
                connection.execute(
                    "UPDATE runtime_notification_delivery SET logical_state='pending',recovery_attempted=1,pretransmission_failures=0,retry_due_at=NULL,version=version+1,updated_at=? WHERE delivery_id=? AND logical_state='failed' AND recovery_attempted=0",
                    (_timestamp(current), delivery_id),
                )
            return {"ok": True, "status": "pending", "delivery_id": delivery_id}
        if normalized in {"abandon", "mark_abandoned"}:
            if logical not in {"failed", "retry_wait"}:
                return {"ok": False, "status": logical, "delivery_id": delivery_id}
            with self.database.transaction() as connection:
                if str(row["status"]) == "retry_wait":
                    connection.execute(
                        "UPDATE deliveries SET status='failed',version=version+1,updated_at=? WHERE id=? AND status='retry_wait'",
                        (_timestamp(current), delivery_id),
                    )
                connection.execute(
                    "UPDATE deliveries SET status='abandoned',abandoned_at=?,terminal_at=?,version=version+1,updated_at=? WHERE id=? AND status='failed'",
                    (
                        _timestamp(current),
                        _timestamp(current),
                        _timestamp(current),
                        delivery_id,
                    ),
                )
                connection.execute(
                    "UPDATE runtime_notification_delivery SET logical_state='abandoned',version=version+1,updated_at=? WHERE delivery_id=?",
                    (_timestamp(current), delivery_id),
                )
            self._set_group_obligations_by_delivery(
                delivery_id, "abandoned", "failed", current
            )
            return {"ok": True, "status": "abandoned", "delivery_id": delivery_id}
        if normalized in {"assume_sent", "assume"}:
            if logical != "unknown":
                return {"ok": False, "status": logical, "delivery_id": delivery_id}
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE deliveries SET status='assumed_sent',unknown_resolved=1,terminal_at=?,version=version+1,updated_at=? WHERE id=? AND status='unknown'",
                    (_timestamp(current), _timestamp(current), delivery_id),
                )
                connection.execute(
                    "UPDATE delivery_chunks SET status='assumed_sent',updated_at=? WHERE delivery_id=? AND status='unknown'",
                    (_timestamp(current), delivery_id),
                )
                connection.execute(
                    "UPDATE runtime_notification_delivery SET logical_state='assumed_sent',version=version+1,updated_at=? WHERE delivery_id=?",
                    (_timestamp(current), delivery_id),
                )
            self._set_group_obligations_by_delivery(
                delivery_id, "assumed_sent", "fulfilled", current
            )
            self._sync_group_delivery_state(delivery_id, "sent", current)
            return {"ok": True, "status": "assumed_sent", "delivery_id": delivery_id}
        if normalized in {"resend", "resend_once"}:
            return self._resend_once(delivery_id, current)
        raise NotificationRuntimeError(
            f"unknown manual notification action: {action!r}"
        )

    recover = manual_action

    def _resend_once(self, delivery_id: int, now: datetime) -> Mapping[str, object]:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            runtime = connection.execute(
                "SELECT * FROM runtime_notification_delivery WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if (
                row is None
                or runtime is None
                or str(runtime["logical_state"]) != "unknown"
                or bool(runtime["recovery_attempted"])
            ):
                return {
                    "ok": False,
                    "status": "unknown" if runtime is not None else "not_found",
                    "delivery_id": delivery_id,
                }
            connection.execute(
                "UPDATE deliveries SET status='superseded',unknown_resolved=1,terminal_at=?,version=version+1,updated_at=? WHERE id=? AND status='unknown'",
                (_timestamp(now), _timestamp(now), delivery_id),
            )
            connection.execute(
                "UPDATE delivery_chunks SET status='superseded',updated_at=? WHERE delivery_id=? AND status='unknown'",
                (_timestamp(now), delivery_id),
            )
            resend_key = f"{str(row['idempotency_key'])}:resend:1"
            connection.execute(
                """
                INSERT INTO deliveries(group_id,destination,chat_id,notification_class,event_key,
                    subscription_generation,idempotency_key,status,obligation_key,chunk_ordinal,chunk_count,
                    parent_delivery_id,possible_duplicate,recovery_generation,version,created_at,updated_at)
                SELECT group_id,destination,chat_id,notification_class,event_key,subscription_generation,?,
                    'pending',NULL,1,chunk_count,?,1,1,0,?,?
                FROM deliveries WHERE id=?
                """,
                (
                    resend_key,
                    delivery_id,
                    _timestamp(now),
                    _timestamp(now),
                    delivery_id,
                ),
            )
            new_row = connection.execute(
                "SELECT id FROM deliveries WHERE idempotency_key=?", (resend_key,)
            ).fetchone()
            if new_row is None:
                raise NotificationRuntimeError("resend row was not persisted")
            new_id = int(new_row[0])
            connection.execute(
                "INSERT INTO runtime_notification_delivery(delivery_id,group_key,logical_state,recovery_attempted,possible_duplicate,updated_at) SELECT ?,group_key,'pending',1,1,? FROM runtime_notification_delivery WHERE delivery_id=?",
                (new_id, _timestamp(now), delivery_id),
            )
            chunks = connection.execute(
                "SELECT ordinal,chunk_count,stable_key,payload_json FROM delivery_chunks WHERE delivery_id=? ORDER BY ordinal",
                (delivery_id,),
            ).fetchall()
            for chunk in chunks:
                stable = hashlib.sha256(
                    f"{resend_key}:{int(chunk[0])}:{str(chunk[3])}".encode()
                ).hexdigest()
                connection.execute(
                    "INSERT INTO delivery_chunks(delivery_id,ordinal,chunk_count,stable_key,payload_json,status,possible_duplicate,version,created_at,updated_at) VALUES(?,?,?,?,?,'pending',1,0,?,?)",
                    (
                        new_id,
                        int(chunk[0]),
                        int(chunk[1]),
                        stable,
                        str(chunk[3]),
                        _timestamp(now),
                        _timestamp(now),
                    ),
                )
            connection.execute(
                "UPDATE runtime_notification_obligations SET delivery_id=?,state=CASE WHEN state='suppressed' THEN 'suppressed' ELSE 'pending' END,membership_status=CASE WHEN state='suppressed' THEN 'suppressed' ELSE 'eligible' END,version=version+1,updated_at=? WHERE delivery_id=? AND state IN ('unknown','suppressed')",
                (new_id, _timestamp(now), delivery_id),
            )
            connection.execute(
                "UPDATE runtime_notification_delivery SET logical_state='superseded',version=version+1,updated_at=? WHERE delivery_id=?",
                (_timestamp(now), delivery_id),
            )
        return {
            "ok": True,
            "status": "pending",
            "delivery_id": new_id,
            "previous_delivery_id": delivery_id,
            "possible_duplicate": True,
        }

    def get_delivery(self, delivery_id: int) -> RuntimeDelivery | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT d.*,r.group_key,r.logical_state,r.retry_due_at,r.possible_duplicate FROM deliveries d JOIN runtime_notification_delivery r ON r.delivery_id=d.id WHERE d.id=?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            return None
        return RuntimeDelivery(
            int(row["id"]),
            str(row["group_key"]),
            str(row["destination"]),
            int(row["chat_id"]),
            str(row["notification_class"]),
            str(row["status"]),
            str(row["logical_state"]),
            int(row["attempts"]),
            _parse_time(row["retry_due_at"]),
            _parse_time(row["claim_expires_at"]),
            int(row["chunk_count"]),
            bool(row["possible_duplicate"]),
        )

    def list_deliveries(self) -> tuple[RuntimeDelivery, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM deliveries WHERE id IN (SELECT delivery_id FROM runtime_notification_delivery) ORDER BY id"
            ).fetchall()
        result: list[RuntimeDelivery] = []
        for row in rows:
            value = self.get_delivery(int(row[0]))
            if value is not None:
                result.append(value)
        return tuple(result)

    def oracle(
        self,
        *,
        scan_complete: bool = False,
        full_sweep_complete: bool = False,
        scans_fresh: bool = False,
        keys: Iterable[str] | None = None,
    ) -> OracleResult:
        with self.database.connection() as connection:
            rows = self._runtime_obligation_rows(connection, keys)
        obligations: list[Mapping[str, object]] = []
        accounted: dict[ObligationKey, str] = {}
        for row in rows:
            try:
                key = _key_from_json(row["obligation_key"])
            except NotificationRuntimeError:
                continue
            obligations.append({"key": key})
            accounted[key] = str(row["state"])
        return evaluate_oracle(
            obligations,
            accounted,
            scan_complete=scan_complete,
            full_sweep_complete=full_sweep_complete,
            scans_fresh=scans_fresh,
        )

    accounting = oracle

    def due_failure_alerts(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> tuple[int, ...]:
        """Return failed deliveries whose immediate/24h/7d alert is due."""

        current = self._current(now)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id,terminal_at,alert_count FROM deliveries WHERE status='failed' AND terminal_at IS NOT NULL ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        result: list[int] = []
        for row in rows:
            terminal = _parse_time(row[1])
            count = int(row[2] or 0)
            if (
                terminal is not None
                and count < len(FAILURE_ALERT_DELAYS_SECONDS)
                and current
                >= terminal + timedelta(seconds=FAILURE_ALERT_DELAYS_SECONDS[count])
            ):
                result.append(int(row[0]))
        return tuple(result)

    def record_failure_alert(
        self, delivery_id: int, *, now: datetime | None = None
    ) -> bool:
        current = self._current(now)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT alert_count,terminal_at,status,version FROM deliveries WHERE id=?",
                (delivery_id,),
            ).fetchone()
            if row is None or str(row[2]) != "failed" or row[1] is None:
                return False
            terminal = _parse_time(row[1])
            count = int(row[0] or 0)
            if (
                terminal is None
                or count >= len(FAILURE_ALERT_DELAYS_SECONDS)
                or current
                < terminal + timedelta(seconds=FAILURE_ALERT_DELAYS_SECONDS[count])
            ):
                return False
            return (
                connection.execute(
                    "UPDATE deliveries SET alert_count=alert_count+1,last_alert_at=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                    (
                        _timestamp(current),
                        _timestamp(current),
                        delivery_id,
                        int(row[3]),
                    ),
                ).rowcount
                == 1
            )


class _ClassifiedFailure(Exception):
    def __init__(self, info: FailureInfo) -> None:
        super().__init__(info.error)
        self.info = info


class DurableNotificationService:
    """Explicit worker-facing facade over :class:`DurableNotificationRepository`."""

    def __init__(self, repository: DurableNotificationRepository) -> None:
        self.repository = repository

    def plan(self, *args: object, **kwargs: object) -> RuntimePlan:
        return self.repository.plan(*args, **kwargs)  # type: ignore[arg-type]

    plan_notifications = plan

    def deliver_pending(
        self,
        telegram: object,
        *,
        worker_id: str = "media-notification-worker",
        leader_epoch: int | None = None,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        total = 0
        for result in self.repository.deliver_due(
            telegram,
            worker_id=worker_id,
            leader_epoch=leader_epoch,
            now=now,
            limit=limit,
        ):
            total += result.chunks_sent
        return total

    deliver = deliver_pending

    def recover(
        self,
        delivery_id: int,
        action: str,
        *,
        confirmed: bool = False,
        now: datetime | None = None,
    ) -> Mapping[str, object]:
        return self.repository.manual_action(
            delivery_id, action, confirmed=confirmed, now=now
        )

    def oracle(self, **kwargs: object) -> OracleResult:
        return self.repository.oracle(**kwargs)  # type: ignore[arg-type]


# Names used by the worker composition and by callers migrating from the
# earlier pure modules.
NotificationRepository = DurableNotificationRepository
NotificationRuntime = DurableNotificationService
RuntimeNotificationRepository = DurableNotificationRepository
RuntimeNotificationService = DurableNotificationService


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DeliveryAttempt",
    "DeliveryOutcome",
    "DurableNotificationRepository",
    "DurableNotificationService",
    "FailureInfo",
    "NotificationClaimError",
    "NotificationNotDue",
    "NotificationRepository",
    "NotificationRuntime",
    "NotificationRuntimeError",
    "RuntimeDelivery",
    "RuntimeNotificationRepository",
    "RuntimeNotificationService",
    "RuntimePlan",
]
