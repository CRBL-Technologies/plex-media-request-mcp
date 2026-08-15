"""Pure delivery state machine and lease/leader primitives.

This module models the Telegram outbox boundary without making a network call
or writing SQLite.  A worker loads a :class:`DeliveryRecord`, invokes one of
the transition functions, and persists the returned record with a compare and
swap on ``version`` and ``claim_token``.  The state machine intentionally
refuses transitions that would make a transport ambiguity look like a safe
retry:

``claimed`` means no transmission has begun and an expired claim can be
reclaimed; only ``sending`` may become ``unknown``.  Unknown outcomes require
an explicit one-time administrative decision (assume sent or resend once).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import uuid4

from .models import DeliveryState, NotificationClass
from .planner import ClockRollbackError
from .redaction import redact_text

DEFAULT_LEASE_SECONDS = 60
DEFAULT_SEND_DEADLINE_SECONDS = 15
DEFAULT_COMMIT_MARGIN_SECONDS = 5
DEFAULT_SEND_GRACE_SECONDS = 15
MAX_RETRY_AFTER_SECONDS = 86_400
RETRY_DELAYS_SECONDS: tuple[int, ...] = (60, 300, 900, 3_600, 21_600, 86_400)
FAILURE_ALERT_DELAYS_SECONDS: tuple[int, ...] = (0, 86_400, 7 * 86_400)
MAX_ERROR_BYTES = 500


class DeliveryError(RuntimeError):
    """Base error for invalid delivery transitions."""


class DeliveryTransitionError(DeliveryError):
    """The requested state transition is not valid for this record."""


class ClaimConflictError(DeliveryTransitionError):
    """A stale/mismatched claim token or leader epoch was supplied."""


class ClaimExpiredError(DeliveryTransitionError):
    """A transition was attempted after the claim lease expired."""


class LeaseTooShortError(DeliveryTransitionError):
    """The remaining lease is shorter than the send deadline and margin."""


class NotDueError(DeliveryTransitionError):
    """A retry was claimed before its durable retry time."""


class ManualResolutionRequired(DeliveryTransitionError):
    """An ambiguous outcome cannot be automatically recovered."""


class GlobalDeliveryCircuitError(DeliveryError):
    """Telegram configuration/authentication is globally unavailable."""


class TelegramFailureClass(str, Enum):
    PRE_TRANSMISSION = "pre_transmission"
    RATE_LIMITED = "rate_limited"
    DESTINATION_BLOCKED = "destination_blocked"
    APPLICATION = "application"
    AMBIGUOUS = "ambiguous"
    AUTHENTICATION = "authentication"


def _utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise DeliveryError("delivery timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def _state(value: DeliveryState | str) -> DeliveryState:
    if isinstance(value, DeliveryState):
        return value
    try:
        return DeliveryState(value)
    except ValueError as exc:
        raise DeliveryTransitionError(f"invalid delivery state: {value!r}") from exc


def _safe_error(value: object | None) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return redact_text(text, max_bytes=MAX_ERROR_BYTES)


@dataclass(frozen=True, slots=True)
class Claim:
    """Opaque worker claim with both token and fencing epoch."""

    token: str
    version: int
    leader_epoch: int
    worker_id: str
    claimed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.token, str)
            or len(self.token.encode("utf-8")) < 32
            or len(self.token.encode("utf-8")) > 256
            or not self.token.strip()
            or not isinstance(self.worker_id, str)
            or not self.worker_id.strip()
            or len(self.worker_id.encode("utf-8")) > 256
        ):
            raise DeliveryError("claim token and worker_id are required")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or isinstance(self.leader_epoch, bool)
            or not isinstance(self.leader_epoch, int)
            or self.version < 0
            or self.leader_epoch < 0
        ):
            raise DeliveryError("claim version/epoch must be non-negative")
        object.__setattr__(self, "claimed_at", _utc(self.claimed_at))
        object.__setattr__(self, "expires_at", _utc(self.expires_at))
        if self.expires_at <= self.claimed_at:
            raise DeliveryError("claim expiry must follow claim time")

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= datetime.now(timezone.utc)


# Names used by callers that prefer the explicit terminology.
ClaimToken = Claim
DeliveryClaim = Claim


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """Immutable delivery row suitable for transactional CAS persistence."""

    delivery_id: str
    destination: str | int
    notification_class: NotificationClass | str
    idempotency_key: str
    state: DeliveryState | str = DeliveryState.PENDING
    version: int = 0
    attempts: int = 0
    pretransmission_failures: int = 0
    retry_due_at: datetime | None = None
    claim_token: str | None = None
    claim_version: int | None = None
    claim_epoch: int | None = None
    claim_worker: str | None = None
    claim_expires_at: datetime | None = None
    claimed_at: datetime | None = None
    sending_started_at: datetime | None = None
    send_deadline_at: datetime | None = None
    terminal_at: datetime | None = None
    unknown_at: datetime | None = None
    unknown_reason: str | None = None
    last_error_class: TelegramFailureClass | str | None = None
    last_error: str | None = None
    alert_count: int = 0
    last_alert_at: datetime | None = None
    recovery_generation: int = 0
    recovery_attempted: bool = False
    possible_duplicate: bool = False
    unknown_resolved: bool = False
    parent_delivery_id: str | None = None
    chunk_ordinal: int = 1
    chunk_count: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.delivery_id, str)
            or not self.delivery_id.strip()
            or not isinstance(self.idempotency_key, str)
            or not self.idempotency_key.strip()
            or len(self.delivery_id.encode("utf-8")) > 512
            or len(self.idempotency_key.encode("utf-8")) > 512
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in self.delivery_id)
            or any(
                ord(char) < 0x20 or ord(char) == 0x7F for char in self.idempotency_key
            )
        ):
            raise DeliveryError("delivery_id and idempotency_key are required")
        if isinstance(self.destination, bool) or not isinstance(
            self.destination, (str, int)
        ):
            raise DeliveryError("destination must be a string or integer")
        if isinstance(self.destination, str) and not self.destination.strip():
            raise DeliveryError("destination must not be blank")
        if isinstance(self.destination, str):
            object.__setattr__(self, "destination", self.destination.strip())
        if isinstance(self.destination, int) and self.destination == 0:
            raise DeliveryError("destination must be non-zero")
        raw_class = (
            self.notification_class.value
            if isinstance(self.notification_class, NotificationClass)
            else self.notification_class
        )
        try:
            object.__setattr__(self, "notification_class", NotificationClass(raw_class))
        except ValueError as exc:
            raise DeliveryError("notification_class is invalid") from exc
        object.__setattr__(self, "state", _state(self.state))
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.version,
                self.attempts,
                self.pretransmission_failures,
                self.alert_count,
                self.recovery_generation,
            )
        ):
            raise DeliveryError("delivery counters must be non-negative")
        if (
            isinstance(self.chunk_ordinal, bool)
            or not isinstance(self.chunk_ordinal, int)
            or isinstance(self.chunk_count, bool)
            or not isinstance(self.chunk_count, int)
            or self.chunk_ordinal <= 0
            or self.chunk_count <= 0
            or self.chunk_ordinal > self.chunk_count
        ):
            raise DeliveryError("invalid delivery chunk ordinal/count")
        for field_name in (
            "retry_due_at",
            "claim_expires_at",
            "claimed_at",
            "sending_started_at",
            "send_deadline_at",
            "terminal_at",
            "unknown_at",
            "last_alert_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _utc(value))
        if self.last_error_class is not None:
            raw_error_class = (
                self.last_error_class.value
                if isinstance(self.last_error_class, TelegramFailureClass)
                else self.last_error_class
            )
            try:
                parsed_error_class = TelegramFailureClass(raw_error_class)
            except ValueError as exc:
                raise DeliveryError("last_error_class is invalid") from exc
            object.__setattr__(self, "last_error_class", parsed_error_class)
        object.__setattr__(self, "last_error", _safe_error(self.last_error))
        object.__setattr__(self, "unknown_reason", _safe_error(self.unknown_reason))
        if (
            self.state
            in {
                DeliveryState.SENT,
                DeliveryState.ASSUMED_SENT,
                DeliveryState.FAILED,
                DeliveryState.ABANDONED,
                DeliveryState.DELIVERY_BLOCKED,
                DeliveryState.CANCELED,
                DeliveryState.SUPERSEDED,
            }
            and self.terminal_at is None
        ):
            # Keep records ergonomic for tests/repositories constructing a
            # terminal row directly; transition functions always set the time.
            object.__setattr__(
                self, "terminal_at", self.unknown_at or self.sending_started_at
            )

    @property
    def destination_key(self) -> str:
        return str(self.destination)

    @property
    def is_terminal(self) -> bool:
        return self.state in {
            DeliveryState.SENT,
            DeliveryState.ASSUMED_SENT,
            DeliveryState.FAILED,
            DeliveryState.ABANDONED,
            DeliveryState.DELIVERY_BLOCKED,
            DeliveryState.CANCELED,
            DeliveryState.SUPERSEDED,
        }

    @property
    def has_live_claim(self) -> bool:
        return self.claim_token is not None and self.claim_expires_at is not None

    @property
    def claim(self) -> Claim | None:
        if (
            self.claim_token is None
            or self.claim_version is None
            or self.claim_epoch is None
            or self.claim_worker is None
            or self.claimed_at is None
            or self.claim_expires_at is None
        ):
            return None
        return Claim(
            token=self.claim_token,
            version=self.claim_version,
            leader_epoch=self.claim_epoch,
            worker_id=self.claim_worker,
            claimed_at=self.claimed_at,
            expires_at=self.claim_expires_at,
        )


@dataclass(frozen=True, slots=True)
class LeaderLease:
    """Durable leader lease/fencing value."""

    owner: str
    epoch: int
    token: str
    claimed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.owner or not self.token or self.epoch <= 0:
            raise DeliveryError(
                "leader lease requires owner, token, and positive epoch"
            )
        object.__setattr__(self, "claimed_at", _utc(self.claimed_at))
        object.__setattr__(self, "expires_at", _utc(self.expires_at))


@dataclass
class LeaderEpoch:
    """One durable-ish epoch owner used to fence stale workers.

    The object is deliberately small enough to be mirrored by a row in an
    application transaction.  Callers persist ``epoch``/owner/expiry and pass
    the returned lease to claim transitions; a stale lease can never mutate a
    delivery even if its process survives.
    """

    epoch: int = 0
    owner: str | None = None
    token: str | None = None
    expires_at: datetime | None = None
    rollback_tolerance_seconds: int = 30

    def __post_init__(self) -> None:
        if (
            isinstance(self.epoch, bool)
            or not isinstance(self.epoch, int)
            or isinstance(self.rollback_tolerance_seconds, bool)
            or not isinstance(self.rollback_tolerance_seconds, int)
            or self.epoch < 0
            or self.rollback_tolerance_seconds < 0
        ):
            raise DeliveryError("leader epoch/tolerance must be non-negative")
        if self.expires_at is not None:
            self.expires_at = _utc(self.expires_at)

    def acquire(
        self,
        owner: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        clock: ClockGuard | None = None,
    ) -> LeaderLease:
        if not owner:
            raise DeliveryError("leader owner must not be blank")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
        ):
            raise DeliveryError("leader lease must be positive")
        current = _utc(now)
        if clock is not None:
            clock.observe(current)
        if (
            self.expires_at is not None
            and self.expires_at > current
            and self.owner not in {None, owner}
        ):
            raise ClaimConflictError("another leader epoch is live")
        self.epoch += 1
        self.owner = owner
        self.token = uuid4().hex
        self.expires_at = current + timedelta(seconds=lease_seconds)
        return LeaderLease(owner, self.epoch, self.token, current, self.expires_at)

    def renew(
        self,
        lease: LeaderLease,
        *,
        now: datetime | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        clock: ClockGuard | None = None,
    ) -> LeaderLease:
        current = _utc(now)
        self.assert_live(lease, now=current)
        if clock is not None:
            clock.observe(current)
        self.expires_at = current + timedelta(seconds=lease_seconds)
        return LeaderLease(
            lease.owner, lease.epoch, lease.token, lease.claimed_at, self.expires_at
        )

    def assert_live(self, lease: LeaderLease, *, now: datetime | None = None) -> None:
        current = _utc(now)
        if (
            lease.epoch != self.epoch
            or lease.owner != self.owner
            or lease.token != self.token
            or self.expires_at is None
            or self.expires_at <= current
        ):
            raise ClaimConflictError("leader epoch is stale or expired")

    def fence(self, lease: LeaderLease, *, now: datetime | None = None) -> bool:
        try:
            self.assert_live(lease, now=now)
        except ClaimConflictError:
            return False
        return True


@dataclass
class ClockGuard:
    """Reject wall-clock rollback before claims or activation transitions."""

    last_seen: datetime | None = None
    tolerance_seconds: int = 30
    blocked: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.tolerance_seconds, bool)
            or not isinstance(self.tolerance_seconds, int)
            or self.tolerance_seconds < 0
        ):
            raise DeliveryError("clock tolerance must be non-negative")
        if self.last_seen is not None:
            self.last_seen = _utc(self.last_seen)

    def observe(self, now: datetime | None = None) -> datetime:
        current = _utc(now)
        if self.last_seen is not None and current < self.last_seen - timedelta(
            seconds=self.tolerance_seconds
        ):
            self.blocked = True
            raise ClockRollbackError("wall clock rollback blocks claims and activation")
        if self.blocked:
            raise ClockRollbackError(
                "clock guard remains blocked until explicitly cleared"
            )
        if self.last_seen is None or current > self.last_seen:
            self.last_seen = current
        return current

    def assert_safe(self, now: datetime | None = None) -> datetime:
        return self.observe(now)

    def clear(self, now: datetime | None = None) -> None:
        """Clear a rollback only after an operator has established sane time."""

        current = _utc(now)
        self.last_seen = current
        self.blocked = False


def _require_claim(
    record: DeliveryRecord,
    token: Claim,
    *,
    leader_epoch: int | None = None,
    now: datetime,
    allow_expired: bool = False,
) -> None:
    if not isinstance(token, Claim):
        raise ClaimConflictError("versioned claim is required for this transition")
    if record.claim_token != token.token or record.claim_token is None:
        raise ClaimConflictError("claim token does not match current delivery claim")
    if token.version != record.claim_version:
        raise ClaimConflictError("claim version does not match current delivery claim")
    if token.leader_epoch != record.claim_epoch:
        raise ClaimConflictError("claim leader epoch is stale")
    if leader_epoch is not None and record.claim_epoch != leader_epoch:
        raise ClaimConflictError("claim leader epoch is stale")
    if not allow_expired and (
        record.claim_expires_at is None or record.claim_expires_at <= now
    ):
        raise ClaimExpiredError("delivery claim lease has expired")


def _clear_claim(
    record: DeliveryRecord, *, version_increment: int = 1
) -> DeliveryRecord:
    return replace(
        record,
        version=record.version + version_increment,
        claim_token=None,
        claim_version=None,
        claim_epoch=None,
        claim_worker=None,
        claim_expires_at=None,
        claimed_at=None,
    )


def _ready_for_claim(record: DeliveryRecord, now: datetime) -> None:
    if record.state is DeliveryState.RETRY_WAIT:
        if record.retry_due_at is not None and record.retry_due_at > now:
            raise NotDueError("delivery retry is not due")
        return
    if record.state is DeliveryState.PENDING:
        if record.retry_due_at is not None and record.retry_due_at > now:
            raise NotDueError("delivery is not due")
        return
    if (
        record.state is DeliveryState.CLAIMED
        and record.claim_expires_at is not None
        and record.claim_expires_at <= now
    ):
        return
    raise DeliveryTransitionError(
        f"delivery state {_state(record.state).value!r} cannot be claimed"
    )


def claim_delivery(
    record: DeliveryRecord,
    *,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    leader_epoch: int = 0,
    clock: ClockGuard | None = None,
    token: str | None = None,
) -> DeliveryRecord:
    """Atomically model claiming a pending/retry row with a UUID token."""

    if (
        not isinstance(worker_id, str)
        or not worker_id.strip()
        or len(worker_id.encode("utf-8")) > 256
    ):
        raise DeliveryError("worker_id must not be blank")
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or lease_seconds <= 0
    ):
        raise DeliveryError("lease_seconds must be positive")
    current = clock.observe(now) if clock is not None else _utc(now)
    if (
        isinstance(leader_epoch, bool)
        or not isinstance(leader_epoch, int)
        or leader_epoch < 0
    ):
        raise DeliveryError("leader_epoch must be non-negative")
    _ready_for_claim(record, current)
    if (
        record.state is DeliveryState.CLAIMED
        and record.claim_expires_at is not None
        and record.claim_expires_at > current
    ):
        raise ClaimConflictError("delivery already has a live claim")
    if token is not None and (
        not isinstance(token, str)
        or len(token.encode("utf-8")) < 32
        or not token.strip()
    ):
        raise DeliveryError("caller-supplied claim token must be a bounded UUID token")
    claim_token = token or secrets.token_urlsafe(32)
    expiry = current + timedelta(seconds=lease_seconds)
    next_version = record.version + 1
    return replace(
        record,
        state=DeliveryState.CLAIMED,
        version=next_version,
        claim_token=claim_token,
        claim_version=next_version,
        claim_epoch=leader_epoch,
        claim_worker=worker_id,
        claimed_at=current,
        claim_expires_at=expiry,
        retry_due_at=None,
    )


claim = claim_delivery
claim_row = claim_delivery


def renew_claim(
    record: DeliveryRecord,
    token: Claim,
    *,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    leader_epoch: int | None = None,
    clock: ClockGuard | None = None,
) -> DeliveryRecord:
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or lease_seconds <= 0
    ):
        raise DeliveryError("lease_seconds must be positive")
    current = clock.observe(now) if clock is not None else _utc(now)
    if record.state not in {DeliveryState.CLAIMED, DeliveryState.SENDING}:
        raise DeliveryTransitionError("only claimed/sending work can renew")
    _require_claim(record, token, leader_epoch=leader_epoch, now=current)
    next_version = record.version + 1
    return replace(
        record,
        version=next_version,
        claim_version=next_version,
        claim_expires_at=current + timedelta(seconds=lease_seconds),
    )


def begin_sending(
    record: DeliveryRecord,
    token: Claim,
    *,
    now: datetime | None = None,
    send_deadline_seconds: int = DEFAULT_SEND_DEADLINE_SECONDS,
    commit_margin_seconds: int = DEFAULT_COMMIT_MARGIN_SECONDS,
    leader_epoch: int | None = None,
    clock: ClockGuard | None = None,
) -> DeliveryRecord:
    """Enter ``sending`` only when the lease can cover HTTP + commit margin."""

    if send_deadline_seconds <= 0 or commit_margin_seconds < 0:
        raise DeliveryError("send deadline/margin is invalid")
    current = clock.observe(now) if clock is not None else _utc(now)
    if record.state is not DeliveryState.CLAIMED:
        raise DeliveryTransitionError("only claimed work can enter sending")
    _require_claim(record, token, leader_epoch=leader_epoch, now=current)
    assert record.claim_expires_at is not None
    required = timedelta(seconds=send_deadline_seconds + commit_margin_seconds)
    if record.claim_expires_at - current < required:
        raise LeaseTooShortError(
            "claim lease is shorter than send deadline plus commit margin"
        )
    deadline = current + timedelta(seconds=send_deadline_seconds)
    return replace(
        record,
        state=DeliveryState.SENDING,
        version=record.version + 1,
        attempts=record.attempts + 1,
        sending_started_at=current,
        send_deadline_at=deadline,
    )


start_sending = begin_sending


def mark_sent(
    record: DeliveryRecord,
    token: Claim,
    *,
    now: datetime | None = None,
    leader_epoch: int | None = None,
    clock: ClockGuard | None = None,
    grace_seconds: int = DEFAULT_SEND_GRACE_SECONDS,
) -> DeliveryRecord:
    if grace_seconds < 0:
        raise DeliveryError("grace_seconds cannot be negative")
    current = clock.observe(now) if clock is not None else _utc(now)
    if record.state is not DeliveryState.SENDING:
        raise DeliveryTransitionError("only sending work can become sent")
    _require_claim(record, token, leader_epoch=leader_epoch, now=current)
    if (
        record.send_deadline_at is not None
        and current > record.send_deadline_at + timedelta(seconds=grace_seconds)
    ):
        raise DeliveryTransitionError(
            "send result arrived after the commit grace period"
        )
    return replace(
        _clear_claim(record),
        state=DeliveryState.SENT,
        terminal_at=current,
        last_error=None,
        last_error_class=None,
    )


complete = mark_sent
complete_delivery = mark_sent


def release_claim(
    record: DeliveryRecord,
    token: Claim,
    *,
    now: datetime | None = None,
    leader_epoch: int | None = None,
    retry_due_at: datetime | None = None,
    error: str | None = None,
) -> DeliveryRecord:
    """Release a claim when no transmission started.

    A released claim remains retryable.  It can never become ``unknown`` from
    ``claimed``; transport ambiguity begins only after ``begin_sending``.
    """

    current = _utc(now)
    if record.state is not DeliveryState.CLAIMED:
        raise DeliveryTransitionError("only a claimed pre-send row can be released")
    _require_claim(record, token, leader_epoch=leader_epoch, now=current)
    next_retry = _utc(retry_due_at) if retry_due_at is not None else current
    released = _clear_claim(record)
    return replace(
        released,
        state=DeliveryState.PENDING,
        retry_due_at=next_retry,
        last_error=error,
        last_error_class=TelegramFailureClass.PRE_TRANSMISSION if error else None,
    )


def fail_before_transmission(
    record: DeliveryRecord,
    token: Claim,
    *,
    now: datetime | None = None,
    error: str | None = None,
    error_class: TelegramFailureClass | str = TelegramFailureClass.PRE_TRANSMISSION,
    retry_after_seconds: int | None = None,
    leader_epoch: int | None = None,
) -> DeliveryRecord:
    """Schedule a proven pre-transmission failure or exhaust into ``failed``."""

    current = _utc(now)
    if record.state not in {DeliveryState.CLAIMED, DeliveryState.SENDING}:
        raise DeliveryTransitionError(
            "pre-transmission/application failure requires claimed or sending state"
        )
    _require_claim(record, token, leader_epoch=leader_epoch, now=current)
    failure_count = record.pretransmission_failures + 1
    raw_class = (
        error_class.value
        if isinstance(error_class, TelegramFailureClass)
        else error_class
    )
    try:
        parsed_class = TelegramFailureClass(raw_class)
    except ValueError as exc:
        raise DeliveryError("unknown Telegram failure class") from exc
    if retry_after_seconds is not None:
        if retry_after_seconds < 0:
            raise DeliveryError("retry_after_seconds cannot be negative")
        delay_seconds = min(retry_after_seconds, MAX_RETRY_AFTER_SECONDS)
    elif failure_count <= len(RETRY_DELAYS_SECONDS):
        delay_seconds = RETRY_DELAYS_SECONDS[failure_count - 1]
    else:
        delay_seconds = 0
    released = _clear_claim(record)
    if (
        not record.recovery_attempted
        and parsed_class
        in {
            TelegramFailureClass.PRE_TRANSMISSION,
            TelegramFailureClass.RATE_LIMITED,
        }
        and failure_count <= len(RETRY_DELAYS_SECONDS)
    ):
        return replace(
            released,
            state=DeliveryState.RETRY_WAIT,
            pretransmission_failures=failure_count,
            retry_due_at=current + timedelta(seconds=delay_seconds),
            last_error=_safe_error(error),
            last_error_class=parsed_class,
        )
    return replace(
        released,
        state=DeliveryState.FAILED,
        pretransmission_failures=(
            failure_count
            if parsed_class
            in {
                TelegramFailureClass.PRE_TRANSMISSION,
                TelegramFailureClass.RATE_LIMITED,
            }
            else record.pretransmission_failures
        ),
        retry_due_at=None,
        terminal_at=current,
        last_error=_safe_error(error),
        last_error_class=parsed_class,
    )


pretransmission_failure = fail_before_transmission
retryable_failure = fail_before_transmission


def mark_unknown(
    record: DeliveryRecord,
    token: Claim,
    *,
    now: datetime | None = None,
    reason: str,
    leader_epoch: int | None = None,
    clock: ClockGuard | None = None,
    require_deadline: bool = False,
    grace_seconds: int = DEFAULT_SEND_GRACE_SECONDS,
) -> DeliveryRecord:
    """Record an ambiguous outcome; only ``sending`` may become unknown."""

    if not reason or not isinstance(reason, str):
        raise DeliveryError("unknown outcome requires a bounded reason")
    if grace_seconds < 0:
        raise DeliveryError("grace_seconds cannot be negative")
    current = clock.observe(now) if clock is not None else _utc(now)
    if record.state is not DeliveryState.SENDING:
        raise DeliveryTransitionError("only sending work can become unknown")
    _require_claim(
        record, token, leader_epoch=leader_epoch, now=current, allow_expired=True
    )
    if (
        require_deadline
        and record.send_deadline_at is not None
        and current < record.send_deadline_at + timedelta(seconds=grace_seconds)
    ):
        raise DeliveryTransitionError(
            "sending claim is still within its response grace period"
        )
    # Clear the live claim.  An unknown row is intentionally not claimable by
    # another worker, even when a process restarted.
    return replace(
        _clear_claim(record),
        state=DeliveryState.UNKNOWN,
        unknown_at=current,
        unknown_reason=_safe_error(reason),
        last_error=_safe_error(reason),
        last_error_class=TelegramFailureClass.AMBIGUOUS,
    )


ambiguous_failure = mark_unknown
mark_ambiguous = mark_unknown


def expire_claim(
    record: DeliveryRecord,
    *,
    now: datetime | None = None,
    grace_seconds: int = DEFAULT_SEND_GRACE_SECONDS,
    clock: ClockGuard | None = None,
) -> DeliveryRecord:
    """Resolve an expired lease without stealing an in-flight transmission."""

    if grace_seconds < 0:
        raise DeliveryError("grace_seconds cannot be negative")
    current = clock.observe(now) if clock is not None else _utc(now)
    if record.state is DeliveryState.SENDING:
        if record.claim_expires_at is None:
            return record
    elif record.claim_expires_at is None or record.claim_expires_at > current:
        return record
    if record.state is DeliveryState.CLAIMED:
        # No request was started, so this is safe to put back in the queue.
        released = _clear_claim(record)
        return replace(released, state=DeliveryState.PENDING, retry_due_at=current)
    if record.state is DeliveryState.SENDING:
        deadline = record.send_deadline_at or record.claim_expires_at
        assert deadline is not None
        if deadline + timedelta(seconds=grace_seconds) > current:
            return record
        # Calling mark_unknown here would require the old token and can be
        # rejected by an adapter that has already fenced it.  This transition
        # still records only a sending-origin unknown and clears the claim.
        released = _clear_claim(record)
        return replace(
            released,
            state=DeliveryState.UNKNOWN,
            unknown_at=current,
            unknown_reason=_safe_error("sending deadline expired after grace period"),
            last_error=_safe_error("sending deadline expired after grace period"),
            last_error_class=TelegramFailureClass.AMBIGUOUS,
        )
    return record


reclaim_expired_claim = expire_claim


def assume_sent(
    record: DeliveryRecord,
    *,
    confirmed: bool = False,
    now: datetime | None = None,
) -> DeliveryRecord:
    """Resolve an unknown chunk as terminal ``assumed_sent``."""

    if not confirmed:
        raise ManualResolutionRequired("assume_sent requires explicit confirmation")
    if record.state is not DeliveryState.UNKNOWN:
        raise DeliveryTransitionError("only unknown work can be assumed sent")
    if record.unknown_resolved:
        raise DeliveryTransitionError("unknown outcome already resolved")
    current = _utc(now)
    return replace(
        record,
        state=DeliveryState.ASSUMED_SENT,
        version=record.version + 1,
        terminal_at=current,
        unknown_resolved=True,
        claim_token=None,
        claim_version=None,
        claim_epoch=None,
        claim_worker=None,
        claim_expires_at=None,
        claimed_at=None,
    )


@dataclass(frozen=True, slots=True)
class ResendRecovery:
    """The old resolved unknown and its one possible-duplicate retry."""

    previous: DeliveryRecord
    retry: DeliveryRecord


def resend_once(
    record: DeliveryRecord,
    *,
    confirmed: bool = False,
    now: datetime | None = None,
) -> ResendRecovery:
    """Create exactly one manually confirmed possible-duplicate generation."""

    if not confirmed:
        raise ManualResolutionRequired("resend_once requires explicit confirmation")
    if record.state is not DeliveryState.UNKNOWN:
        raise DeliveryTransitionError("only unknown work can be resent once")
    if record.unknown_resolved:
        raise DeliveryTransitionError("unknown outcome already resolved")
    current = _utc(now)
    previous = replace(
        record,
        state=DeliveryState.SUPERSEDED,
        version=record.version + 1,
        terminal_at=current,
        unknown_resolved=True,
        last_error_class=TelegramFailureClass.AMBIGUOUS,
    )
    retry = DeliveryRecord(
        delivery_id=f"{record.delivery_id}:resend:{record.recovery_generation + 1}",
        destination=record.destination,
        notification_class=record.notification_class,
        idempotency_key=f"{record.idempotency_key}:resend:{record.recovery_generation + 1}",
        state=DeliveryState.PENDING,
        version=0,
        recovery_generation=record.recovery_generation + 1,
        recovery_attempted=True,
        possible_duplicate=True,
        parent_delivery_id=record.delivery_id,
        chunk_ordinal=record.chunk_ordinal,
        chunk_count=record.chunk_count,
    )
    return ResendRecovery(previous=previous, retry=retry)


resolve_unknown_resend = resend_once


def retry_failed_once(
    record: DeliveryRecord,
    *,
    confirmed: bool = False,
    now: datetime | None = None,
) -> DeliveryRecord:
    """Create one audited recovery generation for a terminal failed row."""

    if not confirmed:
        raise ManualResolutionRequired("retry_once requires explicit confirmation")
    if record.state is not DeliveryState.FAILED:
        raise DeliveryTransitionError("retry_once requires failed state")
    if record.recovery_attempted:
        raise DeliveryTransitionError("failed delivery recovery was already used")
    current = _utc(now)
    return replace(
        record,
        state=DeliveryState.PENDING,
        version=record.version + 1,
        retry_due_at=current,
        pretransmission_failures=0,
        recovery_generation=record.recovery_generation + 1,
        recovery_attempted=True,
        terminal_at=None,
        last_error=None,
        last_error_class=None,
        alert_count=0,
        last_alert_at=None,
    )


retry_once = retry_failed_once


def mark_abandoned(
    record: DeliveryRecord,
    *,
    confirmed: bool = False,
    now: datetime | None = None,
) -> DeliveryRecord:
    """Terminally abandon a failed delivery without fulfilling a requester."""

    if not confirmed:
        raise ManualResolutionRequired("mark_abandoned requires explicit confirmation")
    if record.state is not DeliveryState.FAILED:
        raise DeliveryTransitionError("only failed work can be abandoned")
    current = _utc(now)
    return replace(
        record,
        state=DeliveryState.ABANDONED,
        version=record.version + 1,
        terminal_at=current,
        retry_due_at=None,
    )


abandon = mark_abandoned


def block_destination(
    record: DeliveryRecord,
    token: Claim,
    *,
    reason: str,
    now: datetime | None = None,
    leader_epoch: int | None = None,
) -> DeliveryRecord:
    """Stop retries only for a recognized destination-terminal error."""

    if not reason:
        raise DeliveryError("destination block requires a reason")
    if not isinstance(token, Claim):
        raise ClaimConflictError("versioned claim is required to block a destination")
    if record.state in {
        DeliveryState.SENT,
        DeliveryState.ASSUMED_SENT,
        DeliveryState.ABANDONED,
        DeliveryState.SUPERSEDED,
        DeliveryState.UNKNOWN,
    }:
        raise DeliveryTransitionError("unknown/terminal row requires explicit recovery")
    current = _utc(now)
    _require_claim(record, token, leader_epoch=leader_epoch, now=current)
    return replace(
        _clear_claim(record) if record.claim_token is not None else record,
        state=DeliveryState.DELIVERY_BLOCKED,
        version=record.version + 1,
        terminal_at=current,
        retry_due_at=None,
        last_error=_safe_error(reason),
        last_error_class=TelegramFailureClass.DESTINATION_BLOCKED,
    )


def pause_global_circuit(
    record: DeliveryRecord,
    token: Claim,
    *,
    reason: str,
    now: datetime | None = None,
    retry_after_seconds: int | None = None,
    leader_epoch: int | None = None,
) -> DeliveryRecord:
    """Keep destination work retryable while global Telegram auth is fixed."""

    if not reason:
        raise DeliveryError("global circuit pause requires a reason")
    if record.state not in {DeliveryState.CLAIMED, DeliveryState.SENDING}:
        raise DeliveryTransitionError(
            "global circuit pause requires active claimed work"
        )
    current = _utc(now)
    _require_claim(record, token, leader_epoch=leader_epoch, now=current)
    delay = 60 if retry_after_seconds is None else retry_after_seconds
    if delay < 0:
        raise DeliveryError("retry_after_seconds cannot be negative")
    delay = min(delay, MAX_RETRY_AFTER_SECONDS)
    return replace(
        _clear_claim(record),
        state=DeliveryState.RETRY_WAIT,
        retry_due_at=current + timedelta(seconds=delay),
        last_error=_safe_error(reason),
        last_error_class=TelegramFailureClass.AUTHENTICATION,
    )


def fail_application(
    record: DeliveryRecord,
    token: Claim,
    *,
    reason: str | None = None,
    now: datetime | None = None,
    leader_epoch: int | None = None,
) -> DeliveryRecord:
    """Terminally quarantine one immutable payload without disabling its chat."""

    return fail_before_transmission(
        record,
        token,
        now=now,
        error=reason,
        error_class=TelegramFailureClass.APPLICATION,
        leader_epoch=leader_epoch,
    )


def classify_telegram_error(
    *,
    status_code: int | None = None,
    error_code: str | None = None,
    transmitted: bool = False,
    retry_after_seconds: int | None = None,
) -> tuple[TelegramFailureClass, int | None]:
    """Classify a bounded Telegram response without retaining its raw body."""

    code = (error_code or "").lower()
    if code in {"bot_blocked", "bot_kicked", "chat_not_found", "user_deactivated"}:
        return TelegramFailureClass.DESTINATION_BLOCKED, None
    if (
        code in {"invalid_token", "unauthorized", "authentication_failed"}
        or status_code in {401, 403}
        and "chat" not in code
    ):
        return TelegramFailureClass.AUTHENTICATION, None
    if status_code == 429 or code in {"rate_limited", "too_many_requests"}:
        bounded = (
            None
            if retry_after_seconds is None
            else min(max(retry_after_seconds, 0), MAX_RETRY_AFTER_SECONDS)
        )
        return TelegramFailureClass.RATE_LIMITED, bounded
    if transmitted or status_code in {408, 499, 500, 502, 503, 504}:
        return TelegramFailureClass.AMBIGUOUS, None
    return TelegramFailureClass.APPLICATION, None


def process_telegram_failure(
    record: DeliveryRecord,
    token: Claim,
    *,
    failure_class: TelegramFailureClass | str,
    now: datetime | None = None,
    error: str | None = None,
    retry_after_seconds: int | None = None,
    leader_epoch: int | None = None,
) -> DeliveryRecord:
    """Apply a classified response at the send boundary."""

    raw_class = (
        failure_class.value
        if isinstance(failure_class, TelegramFailureClass)
        else failure_class
    )
    try:
        parsed = TelegramFailureClass(raw_class)
    except ValueError as exc:
        raise DeliveryError("unknown Telegram failure class") from exc
    if parsed is TelegramFailureClass.DESTINATION_BLOCKED:
        return block_destination(
            record,
            token,
            reason=error or parsed.value,
            now=now,
            leader_epoch=leader_epoch,
        )
    if parsed is TelegramFailureClass.AUTHENTICATION:
        return pause_global_circuit(
            record,
            token,
            reason=error or "Telegram authentication/configuration failure",
            now=now,
            retry_after_seconds=retry_after_seconds,
            leader_epoch=leader_epoch,
        )
    if parsed is TelegramFailureClass.AMBIGUOUS:
        return mark_unknown(
            record,
            token,
            now=now,
            reason=error or parsed.value,
            leader_epoch=leader_epoch,
        )
    if parsed in {
        TelegramFailureClass.PRE_TRANSMISSION,
        TelegramFailureClass.RATE_LIMITED,
    }:
        return fail_before_transmission(
            record,
            token,
            now=now,
            error=error,
            error_class=parsed,
            retry_after_seconds=retry_after_seconds,
            leader_epoch=leader_epoch,
        )
    # An HTTP 400/rendering error is terminal to this payload but not to the
    # destination.  It is modeled as failed so an operator can quarantine or
    # repair the immutable payload explicitly.
    return fail_application(
        record,
        token,
        now=now,
        reason=error or parsed.value,
        leader_epoch=leader_epoch,
    )


def alert_due(
    record: DeliveryRecord,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether the next failure reminder is due (at most immediate + 2)."""

    if record.state is not DeliveryState.FAILED or record.terminal_at is None:
        return False
    if record.alert_count >= len(FAILURE_ALERT_DELAYS_SECONDS):
        return False
    current = _utc(now)
    due = record.terminal_at + timedelta(
        seconds=FAILURE_ALERT_DELAYS_SECONDS[record.alert_count]
    )
    return current >= due


