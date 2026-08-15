"""Bounded retention planning for terminal notification records.

Cleanup returns selections and scrub plans; it never deletes rows itself.  The
caller applies each plan in a transaction with the same claim/CAS discipline
as delivery transitions.  In particular, unknown Telegram outcomes,
active subscriptions, and unresolved quarantine records are never selected by
the 60-day personal-data purge.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, cast

from .redaction import is_path_key, is_secret_key, is_url_key, redact_text

RETENTION_DAYS = 60
QUARANTINE_ALERT_DAYS = 30
QUARANTINE_ALERT_REPEAT_DAYS = 7
RETENTION_WINDOW = timedelta(days=RETENTION_DAYS)
QUARANTINE_ALERT_WINDOW = timedelta(days=QUARANTINE_ALERT_DAYS)
QUARANTINE_ALERT_REPEAT_WINDOW = timedelta(days=QUARANTINE_ALERT_REPEAT_DAYS)


class CleanupError(ValueError):
    """Invalid cleanup input."""


class CleanupAction(str, Enum):
    SCRUB = "scrub"
    DELETE = "delete"
    EXPIRE = "expire"
    ALERT = "alert"


TERMINAL_STATUSES = frozenset(
    {
        "sent",
        "assumed_sent",
        "failed",
        "abandoned",
        "delivery_blocked",
        "canceled",
        "cancelled",
        "superseded",
        "fulfilled",
        "archived",
        "resolved",
        "terminal",
    }
)
NON_TERMINAL_STATUSES = frozenset(
    {
        "pending",
        "ready",
        "claimed",
        "sending",
        "retry_wait",
        "unknown",
        "active",
        "open",
        "quarantined",
        "processing",
    }
)
EPHEMERAL_KINDS = frozenset(
    {"candidate", "cursor", "nonce", "confirmation", "dashboard_session", "session"}
)


def _utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise CleanupError("cleanup timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def _value(record: object, *names: str, default: object = None) -> object:
    if isinstance(record, Mapping):
        for name in names:
            if name in record:
                return record[name]
    else:
        for name in names:
            if hasattr(record, name):
                return getattr(record, name)
    return default


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return _utc(datetime.fromisoformat(text))
        except ValueError as exc:
            raise CleanupError("invalid terminal/expiry timestamp") from exc
    raise CleanupError("invalid terminal/expiry timestamp")


def _record_status(record: object) -> str:
    value = _value(record, "status", "state", default="")
    if hasattr(value, "value"):
        value = value.value
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _record_kind(record: object) -> str:
    value = _value(record, "record_type", "kind", "type", default="record")
    return str(value).strip().lower()


def _record_id(record: object) -> str:
    value = _value(record, "record_id", "id", "key", default="")
    return str(value)


def _terminal_at(record: object) -> datetime | None:
    return _timestamp(
        _value(
            record,
            "terminal_at",
            "resolved_at",
            "completed_at",
            "fulfilled_at",
            "sent_at",
            "closed_at",
            "quarantined_at",
            "unresolved_since",
            "created_at",
        )
    )


def _expiry_at(record: object) -> datetime | None:
    return _timestamp(_value(record, "expires_at", "expiry_at", "expiration_at"))


def _bool(record: object, *names: str, default: bool = False) -> bool:
    value = _value(record, *names, default=default)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return default


def _normalized_field_key(value: object) -> str:
    text = str(value).strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text).lower()
    return re.sub(r"[-\s]+", "_", text)


@dataclass(frozen=True, slots=True)
class CleanupRecord:
    """Normalized metadata needed to decide retention eligibility."""

    record_type: str
    record_id: str
    status: str
    terminal_at: datetime | None = None
    personal: bool = True
    unknown_outcome: bool = False
    unresolved_quarantine: bool = False
    active: bool = False
    payload: Mapping[str, Any] | None = None
    dedupe_key: str | None = None
    expires_at: datetime | None = None
    last_alert_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.record_type, str)
            or not isinstance(self.record_id, str)
            or not isinstance(self.status, str)
            or not self.record_type.strip()
            or not self.record_id.strip()
            or not self.status.strip()
        ):
            raise CleanupError("cleanup record type/id are required")
        object.__setattr__(self, "record_type", self.record_type.strip().lower())
        object.__setattr__(self, "record_id", self.record_id.strip())
        object.__setattr__(self, "status", self.status.strip().lower())
        for field_name in (
            "personal",
            "unknown_outcome",
            "unresolved_quarantine",
            "active",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise CleanupError(f"{field_name} must be boolean")
        if self.status == "quarantined" and not self.unresolved_quarantine:
            # A typed quarantine row has no separate resolved flag, so its
            # conservative default is unresolved.  Repository rows with
            # explicit resolution are normalized to ``resolved`` in
            # ``from_record`` before construction.
            object.__setattr__(self, "unresolved_quarantine", True)
        if self.terminal_at is not None:
            object.__setattr__(self, "terminal_at", _utc(self.terminal_at))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _utc(self.expires_at))
        if self.last_alert_at is not None:
            object.__setattr__(self, "last_alert_at", _utc(self.last_alert_at))

    @classmethod
    def from_record(cls, record: object) -> CleanupRecord:
        raw_status = _record_status(record)
        resolved = _bool(record, "resolved", "is_resolved", default=False) or (
            _value(record, "resolved_at") is not None
        )
        unresolved_quarantine = _bool(
            record, "unresolved_quarantine", "quarantine_unresolved", default=False
        ) or (raw_status == "quarantined" and not resolved)
        status = "resolved" if raw_status == "quarantined" and resolved else raw_status
        terminal_at = _terminal_at(record)
        return cls(
            record_type=_record_kind(record),
            record_id=_record_id(record),
            status=status,
            terminal_at=terminal_at,
            personal=_bool(record, "personal", "contains_personal_data", default=True),
            unknown_outcome=_bool(record, "unknown_outcome", "unknown", default=False)
            or _record_status(record) == "unknown",
            unresolved_quarantine=unresolved_quarantine,
            active=_bool(record, "active", "is_active", default=False)
            or status in NON_TERMINAL_STATUSES,
            payload=cast(
                Mapping[str, Any] | None,
                _value(record, "payload", "payload_json", default=None),
            ),
            dedupe_key=cast(
                str | None,
                _value(record, "dedupe_key", "logical_dedupe_key", default=None),
            ),
            expires_at=_expiry_at(record),
            last_alert_at=_timestamp(
                _value(record, "last_alert_at", "alerted_at", "last_notified_at")
            ),
        )


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    """A safe, bounded operation selected for a transaction."""

    record_type: str
    record_id: str
    action: CleanupAction
    eligible_at: datetime
    reason: str
    scrub_fields: tuple[str, ...] = ()
    preserve_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QuarantineAlert:
    record_type: str
    record_id: str
    unresolved_since: datetime
    due_at: datetime
    reason: str = "quarantine unresolved for 30 days"
    last_alert_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    candidates: tuple[CleanupCandidate, ...] = ()
    quarantine_alerts: tuple[QuarantineAlert, ...] = ()
    expired_ephemeral: tuple[CleanupCandidate, ...] = ()

    @property
    def empty(self) -> bool:
        return (
            not self.candidates
            and not self.quarantine_alerts
            and not self.expired_ephemeral
        )


def _scrub_fields(record_type: str) -> tuple[str, ...]:
    """Fields that can contain direct Telegram/requester personal data."""

    common = (
        "user_id",
        "actor_user_id",
        "actor_chat_id",
        "requester_user_id",
        "requester_id",
        "chat_id",
        "destination",
        "message_id",
        "request_id",
        "subscription_id",
        "dedupe_key",
        "username",
        "display_name",
        "requester_name",
        "requested_by_username",
        "raw_error",
        "last_error",
        "error_text",
        "error",
        "error_message",
        "error_detail",
        "response_body",
        "raw_response",
        "resolved_by",
        "payload",
        "payload_json",
        "requester_to_media",
        "subscription_user_id",
        "message",
        "caption",
        "text",
        "html",
        "immutable_payload",
        "raw_payload",
    )
    # Group/chunk snapshots are called out explicitly by the state-machine
    # contract because requester attribution can survive the delivery row.
    if record_type in {"notification_group", "group", "delivery_chunk", "chunk"}:
        return common + ("caption", "text", "html", "message", "immutable_payload")
    return common


def _preserve_keys(record: CleanupRecord) -> tuple[str, ...]:
    values = ["logical_dedupe_key"]
    # Plex item/tombstone identity and non-personal event generations remain
    # available to suppress a future rescan duplicate.
    if record.record_type in {
        "plex_item",
        "plex_generation",
        "delivery",
        "notification_group",
        "group",
        "chunk",
        "delivery_chunk",
    }:
        values.extend(("logical_unit_key", "tombstone_generation", "event_generation"))
    return tuple(dict.fromkeys(values))


def select_cleanup_candidates(
    records: Iterable[CleanupRecord | object],
    *,
    now: datetime | None = None,
    retention_days: int = RETENTION_DAYS,
    limit: int = 100,
) -> tuple[CleanupCandidate, ...]:
    """Select only personal terminal records older than the retention window.

    Unknown outcomes are explicitly non-terminal.  A quarantined row remains
    until resolved, even if its timestamp is older than 60 days.
    """

    if retention_days < 0 or limit <= 0:
        raise CleanupError("retention_days must be non-negative and limit positive")
    current = _utc(now)
    cutoff = current - timedelta(days=retention_days)
    normalized = tuple(
        row if isinstance(row, CleanupRecord) else CleanupRecord.from_record(row)
        for row in records
    )
    selected: list[CleanupCandidate] = []
    for row in normalized:
        if (
            not row.personal
            or row.unknown_outcome
            or row.unresolved_quarantine
            or row.active
        ):
            continue
        if (
            row.status not in TERMINAL_STATUSES
            or row.terminal_at is None
            or row.terminal_at > cutoff
        ):
            continue
        selected.append(
            CleanupCandidate(
                record_type=row.record_type,
                record_id=row.record_id,
                action=CleanupAction.SCRUB,
                eligible_at=row.terminal_at + timedelta(days=retention_days),
                reason=f"terminal for at least {retention_days} days",
                scrub_fields=_scrub_fields(row.record_type),
                preserve_keys=_preserve_keys(row),
            )
        )
    selected.sort(key=lambda item: (item.record_type, item.record_id))
    return tuple(selected[:limit])


def select_quarantine_alerts(
    records: Iterable[CleanupRecord | object],
    *,
    now: datetime | None = None,
    alert_after_days: int = QUARANTINE_ALERT_DAYS,
    repeat_after_days: int = QUARANTINE_ALERT_REPEAT_DAYS,
    limit: int = 100,
) -> tuple[QuarantineAlert, ...]:
    """Select unresolved quarantine diagnostics without deleting them."""

    if alert_after_days < 0 or repeat_after_days < 0 or limit <= 0:
        raise CleanupError("alert/repeat days must be non-negative and limit positive")
    current = _utc(now)
    threshold = current - timedelta(days=alert_after_days)
    alerts: list[QuarantineAlert] = []
    for raw in records:
        row = raw if isinstance(raw, CleanupRecord) else CleanupRecord.from_record(raw)
        if (
            not row.unresolved_quarantine
            or row.terminal_at is None
            or row.terminal_at > threshold
            or (
                row.last_alert_at is not None
                and row.last_alert_at + timedelta(days=repeat_after_days) > current
            )
        ):
            continue
        due_at = (
            row.last_alert_at + timedelta(days=repeat_after_days)
            if row.last_alert_at is not None
            else row.terminal_at + timedelta(days=alert_after_days)
        )
        alerts.append(
            QuarantineAlert(
                record_type=row.record_type,
                record_id=row.record_id,
                unresolved_since=row.terminal_at,
                due_at=due_at,
                last_alert_at=row.last_alert_at,
            )
        )
    alerts.sort(key=lambda item: (item.due_at, item.record_type, item.record_id))
    return tuple(alerts[:limit])


def select_expired_ephemeral(
    records: Iterable[CleanupRecord | object],
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> tuple[CleanupCandidate, ...]:
    """Select expired candidate/cursor/nonce/confirmation/session records."""

    if limit <= 0:
        raise CleanupError("limit must be positive")
    current = _utc(now)
    result: list[CleanupCandidate] = []
    for raw in records:
        row = raw if isinstance(raw, CleanupRecord) else CleanupRecord.from_record(raw)
        if (
            row.record_type not in EPHEMERAL_KINDS
            or row.expires_at is None
            or row.expires_at > current
        ):
            continue
        result.append(
            CleanupCandidate(
                record_type=row.record_type,
                record_id=row.record_id,
                action=CleanupAction.EXPIRE,
                eligible_at=current,
                reason="ephemeral record expired",
            )
        )
    result.sort(key=lambda item: (item.record_type, item.record_id))
    return tuple(result[:limit])


def plan_cleanup(
    records: Iterable[CleanupRecord | object],
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> CleanupPlan:
    """Build one bounded cleanup/alert batch for a worker transaction."""

    rows = tuple(records)
    return CleanupPlan(
        candidates=select_cleanup_candidates(rows, now=now, limit=limit),
        quarantine_alerts=select_quarantine_alerts(rows, now=now, limit=limit),
        expired_ephemeral=select_expired_ephemeral(rows, now=now, limit=limit),
    )


_PERSONAL_KEYS = frozenset(
    {
        "user_id",
        "actor_user_id",
        "actor_chat_id",
        "requester_user_id",
        "requester_id",
        "chat_id",
        "destination",
        "message_id",
        "request_id",
        "subscription_id",
        "dedupe_key",
        "username",
        "display_name",
        "requester_name",
        "requested_by_username",
        "raw_error",
        "last_error",
        "error_text",
        "error",
        "error_message",
        "error_detail",
        "response_body",
        "raw_response",
        "resolved_by",
        "requester_to_media",
        "subscription_user_id",
        "message",
        "caption",
        "text",
        "html",
        "immutable_payload",
        "raw_payload",
    }
)


def scrub_payload(
    payload: Mapping[str, Any] | None,
    *,
    preserve_keys: Sequence[str] = (
        "logical_dedupe_key",
        "tombstone_generation",
        "event_generation",
    ),
) -> dict[str, Any] | None:
    """Return a non-mutating scrubbed snapshot for terminal payload cleanup.

    This helper is intentionally conservative: direct personal keys are
    removed, nested mappings/lists are traversed, and stable non-personal
    generation/dedupe values survive.  It is a payload scrubber, not an
    authorization/redaction boundary for live responses.
    """

    if payload is None:
        return None
    preserve = {_normalized_field_key(value) for value in preserve_keys}

    def visit(value: Any, key: str | None = None) -> Any:
        normalized_key = _normalized_field_key(key) if key is not None else None
        if (
            normalized_key is not None
            and (
                normalized_key in _PERSONAL_KEYS
                or is_secret_key(normalized_key)
                or is_path_key(normalized_key)
                or is_url_key(normalized_key)
            )
            and normalized_key not in preserve
        ):
            return None
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for raw_key, child in value.items():
                child_key = str(raw_key)
                normalized_child_key = _normalized_field_key(child_key)
                if (
                    normalized_child_key in _PERSONAL_KEYS
                    or is_secret_key(normalized_child_key)
                    or is_path_key(normalized_child_key)
                    or is_url_key(normalized_child_key)
                ) and normalized_child_key not in preserve:
                    continue
                scrubbed = visit(child, child_key)
                if scrubbed is not None:
                    result[child_key] = scrubbed
            return result
        if isinstance(value, list):
            return [
                child for child in (visit(item) for item in value) if child is not None
            ]
        if isinstance(value, tuple):
            return tuple(
                child for child in (visit(item) for item in value) if child is not None
            )
        if isinstance(value, str):
            return redact_text(value, max_bytes=4096)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        # Unknown objects/bytes are not safe to retain in a supposedly
        # scrubbed historical snapshot; dropping them is fail-closed.
        return None

    result = visit(payload)
    return result if isinstance(result, dict) else None


# Readable aliases used by transaction/repository layers.
select_terminal_cleanup = select_cleanup_candidates
select_quarantine_notifications = select_quarantine_alerts
scrub_terminal_payload = scrub_payload


__all__ = [
    "EPHEMERAL_KINDS",
    "NON_TERMINAL_STATUSES",
    "QUARANTINE_ALERT_DAYS",
    "QUARANTINE_ALERT_REPEAT_DAYS",
    "RETENTION_DAYS",
    "TERMINAL_STATUSES",
    "CleanupAction",
    "CleanupCandidate",
    "CleanupError",
    "CleanupPlan",
    "CleanupRecord",
    "QuarantineAlert",
    "plan_cleanup",
    "scrub_payload",
    "scrub_terminal_payload",
    "select_cleanup_candidates",
    "select_expired_ephemeral",
    "select_quarantine_alerts",
    "select_quarantine_notifications",
    "select_terminal_cleanup",
]