def record_alert(
    record: DeliveryRecord, *, now: datetime | None = None
) -> DeliveryRecord:
    if not alert_due(record, now=now):
        raise DeliveryTransitionError("delivery failure alert is not due")
    current = _utc(now)
    return replace(
        record,
        alert_count=record.alert_count + 1,
        last_alert_at=current,
        version=record.version + 1,
    )


def next_retry_at(record: DeliveryRecord) -> datetime | None:
    return record.retry_due_at


def retry_schedule() -> tuple[timedelta, ...]:
    return tuple(timedelta(seconds=seconds) for seconds in RETRY_DELAYS_SECONDS)


__all__ = [
    "DEFAULT_COMMIT_MARGIN_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_SEND_DEADLINE_SECONDS",
    "DEFAULT_SEND_GRACE_SECONDS",
    "FAILURE_ALERT_DELAYS_SECONDS",
    "MAX_RETRY_AFTER_SECONDS",
    "MAX_ERROR_BYTES",
    "RETRY_DELAYS_SECONDS",
    "Claim",
    "ClaimConflictError",
    "ClaimExpiredError",
    "ClaimToken",
    "ClockGuard",
    "ClockRollbackError",
    "DeliveryClaim",
    "DeliveryError",
    "DeliveryRecord",
    "DeliveryState",
    "DeliveryTransitionError",
    "GlobalDeliveryCircuitError",
    "LeaderEpoch",
    "LeaderLease",
    "LeaseTooShortError",
    "ManualResolutionRequired",
    "NotDueError",
    "ResendRecovery",
    "TelegramFailureClass",
    "abandon",
    "alert_due",
    "ambiguous_failure",
    "assume_sent",
    "begin_sending",
    "block_destination",
    "claim",
    "claim_delivery",
    "classify_telegram_error",
    "complete",
    "complete_delivery",
    "expire_claim",
    "fail_before_transmission",
    "fail_application",
    "mark_abandoned",
    "mark_ambiguous",
    "mark_sent",
    "mark_unknown",
    "next_retry_at",
    "pretransmission_failure",
    "process_telegram_failure",
    "pause_global_circuit",
    "reclaim_expired_claim",
    "record_alert",
    "release_claim",
    "renew_claim",
    "resend_once",
    "resolve_unknown_resend",
    "retry_failed_once",
    "retry_once",
    "retry_schedule",
    "retryable_failure",
    "start_sending",
]
