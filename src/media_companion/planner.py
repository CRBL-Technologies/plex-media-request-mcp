"""Pure planning primitives for Plex availability notifications.

The planner deliberately does not open a database or call Plex/Telegram.  A
resolver supplies verified :class:`CanonicalUnit` records and the caller
persists the returned groups, obligations, and accounting result in one
transaction.  Keeping the decision logic here makes the linearisation points
explicit and makes it possible to exercise the failure matrix without a live
Plex or Telegram service.

There are three details worth calling out:

* activation is start-time aware.  A pass-one member is historical; only a
  pass-two-only item with a verified ``addedAt`` strictly after
  ``baseline_started_at`` can be classified as new;
* a TV group's deadline never slides.  An item at exactly ``due_at`` starts a
  new generation; and
* accounting is performed at obligation grain, not by subtracting delivery
  rows from media rows.  An admin-winning requester obligation is represented
  explicitly as ``suppressed`` and linked to the admin obligation.

All public functions accept ordinary mappings in addition to the typed
records.  This is intentional: transaction/repository code can pass a row
mapping without leaking a sqlite dependency into this module.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from .models import MediaType, NotificationClass, RequestMode

if TYPE_CHECKING:
    from .delivery import ClockGuard, LeaderEpoch, LeaderLease

GROUP_WINDOW_SECONDS = 300
GROUP_WINDOW = timedelta(seconds=GROUP_WINDOW_SECONDS)
MAX_RENDER_FIELD_BYTES = 512
MAX_RENDER_URL_BYTES = 2048
MAX_RENDER_MESSAGE_BYTES = 4096
ObligationKey = tuple[str, str, str, int | str | None]


class PlanningError(ValueError):
    """Base error raised for an invalid planning input."""


class ActivationError(PlanningError):
    """The activation baseline cannot safely be classified."""


class IncompleteScanError(ActivationError):
    """A paginated pass lacks complete/integrity evidence."""


class ClockRollbackError(PlanningError):
    """The wall clock moved backwards beyond the configured tolerance."""


class ActivationDisposition(str, Enum):
    HISTORICAL = "historical"
    NEW = "new"
    QUARANTINED = "quarantined"


class ObligationState(str, Enum):
    """States understood by the no-loss oracle.

    ``suppressed`` is a real accounting state: the requester obligation is
    linked to an admin-class delivery for the same destination and must only
    advance when that winning delivery resolves successfully.
    """

    PENDING = "pending"
    READY = "ready"
    CLAIMED = "claimed"
    SENDING = "sending"
    RETRY_WAIT = "retry_wait"
    SENT = "sent"
    ASSUMED_SENT = "assumed_sent"
    UNKNOWN = "unknown"
    FAILED = "failed"
    ABANDONED = "abandoned"
    DELIVERY_BLOCKED = "delivery_blocked"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"
    QUARANTINED = "quarantined"
    SUPPRESSED = "suppressed"


_ACCOUNTED_STATES = frozenset(state.value for state in ObligationState)


def _coerce_datetime(value: object, field_name: str) -> datetime:
    """Parse an aware UTC timestamp without accepting a local/naive time."""

    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError as exc:
            raise PlanningError(f"{field_name} must be an ISO timestamp") from exc
    else:
        raise PlanningError(f"{field_name} must be timezone-aware")
    if result.tzinfo is None or result.utcoffset() is None:
        raise PlanningError(f"{field_name} must be timezone-aware")
    return result.astimezone(timezone.utc)


def _optional_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _coerce_datetime(value, field_name)


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


def _text(value: object, field_name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise PlanningError(f"{field_name} must be a string")
    result = value.strip()
    if not result and not optional:
        raise PlanningError(f"{field_name} must not be blank")
    return result or None


def _bool_value(value: object, field_name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PlanningError(f"{field_name} must be a boolean")
    return value


def _int_value(value: object, field_name: str, *, optional: bool = True) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanningError(f"{field_name} must be an integer")
    return value


def _class_value(value: NotificationClass | str) -> str:
    return value.value if isinstance(value, NotificationClass) else value


def _iterable_values(value: object) -> Iterable[object]:
    if value is None or isinstance(value, (str, bytes, bytearray)):
        return ()
    if isinstance(value, Iterable):
        return value
    raise PlanningError("expected an iterable value")


def _destination_key(value: object, field_name: str = "destination") -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PlanningError(f"{field_name} must be a string or integer")
    if isinstance(value, int) and value == 0:
        raise PlanningError(f"{field_name} must be non-zero")
    result = str(value).strip()
    if not result:
        raise PlanningError(f"{field_name} must not be blank")
    return result


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    """The safe outcome of classifying one pass-two Plex identity."""

    disposition: ActivationDisposition
    reason: str
    logical_key: str | None = None

    @property
    def is_new(self) -> bool:
        return self.disposition is ActivationDisposition.NEW

    @property
    def quarantined(self) -> bool:
        return self.disposition is ActivationDisposition.QUARANTINED


@dataclass(frozen=True, slots=True)
class ActivationPass:
    """Evidence from one complete Plex identity-set traversal.

    ``keys`` is the de-duplicated identity set as persisted by the caller.
    ``seen_keys`` may contain the raw traversal order; when supplied, repeats
    are rejected.  A pass must explicitly declare itself complete.  Empty
    pages are valid, but an incomplete/invalid pass can never activate.
    """

    number: int
    keys: frozenset[str]
    complete: bool = False
    cursor_regressed: bool = False
    cursor_gap: bool = False
    count_mismatch: bool = False
    seen_keys: tuple[str, ...] = ()
    fresh_at: datetime | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.number, bool) or self.number not in (1, 2):
            raise ActivationError("activation pass number must be 1 or 2")
        for field_name in (
            "complete",
            "cursor_regressed",
            "cursor_gap",
            "count_mismatch",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ActivationError(f"{field_name} must be boolean")
        if any(not isinstance(key, str) or not key.strip() for key in self.keys):
            raise ActivationError("activation pass keys must be non-empty strings")
        normalized_keys = frozenset(key.strip() for key in self.keys)
        object.__setattr__(self, "keys", normalized_keys)
        if any(not isinstance(key, str) or not key.strip() for key in self.seen_keys):
            raise ActivationError("activation pass seen keys must be non-empty strings")
        normalized_seen = tuple(key.strip() for key in self.seen_keys)
        object.__setattr__(self, "seen_keys", normalized_seen)
        if normalized_seen and len(set(normalized_seen)) != len(normalized_seen):
            object.__setattr__(self, "cursor_gap", True)
        if self.fresh_at is not None:
            object.__setattr__(
                self, "fresh_at", _coerce_datetime(self.fresh_at, "fresh_at")
            )

    @property
    def valid(self) -> bool:
        return bool(
            self.complete
            and not self.cursor_regressed
            and not self.cursor_gap
            and not self.count_mismatch
            and self.error is None
            and self.fresh_at is not None
        )

    def require_valid(self) -> None:
        if not self.valid:
            reason = self.error or "pagination integrity evidence is incomplete"
            raise IncompleteScanError(reason)


@dataclass(frozen=True, slots=True)
class ActivationBaseline:
    """Two-pass activation state before delivery is enabled."""

    activation_id: str
    baseline_started_at: datetime
    baseline_completed_at: datetime | None = None
    pass_one: ActivationPass | None = None
    pass_two: ActivationPass | None = None
    activated: bool = False
    delivery_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.activation_id, str) or not self.activation_id.strip():
            raise ActivationError("activation_id must not be blank")
        object.__setattr__(self, "activation_id", self.activation_id.strip())
        if not isinstance(self.activated, bool) or not isinstance(
            self.delivery_enabled, bool
        ):
            raise ActivationError("activation flags must be boolean")
        object.__setattr__(
            self,
            "baseline_started_at",
            _coerce_datetime(self.baseline_started_at, "baseline_started_at"),
        )
        if self.baseline_completed_at is not None:
            object.__setattr__(
                self,
                "baseline_completed_at",
                _coerce_datetime(self.baseline_completed_at, "baseline_completed_at"),
            )
        if (
            self.baseline_completed_at is not None
            and self.baseline_completed_at < self.baseline_started_at
        ):
            raise ActivationError("baseline_completed_at precedes baseline_started_at")

    def record_pass(self, scan: ActivationPass) -> ActivationBaseline:
        if scan.number == 1:
            return replace(self, pass_one=scan)
        return replace(self, pass_two=scan)

    def activate(
        self,
        *,
        completed_at: datetime | None = None,
        clock: object | None = None,
    ) -> ActivationBaseline:
        if self.pass_one is None or self.pass_two is None:
            raise IncompleteScanError("both activation passes are required")
        if self.pass_one.number != 1 or self.pass_two.number != 2:
            raise IncompleteScanError("activation passes are out of order")
        self.pass_one.require_valid()
        self.pass_two.require_valid()
        finished = _coerce_datetime(
            completed_at or datetime.now(timezone.utc), "completed_at"
        )
        if clock is not None:
            observer = getattr(clock, "observe", None)
            if not callable(observer):
                raise ActivationError("clock guard does not provide observe()")
            observer(finished)
        if finished < self.baseline_started_at:
            raise ActivationError("activation completion precedes baseline start")
        for scan in (self.pass_one, self.pass_two):
            assert scan.fresh_at is not None
            if scan.fresh_at > finished:
                raise ActivationError("activation scan freshness is after completion")
        return replace(
            self,
            baseline_completed_at=finished,
            activated=True,
            delivery_enabled=False,
        )

    def enable_delivery(
        self,
        *,
        accounting: OracleResult | None = None,
        scan_complete: bool = False,
        full_sweep_complete: bool = False,
        scans_fresh: bool = False,
        leader: object | None = None,
        now: datetime | None = None,
    ) -> ActivationBaseline:
        if not self.activated or self.baseline_completed_at is None:
            raise ActivationError("activation must complete before delivery enablement")
        if any(
            not isinstance(flag, bool)
            for flag in (scan_complete, full_sweep_complete, scans_fresh)
        ):
            raise ActivationError("scan evidence flags must be boolean")
        if not scan_complete or not full_sweep_complete or not scans_fresh:
            raise ActivationError("fresh complete scan evidence is required")
        if accounting is None or not accounting.ready:
            raise ActivationError(
                "delivery accounting must be complete before enablement"
            )
        if leader is not None:
            checker = getattr(leader, "assert_live", None)
            if not callable(checker):
                checker = getattr(leader, "fence", None)
            if not callable(checker):
                raise ActivationError("leader lease cannot fence activation")
            current = _coerce_datetime(now or datetime.now(timezone.utc), "now")
            try:
                result = checker(now=current)
            except TypeError as exc:
                raise ActivationError(
                    "leader lease checker must be bound to the current lease"
                ) from exc
            if result is False:
                raise ActivationError("leader lease is not live")
        return replace(self, delivery_enabled=True)

    @property
    def historical_keys(self) -> frozenset[str]:
        if self.pass_one is None:
            return frozenset()
        return self.pass_one.keys


def _item_key(item: object) -> str | None:
    value = _value(
        item, "logical_key", "logical_unit_key", "unit_key", "key", "rating_key"
    )
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    return value.strip() or None


def classify_activation_item(
    item: object,
    *,
    baseline_started_at: datetime,
    pass_one_membership: Iterable[str] = (),
    added_at: datetime | str | None = None,
    coarse: bool | None = None,
    clock_ambiguous: bool | None = None,
) -> ActivationDecision:
    """Classify an item against an activation baseline.

    Pass-one membership wins and marks an item historical regardless of a
    later webhook timestamp.  For a pass-two-only item, ``addedAt`` is a
    mandatory trusted timestamp and the comparison is strictly ``>``.  A
    missing/invalid/coarse/equal/backdated/ambiguous timestamp is quarantined;
    no fallback to webhook receipt time or current time is allowed.
    """

    baseline = _coerce_datetime(baseline_started_at, "baseline_started_at")
    key = _item_key(item)
    pass_one = frozenset(str(value) for value in pass_one_membership)
    if key is None:
        return ActivationDecision(
            ActivationDisposition.QUARANTINED, "logical_key_missing", None
        )
    if key is not None and key in pass_one:
        return ActivationDecision(
            ActivationDisposition.HISTORICAL, "pass_one_member", key
        )

    raw_added = (
        added_at if added_at is not None else _value(item, "added_at", "addedAt")
    )
    raw_coarse = (
        coarse if coarse is not None else _value(item, "coarse", "added_at_coarse")
    )
    raw_ambiguous = (
        clock_ambiguous
        if clock_ambiguous is not None
        else _value(item, "clock_ambiguous", "added_at_ambiguous")
    )
    try:
        is_coarse = _bool_value(raw_coarse, "coarse", default=False)
        is_ambiguous = _bool_value(raw_ambiguous, "clock_ambiguous", default=False)
    except PlanningError as exc:
        return ActivationDecision(ActivationDisposition.QUARANTINED, str(exc), key)

    precision = _value(item, "timestamp_precision", "added_at_precision")
    if isinstance(precision, str) and precision.lower() in {
        "day",
        "hour",
        "minute",
        "second",
        "coarse",
        "unknown",
    }:
        is_coarse = True
    if is_ambiguous:
        return ActivationDecision(
            ActivationDisposition.QUARANTINED, "clock_ambiguous", key
        )
    if is_coarse:
        return ActivationDecision(
            ActivationDisposition.QUARANTINED, "added_at_coarse", key
        )
    if raw_added is None:
        return ActivationDecision(
            ActivationDisposition.QUARANTINED, "added_at_missing", key
        )
    try:
        observed = _coerce_datetime(raw_added, "added_at")
    except PlanningError:
        return ActivationDecision(
            ActivationDisposition.QUARANTINED, "added_at_invalid", key
        )
    if observed <= baseline:
        reason = (
            "added_at_equal_baseline"
            if observed == baseline
            else "added_at_before_baseline"
        )
        return ActivationDecision(ActivationDisposition.QUARANTINED, reason, key)
    return ActivationDecision(ActivationDisposition.NEW, "added_at_after_baseline", key)


# Friendly aliases used by resolver/reconciliation callers.
classify_activation = classify_activation_item
classify_activation_candidate = classify_activation_item


@dataclass(frozen=True, slots=True)
class CanonicalUnit:
    """A verified playable movie or episode used as a planning unit."""

    unit_id: str
    media_type: MediaType | str
    visible_in_plex_at: datetime
    title: str = ""
    year: int | None = None
    show_identity: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    quality: str | None = None
    plex_url: str | None = None
    logical_identity: str | None = None
    mode: RequestMode | str | None = None
    provider_identity: str | None = None
    server_uuid: str | None = None
    library_uuid: str | None = None
    snapshot_verified: bool = False
    playable: bool = False
    library_priority: int = 0
    resolution: int | None = None
    bitrate: int | None = None
    tombstone_generation: int = 0

    def __post_init__(self) -> None:
        key = _text(self.unit_id, "unit_id")
        assert key is not None
        object.__setattr__(self, "unit_id", key)
        media_type = (
            self.media_type.value
            if isinstance(self.media_type, MediaType)
            else self.media_type
        )
        if media_type not in {MediaType.MOVIE.value, MediaType.EPISODE.value}:
            raise PlanningError("canonical unit media_type must be movie or episode")
        object.__setattr__(self, "media_type", MediaType(media_type))
        object.__setattr__(
            self,
            "visible_in_plex_at",
            _coerce_datetime(self.visible_in_plex_at, "visible_in_plex_at"),
        )
        if isinstance(self.year, bool) or (
            self.year is not None and (not isinstance(self.year, int) or self.year < 0)
        ):
            raise PlanningError("year must be a non-negative integer")
        object.__setattr__(
            self, "title", _text(self.title, "title", optional=True) or ""
        )
        object.__setattr__(
            self,
            "show_identity",
            _text(self.show_identity, "show_identity", optional=True),
        )
        object.__setattr__(
            self,
            "logical_identity",
            _text(self.logical_identity, "logical_identity", optional=True),
        )
        object.__setattr__(
            self,
            "provider_identity",
            _text(self.provider_identity, "provider_identity", optional=True),
        )
        object.__setattr__(
            self, "server_uuid", _text(self.server_uuid, "server_uuid", optional=True)
        )
        object.__setattr__(
            self,
            "library_uuid",
            _text(self.library_uuid, "library_uuid", optional=True),
        )
        if not isinstance(self.snapshot_verified, bool) or not isinstance(
            self.playable, bool
        ):
            raise PlanningError("canonical unit verification flags must be boolean")
        if not self.snapshot_verified or not self.playable:
            raise PlanningError(
                "canonical unit requires a verified playable Plex snapshot"
            )
        if self.server_uuid is None or self.library_uuid is None:
            raise PlanningError("canonical unit requires server and library identity")
        if (
            len(self.server_uuid.encode("utf-8")) > 256
            or len(self.library_uuid.encode("utf-8")) > 256
        ):
            raise PlanningError("server/library identity is too long")
        for field_name in (
            "library_priority",
            "resolution",
            "bitrate",
            "tombstone_generation",
        ):
            value = getattr(self, field_name)
            if value is None and field_name in {"resolution", "bitrate"}:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise PlanningError(f"{field_name} must be an integer")
            if field_name == "tombstone_generation" and value < 0:
                raise PlanningError("tombstone_generation must be non-negative")
            if field_name in {"resolution", "bitrate"} and value < 0:
                raise PlanningError(f"{field_name} must be non-negative")
        object.__setattr__(
            self, "quality", _text(self.quality, "quality", optional=True)
        )
        object.__setattr__(
            self, "plex_url", _text(self.plex_url, "plex_url", optional=True)
        )
        for field_name in ("season_number", "episode_number"):
            value = _int_value(getattr(self, field_name), field_name)
            if value is not None and value < 0:
                raise PlanningError(f"{field_name} must be non-negative")
            object.__setattr__(self, field_name, value)
        if self.media_type is MediaType.EPISODE and (
            self.show_identity is None
            or self.season_number is None
            or self.episode_number is None
        ):
            raise PlanningError(
                "episode units require show, season, and episode identity"
            )
        if self.mode is not None:
            raw_mode = (
                self.mode.value if isinstance(self.mode, RequestMode) else self.mode
            )
            try:
                object.__setattr__(self, "mode", RequestMode(raw_mode))
            except ValueError as exc:
                raise PlanningError("canonical unit mode is invalid") from exc

    @property
    def logical_key(self) -> str:
        return self.unit_id

    @property
    def is_episode(self) -> bool:
        return self.media_type is MediaType.EPISODE

    @property
    def season_key(self) -> tuple[str, int] | None:
        if (
            not self.is_episode
            or self.show_identity is None
            or self.season_number is None
        ):
            return None
        return self.show_identity, self.season_number

    @classmethod
    def from_record(cls, record: object) -> CanonicalUnit:
        raw_unit_id = _value(
            record, "unit_id", "logical_key", "logical_unit_key", "rating_key"
        )
        if raw_unit_id is None or not str(raw_unit_id).strip():
            raise PlanningError("canonical unit requires a logical identity")
        raw_priority = _value(record, "library_priority", default=0)
        raw_tombstone = _value(record, "tombstone_generation", "generation", default=0)
        priority = _int_value(raw_priority, "library_priority", optional=False)
        tombstone = _int_value(raw_tombstone, "tombstone_generation", optional=False)
        assert priority is not None and tombstone is not None
        media_type = cast(
            MediaType | str,
            _value(record, "media_type", "type", default=MediaType.MOVIE.value),
        )
        return cls(
            unit_id=str(raw_unit_id),
            media_type=media_type,
            visible_in_plex_at=cast(
                datetime,
                _value(record, "visible_in_plex_at", "visible_at", "visibleAt"),
            ),
            title=str(_value(record, "title", default="")),
            year=_int_value(_value(record, "year"), "year"),
            show_identity=cast(
                str | None,
                _value(record, "show_identity", "show_id", "canonical_show_identity"),
            ),
            season_number=_int_value(
                _value(record, "season_number", "season"), "season_number"
            ),
            episode_number=_int_value(
                _value(record, "episode_number", "episode"), "episode_number"
            ),
            quality=cast(str | None, _value(record, "quality")),
            plex_url=cast(str | None, _value(record, "plex_url", "plexUrl")),
            logical_identity=cast(
                str | None, _value(record, "logical_identity", "media_identity")
            ),
            mode=cast(RequestMode | str | None, _value(record, "mode")),
            provider_identity=cast(str | None, _value(record, "provider_identity")),
            server_uuid=cast(str | None, _value(record, "server_uuid", "serverUuid")),
            library_uuid=cast(
                str | None,
                _value(record, "library_uuid", "library_key", "libraryUuid"),
            ),
            snapshot_verified=_bool_value(
                _value(
                    record,
                    "snapshot_verified",
                    "verified_snapshot",
                    "snapshotValid",
                ),
                "snapshot_verified",
                default=False,
            ),
            playable=_bool_value(
                _value(record, "playable", "is_playable", "verified_playable"),
                "playable",
                default=False,
            ),
            library_priority=priority,
            resolution=_int_value(_value(record, "resolution"), "resolution"),
            bitrate=_int_value(_value(record, "bitrate"), "bitrate"),
            tombstone_generation=tombstone,
        )


@dataclass(frozen=True, slots=True)
class Subscription:
    """Trusted requester interest used for destination fan-out."""

    generation: int
    destination: str | int
    active_at: datetime
    user_id: int | None = None
    media_identity: str | None = None
    unit_keys: frozenset[str] = frozenset()
    show_identity: str | None = None
    seasons: frozenset[int] = frozenset()
    mode: RequestMode | str | None = None
    active: bool = True
    trusted: bool = True
    requester_name: str | None = None
    subscription_id: str | int | None = None
    required_unit_keys: frozenset[str] = frozenset()
    enumeration_complete: bool = False
    season_ended: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation <= 0
        ):
            raise PlanningError("subscription generation must be a positive integer")
        if isinstance(self.destination, bool) or not isinstance(
            self.destination, (str, int)
        ):
            raise PlanningError("subscription destination must be a string or integer")
        if isinstance(self.destination, str) and not self.destination.strip():
            raise PlanningError("subscription destination must not be blank")
        if isinstance(self.destination, str):
            object.__setattr__(self, "destination", self.destination.strip())
        if isinstance(self.destination, int) and self.destination == 0:
            raise PlanningError("subscription destination must be non-zero")
        if self.user_id is not None and (
            isinstance(self.user_id, bool)
            or not isinstance(self.user_id, int)
            or self.user_id == 0
        ):
            raise PlanningError("subscription user_id must be a non-zero integer")
        object.__setattr__(
            self, "active_at", _coerce_datetime(self.active_at, "active_at")
        )
        object.__setattr__(
            self,
            "media_identity",
            _text(self.media_identity, "media_identity", optional=True),
        )
        object.__setattr__(
            self,
            "show_identity",
            _text(self.show_identity, "show_identity", optional=True),
        )
        object.__setattr__(
            self,
            "requester_name",
            _text(self.requester_name, "requester_name", optional=True),
        )
        if self.subscription_id is not None:
            if isinstance(self.subscription_id, bool) or not isinstance(
                self.subscription_id, (str, int)
            ):
                raise PlanningError("subscription_id must be a string or integer")
            if (
                isinstance(self.subscription_id, str)
                and not self.subscription_id.strip()
            ):
                raise PlanningError("subscription_id must not be blank")
            if isinstance(self.subscription_id, str):
                object.__setattr__(
                    self, "subscription_id", self.subscription_id.strip()
                )
        if self.subscription_id is None and self.user_id is None:
            raise PlanningError("subscription requires a durable subscriber identity")
        raw_unit_keys = () if self.unit_keys is None else self.unit_keys
        object.__setattr__(
            self, "unit_keys", frozenset(str(value) for value in raw_unit_keys)
        )
        if any(not value for value in self.unit_keys):
            raise PlanningError("subscription unit keys must not be blank")
        object.__setattr__(
            self,
            "required_unit_keys",
            frozenset(
                str(value)
                for value in (
                    () if self.required_unit_keys is None else self.required_unit_keys
                )
            ),
        )
        if any(not value for value in self.required_unit_keys):
            raise PlanningError("required unit keys must not be blank")
        normalized_seasons: set[int] = set()
        for value in () if self.seasons is None else self.seasons:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PlanningError(
                    "subscription seasons must be non-negative integers"
                )
            normalized_seasons.add(value)
        object.__setattr__(self, "seasons", frozenset(normalized_seasons))
        if not isinstance(self.active, bool) or not isinstance(self.trusted, bool):
            raise PlanningError("subscription active/trusted flags must be boolean")
        if not isinstance(self.enumeration_complete, bool):
            raise PlanningError("enumeration_complete must be boolean")
        if not isinstance(self.season_ended, bool):
            raise PlanningError("season_ended must be boolean")
        if self.mode is not None:
            raw_mode = (
                self.mode.value if isinstance(self.mode, RequestMode) else self.mode
            )
            try:
                object.__setattr__(self, "mode", RequestMode(raw_mode))
            except ValueError as exc:
                raise PlanningError("subscription mode is invalid") from exc

    @property
    def destination_key(self) -> str:
        return str(self.destination)

    @classmethod
    def from_record(cls, record: object) -> Subscription:
        raw_units = _value(record, "unit_keys", "requested_unit_keys", default=())
        raw_seasons = _value(record, "seasons", "season_numbers", default=())
        raw_ended = _value(record, "season_ended", "ended")
        if raw_ended is None:
            raw_ended = _value(record, "season_ended_at") is not None
        return cls(
            generation=int(
                cast(
                    int,
                    _value(record, "generation", "subscription_generation", default=1),
                )
            ),
            destination=cast(
                str | int,
                _value(record, "destination", "chat_id", "destination_chat_id"),
            ),
            active_at=cast(
                datetime, _value(record, "active_at", "created_at", "activated_at")
            ),
            user_id=_int_value(
                _value(record, "user_id", "requester_user_id"), "user_id"
            ),
            media_identity=cast(
                str | None, _value(record, "media_identity", "logical_identity")
            ),
            unit_keys=frozenset(str(value) for value in _iterable_values(raw_units)),
            show_identity=cast(
                str | None, _value(record, "show_identity", "canonical_show_identity")
            ),
            seasons=frozenset(
                int(cast(int | str, value)) for value in _iterable_values(raw_seasons)
            ),
            mode=cast(RequestMode | str | None, _value(record, "mode")),
            active=_bool_value(
                _value(record, "active", "is_active"), "active", default=True
            ),
            trusted=_bool_value(_value(record, "trusted"), "trusted", default=True),
            requester_name=cast(
                str | None,
                _value(record, "requester_name", "username", "display_name"),
            ),
            subscription_id=cast(
                str | int | None, _value(record, "subscription_id", "id")
            ),
            required_unit_keys=frozenset(
                str(value)
                for value in _iterable_values(
                    _value(
                        record, "required_unit_keys", "expected_unit_keys", default=()
                    )
                )
            ),
            enumeration_complete=_bool_value(
                _value(record, "enumeration_complete", "enumeration_known"),
                "enumeration_complete",
                default=False,
            ),
            season_ended=_bool_value(
                raw_ended,
                "season_ended",
                default=False,
            ),
        )

    @property
    def membership_key(self) -> tuple[str, str, int]:
        """Stable requester membership identity for shared-chat fan-out."""

        if self.subscription_id is not None:
            identity = str(self.subscription_id)
        else:
            scope = ":".join(
                (
                    f"media={self.media_identity or ''}",
                    f"show={self.show_identity or ''}",
                    f"seasons={','.join(str(value) for value in sorted(self.seasons))}",
                    f"units={','.join(sorted(self.unit_keys))}",
                    f"mode={self.mode.value if isinstance(self.mode, RequestMode) else self.mode or ''}",
                )
            )
            identity = f"user:{self.user_id!s}:destination:{self.destination_key}:scope:{scope}"
        return identity, self.destination_key, self.generation


@dataclass(frozen=True, slots=True)
class Obligation:
    """One destination/class/subscription-generation obligation."""

    unit_key: str
    destination: str
    notification_class: NotificationClass | str
    subscription_generation: int | None = None
    state: str = ObligationState.PENDING.value
    paired_obligation: ObligationKey | None = None
    requester_detail: bool = False
    reason: str | None = None
    subscription_id: str | int | None = None
    request_mode: RequestMode | str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.unit_key, str) or not self.unit_key.strip():
            raise PlanningError("obligation unit_key must not be blank")
        if not isinstance(self.destination, str) or not self.destination.strip():
            raise PlanningError("obligation destination must not be blank")
        object.__setattr__(self, "unit_key", self.unit_key.strip())
        object.__setattr__(self, "destination", self.destination.strip())
        raw_class = (
            self.notification_class.value
            if isinstance(self.notification_class, NotificationClass)
            else self.notification_class
        )
        if raw_class not in {
            NotificationClass.ADMIN.value,
            NotificationClass.REQUESTER.value,
        }:
            raise PlanningError("obligation notification class is invalid")
        object.__setattr__(self, "notification_class", NotificationClass(raw_class))
        if self.subscription_generation is not None and (
            isinstance(self.subscription_generation, bool)
            or not isinstance(self.subscription_generation, int)
            or self.subscription_generation <= 0
        ):
            raise PlanningError("subscription_generation must be positive when present")
        raw_state = (
            self.state.value if isinstance(self.state, ObligationState) else self.state
        )
        if raw_state not in _ACCOUNTED_STATES:
            raise PlanningError(f"unknown obligation state: {self.state!r}")
        object.__setattr__(self, "state", raw_state)
        if self.subscription_id is not None:
            if isinstance(self.subscription_id, bool) or not isinstance(
                self.subscription_id, (str, int)
            ):
                raise PlanningError("obligation subscription_id is invalid")
            if (
                isinstance(self.subscription_id, str)
                and not self.subscription_id.strip()
            ):
                raise PlanningError("obligation subscription_id must not be blank")
        if self.request_mode is not None:
            raw_mode = (
                self.request_mode.value
                if isinstance(self.request_mode, RequestMode)
                else self.request_mode
            )
            try:
                object.__setattr__(self, "request_mode", RequestMode(raw_mode))
            except ValueError as exc:
                raise PlanningError("obligation request_mode is invalid") from exc

    @property
    def accounting_generation(self) -> int | str | None:
        if self.subscription_generation is None or self.subscription_id is None:
            return self.subscription_generation
        return f"{self.subscription_id}:{self.subscription_generation}"

    @property
    def key(self) -> ObligationKey:
        return (
            self.unit_key,
            self.destination,
            _class_value(self.notification_class),
            self.accounting_generation,
        )

    @property
    def is_admin(self) -> bool:
        return self.notification_class is NotificationClass.ADMIN

    @property
    def is_requester(self) -> bool:
        return self.notification_class is NotificationClass.REQUESTER


def _as_unit(value: CanonicalUnit | object) -> CanonicalUnit:
    return (
        value if isinstance(value, CanonicalUnit) else CanonicalUnit.from_record(value)
    )


def _as_subscription(value: Subscription | object) -> Subscription:
    return value if isinstance(value, Subscription) else Subscription.from_record(value)


def _subscription_matches(subscription: Subscription, unit: CanonicalUnit) -> bool:
    if not subscription.active or not subscription.trusted:
        return False
    if (
        unit.logical_identity is not None
        and unit.provider_identity is not None
        and unit.logical_identity != unit.provider_identity
    ):
        # Conflicting crosswalks are quarantined by the resolver; they must
        # never become a requester match merely because one identifier agrees.
        return False
    if subscription.unit_keys and unit.unit_id not in subscription.unit_keys:
        return False
    if subscription.media_identity is not None and subscription.media_identity not in {
        unit.logical_identity,
        unit.provider_identity,
    }:
        return False
    if (
        subscription.show_identity is not None
        and unit.show_identity != subscription.show_identity
    ):
        return False
    if subscription.seasons and unit.season_number not in subscription.seasons:
        return False
    if (
        subscription.mode is RequestMode.MOVIE
        and unit.media_type is not MediaType.MOVIE
    ):
        return False
    if (
        subscription.mode in {RequestMode.AIRING_EPISODE, RequestMode.SEASON_COMPLETION}
        and not unit.is_episode
    ):
        return False
    if subscription.mode in {
        RequestMode.AIRING_EPISODE,
        RequestMode.SEASON_COMPLETION,
    } and not (
        subscription.unit_keys or (subscription.show_identity and subscription.seasons)
    ):
        # A show-only subscription is not an explicit season scope.  In
        # particular this prevents accidental season-0 backfill.
        return False
    if unit.is_episode and not (
        subscription.unit_keys or (subscription.show_identity and subscription.seasons)
    ):
        # A provider/show identity without an explicit season or unit set is
        # not an episode scope; never infer all seasons or specials.
        return False
    # A subscription with no explicit scope is not enough to match a unit.
    return bool(
        subscription.unit_keys
        or subscription.media_identity
        or subscription.show_identity
    )


def _unit_dedupe_key(unit: CanonicalUnit) -> tuple[str, str]:
    """Return the provider-scoped key used to collapse duplicate versions."""

    if unit.is_episode:
        if (
            unit.logical_identity is not None
            and unit.provider_identity is not None
            and unit.logical_identity != unit.provider_identity
        ):
            return "episode-conflict", unit.unit_id
        identity = unit.logical_identity or unit.provider_identity
        if identity is not None:
            return (
                "episode",
                f"{identity}:{unit.show_identity}:{unit.season_number}:"
                f"{unit.episode_number}:generation={unit.tombstone_generation}",
            )
        return "episode", unit.unit_id
    if (
        unit.logical_identity is not None
        and unit.provider_identity is not None
        and unit.logical_identity != unit.provider_identity
    ):
        return "movie-conflict", unit.unit_id
    identity = unit.logical_identity or unit.provider_identity or unit.unit_id
    return "movie", f"{identity}:generation={unit.tombstone_generation}"


def _preferred_unit(left: CanonicalUnit, right: CanonicalUnit) -> CanonicalUnit:
    """Choose a deterministic display version while a group is open."""

    left_rank = (
        left.library_priority,
        -(left.resolution or 0),
        -(left.bitrate or 0),
        left.unit_id,
    )
    right_rank = (
        right.library_priority,
        -(right.resolution or 0),
        -(right.bitrate or 0),
        right.unit_id,
    )
    return left if left_rank <= right_rank else right


def _collapse_duplicate_units(
    units: Iterable[CanonicalUnit],
) -> tuple[CanonicalUnit, ...]:
    selected: dict[tuple[str, str], CanonicalUnit] = {}
    for unit in units:
        key = _unit_dedupe_key(unit)
        current = selected.get(key)
        selected[key] = unit if current is None else _preferred_unit(current, unit)
    return tuple(
        sorted(
            selected.values(), key=lambda unit: (unit.visible_in_plex_at, unit.unit_id)
        )
    )


def build_obligations(
    units: Iterable[CanonicalUnit | object],
    *,
    subscriptions: Iterable[Subscription | object] = (),
    admin_destinations: Iterable[str | int] = (),
    include_historical: bool = False,
) -> tuple[Obligation, ...]:
    """Create admin/requester obligations at the exact accounting grain.

    ``units`` should normally already be activation-filtered to new playable
    units.  ``include_historical`` exists for the explicit migrated-baseline
    requester pass; it does *not* create admin obligations for those units.
    The normal caller therefore leaves it false and handles baseline
    fulfillment in its dedicated transaction.
    """

    normalized_units = _collapse_duplicate_units(_as_unit(value) for value in units)
    normalized_subscriptions = tuple(_as_subscription(value) for value in subscriptions)
    admin_destinations_tuple = tuple(
        dict.fromkeys(
            _destination_key(value, "admin destination") for value in admin_destinations
        )
    )
    result: list[Obligation] = []
    for unit in normalized_units:
        # Admin deliveries are always created for eligible post-activation
        # logical units.  Historical units are intentionally excluded here.
        if include_historical:
            admin_destinations_for_unit: tuple[str, ...] = ()
        else:
            admin_destinations_for_unit = admin_destinations_tuple
        for destination in admin_destinations_for_unit:
            result.append(
                Obligation(
                    unit_key=unit.unit_id,
                    destination=destination,
                    notification_class=NotificationClass.ADMIN,
                    subscription_generation=None,
                    requester_detail=False,
                )
            )
        for subscription in normalized_subscriptions:
            if not _subscription_matches(subscription, unit):
                continue
            if subscription.mode is RequestMode.SEASON_COMPLETION:
                expected = subscription.required_unit_keys
                available = {
                    candidate.unit_id
                    for candidate in normalized_units
                    if _subscription_matches(subscription, candidate)
                }
                if (
                    not subscription.season_ended
                    or not subscription.enumeration_complete
                    or not expected
                    or not expected.issubset(available)
                ):
                    # Completion mode is an all-or-nothing season obligation.
                    # Do not emit an episode-level fallback when enumeration is
                    # incomplete or a required unit is absent.
                    continue
            # Eligibility is linearized by the transaction that commits the
            # subscription and verified visible_in_plex_at snapshot.  Equality
            # is visibility-first, not a new asynchronous delivery.
            if subscription.active_at >= unit.visible_in_plex_at:
                continue
            requester = Obligation(
                unit_key=unit.unit_id,
                destination=subscription.destination_key,
                notification_class=NotificationClass.REQUESTER,
                subscription_generation=subscription.generation,
                subscription_id=(
                    subscription.subscription_id
                    if subscription.subscription_id is not None
                    else subscription.membership_key[0]
                ),
                request_mode=subscription.mode,
            )
            result.append(requester)

    # One destination can be both admin and requester.  The admin obligation
    # is the winning delivery, while each requester obligation remains in the
    # ledger as an explicit linked/suppressed obligation.
    admin_by_unit_destination: dict[tuple[str, str], Obligation] = {
        (obligation.unit_key, obligation.destination): obligation
        for obligation in result
        if obligation.is_admin
    }
    winning_keys: set[ObligationKey] = set()
    winning_modes: dict[ObligationKey, RequestMode | str | None] = {}
    transformed: list[Obligation] = []
    for obligation in result:
        if obligation.is_requester:
            winning = admin_by_unit_destination.get(
                (obligation.unit_key, obligation.destination)
            )
            if winning is not None:
                winning_keys.add(winning.key)
                winning_modes[winning.key] = obligation.request_mode
                transformed.append(
                    replace(
                        obligation,
                        state=ObligationState.SUPPRESSED.value,
                        paired_obligation=winning.key,
                        reason="admin_class_wins_same_destination",
                    )
                )
                continue
        transformed.append(obligation)

    # Preserve the requester-relevant episode/season marker in the winning
    # admin payload.  The requester row stays linked/suppressed in the ledger.
    transformed = [
        replace(
            obligation,
            requester_detail=True,
            request_mode=winning_modes.get(obligation.key, obligation.request_mode),
        )
        if obligation.key in winning_keys
        else obligation
        for obligation in transformed
    ]

    # Stable order is useful for deterministic idempotency keys and tests.
    return tuple(
        sorted(
            transformed,
            key=lambda row: (
                row.unit_key,
                row.destination,
                _class_value(row.notification_class),
                str(row.accounting_generation or 0),
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class NotificationGroup:
    """A fixed-window group with an immutable generation key."""

    destination: str
    notification_class: NotificationClass | str
    show_identity: str | None
    season_number: int | None
    window_generation: int
    first_seen_at: datetime
    due_at: datetime
    unit_keys: tuple[str, ...] = ()
    obligation_keys: tuple[ObligationKey, ...] = ()
    state: str = "open"
    payload: str | None = None
    requester_detail: bool = False
    completion_ready: bool = False
    source_group_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.destination, str) or not self.destination.strip():
            raise PlanningError("group destination must not be blank")
        object.__setattr__(self, "destination", self.destination.strip())
        raw_class = (
            self.notification_class.value
            if isinstance(self.notification_class, NotificationClass)
            else self.notification_class
        )
        if raw_class not in {
            NotificationClass.ADMIN.value,
            NotificationClass.REQUESTER.value,
        }:
            raise PlanningError("group notification class is invalid")
        object.__setattr__(self, "notification_class", NotificationClass(raw_class))
        object.__setattr__(
            self,
            "show_identity",
            _text(self.show_identity, "show_identity", optional=True),
        )
        if not isinstance(self.requester_detail, bool):
            raise PlanningError("requester_detail must be boolean")
        if (
            isinstance(self.window_generation, bool)
            or not isinstance(self.window_generation, int)
            or self.window_generation < 0
        ):
            raise PlanningError("window_generation must be non-negative")
        if not isinstance(self.completion_ready, bool):
            raise PlanningError("completion_ready must be boolean")
        first = _coerce_datetime(self.first_seen_at, "first_seen_at")
        due = _coerce_datetime(self.due_at, "due_at")
        if due != first + GROUP_WINDOW:
            raise PlanningError("group due_at must be first_seen_at + five minutes")
        object.__setattr__(self, "first_seen_at", first)
        object.__setattr__(self, "due_at", due)
        if self.state not in {
            "open",
            "ready",
            "sent",
            "superseded",
            "canceled",
            "quarantined",
        }:
            raise PlanningError("invalid group state")
        unit_keys = tuple(dict.fromkeys(self.unit_keys))
        if any(not isinstance(key, str) or not key for key in unit_keys):
            raise PlanningError("group unit_keys must contain non-empty strings")
        object.__setattr__(self, "unit_keys", unit_keys)
        obligation_keys = tuple(dict.fromkeys(self.obligation_keys))
        for key in obligation_keys:
            _obligation_key({"key": key})
        object.__setattr__(self, "obligation_keys", obligation_keys)
        if self.payload is not None:
            if not isinstance(self.payload, str):
                raise PlanningError("group payload must be text")
            if len(self.payload.encode("utf-8")) > MAX_RENDER_MESSAGE_BYTES:
                raise PlanningError("group payload exceeds message limit")
        source_keys = tuple(dict.fromkeys(self.source_group_keys))
        if any(not isinstance(key, str) or not key for key in source_keys):
            raise PlanningError("source_group_keys must contain non-empty strings")
        object.__setattr__(self, "source_group_keys", source_keys)

    @property
    def base_key(self) -> tuple[str, str, str | None, int | None]:
        return (
            self.destination,
            _class_value(self.notification_class),
            self.show_identity,
            self.season_number,
        )

    @property
    def idempotency_key(self) -> str:
        payload = {
            "destination": self.destination,
            "class": _class_value(self.notification_class),
            "show": self.show_identity,
            "season": self.season_number,
            "generation": self.window_generation,
            "sources": self.source_group_keys,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def add(self, unit: CanonicalUnit, obligation: Obligation) -> NotificationGroup:
        if self.state != "open":
            raise PlanningError("ready/terminal groups have immutable payloads")
        if unit.visible_in_plex_at >= self.due_at:
            raise PlanningError("unit is at or after the fixed group boundary")
        first_seen = min(self.first_seen_at, unit.visible_in_plex_at)
        return replace(
            self,
            first_seen_at=first_seen,
            due_at=first_seen + GROUP_WINDOW,
            unit_keys=tuple(sorted(set(self.unit_keys) | {unit.unit_id})),
            obligation_keys=tuple(sorted(set(self.obligation_keys) | {obligation.key})),
            requester_detail=self.requester_detail or obligation.requester_detail,
            completion_ready=self.completion_ready
            or obligation.request_mode is RequestMode.SEASON_COMPLETION,
        )

    def ready(self, *, payload: str | None = None) -> NotificationGroup:
        if not self.unit_keys:
            raise PlanningError("cannot ready an empty notification group")
        return replace(
            self,
            state="ready",
            payload=payload if payload is not None else self.payload,
        )


@dataclass(frozen=True, slots=True)
class NotificationPlan:
    """Groups plus their obligations from one planner transaction."""

    groups: tuple[NotificationGroup, ...]
    obligations: tuple[Obligation, ...]
    accounting: OracleResult


def _group_base_for(
    unit: CanonicalUnit, obligation: Obligation
) -> tuple[str, str, str | None, int | None]:
    if not unit.is_episode:
        # Movies intentionally do not coalesce with another movie.
        return (
            obligation.destination,
            _class_value(obligation.notification_class),
            unit.unit_id,
            None,
        )
    return (
        obligation.destination,
        _class_value(obligation.notification_class),
        unit.show_identity,
        unit.season_number,
    )


def _next_generation(
    base: tuple[str, str, str | None, int | None],
    groups: Mapping[
        tuple[str, str, str | None, int | None], Sequence[NotificationGroup]
    ],
) -> int:
    rows = groups.get(base, ())
    return max((row.window_generation for row in rows), default=-1) + 1


def plan_groups(
    units: Iterable[CanonicalUnit | object],
    obligations: Iterable[Obligation],
    *,
    existing_groups: Iterable[NotificationGroup] = (),
) -> tuple[NotificationGroup, ...]:
    """Build fixed five-minute groups without moving an existing deadline.

    Existing ``open`` groups can absorb a unit only when
    ``visible_in_plex_at < due_at``.  Existing ``ready``/terminal groups are
    immutable and therefore never absorb a unit; a new generation is created.
    """

    normalized_units = _collapse_duplicate_units(_as_unit(unit) for unit in units)
    unit_map = {unit.unit_id: unit for unit in normalized_units}
    existing_by_base: dict[
        tuple[str, str, str | None, int | None], list[NotificationGroup]
    ] = defaultdict(list)
    for group in existing_groups:
        existing_by_base[group.base_key].append(group)
    for rows in existing_by_base.values():
        rows.sort(key=lambda row: row.window_generation)

    groups: dict[tuple[str, str, str | None, int | None, int], NotificationGroup] = {}
    # Reuse only open groups.  Copying them into the working map makes this
    # function safe to call as a pure planner before a transaction commits.
    for group in existing_groups:
        groups[
            (
                group.destination,
                _class_value(group.notification_class),
                group.show_identity,
                group.season_number,
                group.window_generation,
            )
        ] = group

    normalized_obligations = tuple(obligations)
    for obligation in sorted(
        (
            row
            for row in normalized_obligations
            if row.state
            not in {
                ObligationState.SUPPRESSED.value,
                ObligationState.SENT.value,
                ObligationState.ASSUMED_SENT.value,
                ObligationState.FAILED.value,
                ObligationState.ABANDONED.value,
                ObligationState.DELIVERY_BLOCKED.value,
                ObligationState.CANCELED.value,
                ObligationState.SUPERSEDED.value,
                ObligationState.QUARANTINED.value,
            }
            and row.unit_key in unit_map
        ),
        key=lambda row: (
            unit_map[row.unit_key].visible_in_plex_at,
            row.destination,
            _class_value(row.notification_class),
            row.unit_key,
            str(row.accounting_generation or 0),
        ),
    ):
        if obligation.state == ObligationState.SUPPRESSED.value:
            # The requester obligation is linked to the same-destination admin
            # group and must never create a second Telegram message.
            continue
        unit = unit_map[obligation.unit_key]
        base = _group_base_for(unit, obligation)
        candidates = [
            group
            for group in existing_by_base.get(base, [])
            if group.state == "open" and unit.visible_in_plex_at < group.due_at
        ]
        # Prefer the latest currently-open generation.  If none is eligible,
        # create a new generation with first_seen_at from this unit.
        selected: NotificationGroup | None = None
        if candidates:
            selected = max(candidates, key=lambda row: row.window_generation)
            key = (*base, selected.window_generation)
            selected = groups[key]
            # The same logical unit may have both requester/admin obligations;
            # the group records it once and the obligation ledger records both.
            updated = selected.add(unit, obligation)
            groups[key] = updated
            for index, existing in enumerate(existing_by_base[base]):
                if existing.window_generation == updated.window_generation:
                    existing_by_base[base][index] = updated
                    break
            continue

        generation = _next_generation(base, existing_by_base)
        while (*base, generation) in groups:
            generation += 1
        first_seen = unit.visible_in_plex_at
        group = NotificationGroup(
            destination=obligation.destination,
            notification_class=obligation.notification_class,
            show_identity=base[2],
            season_number=base[3],
            window_generation=generation,
            first_seen_at=first_seen,
            due_at=first_seen + GROUP_WINDOW,
            unit_keys=(unit.unit_id,),
            obligation_keys=(obligation.key,),
            requester_detail=obligation.requester_detail,
            completion_ready=obligation.request_mode is RequestMode.SEASON_COMPLETION,
        )
        groups[(*base, generation)] = group
        existing_by_base.setdefault(base, []).append(group)

    return tuple(
        sorted(
            groups.values(),
            key=lambda row: (
                row.first_seen_at,
                row.destination,
                _class_value(row.notification_class),
                row.show_identity or "",
                row.season_number if row.season_number is not None else -1,
                row.window_generation,
            ),
        )
    )


def plan_notifications(
    units: Iterable[CanonicalUnit | object],
    *,
    subscriptions: Iterable[Subscription | object] = (),
    admin_destinations: Iterable[str | int] = (),
    existing_groups: Iterable[NotificationGroup] = (),
    include_historical: bool = False,
    scan_complete: bool = False,
    full_sweep_complete: bool = False,
    scans_fresh: bool = False,
) -> NotificationPlan:
    """Build obligations/groups and evaluate the no-loss oracle together."""

    if any(
        not isinstance(flag, bool)
        for flag in (scan_complete, full_sweep_complete, scans_fresh)
    ):
        raise PlanningError("scan evidence flags must be boolean")
    normalized_units = _collapse_duplicate_units(_as_unit(value) for value in units)
    obligations = build_obligations(
        normalized_units,
        subscriptions=subscriptions,
        admin_destinations=admin_destinations,
        include_historical=include_historical,
    )
    groups = plan_groups(normalized_units, obligations, existing_groups=existing_groups)
    grouped_keys = {
        obligation_key for group in groups for obligation_key in group.obligation_keys
    }
    accounted: dict[ObligationKey, str] = {}
    for obligation in obligations:
        if obligation.state == ObligationState.SUPPRESSED.value:
            accounted[obligation.key] = ObligationState.SUPPRESSED.value
        else:
            accounted[obligation.key] = (
                ObligationState.READY.value
                if obligation.key in grouped_keys
                else obligation.state
            )
    oracle = evaluate_oracle(
        obligations,
        accounted,
        scan_complete=scan_complete,
        full_sweep_complete=full_sweep_complete,
        scans_fresh=scans_fresh,
    )
    return NotificationPlan(groups=groups, obligations=obligations, accounting=oracle)


def render_group_text(
    group: NotificationGroup,
    units: Iterable[CanonicalUnit | object],
    *,
    include_request_marker: bool | None = None,
    max_bytes: int = MAX_RENDER_MESSAGE_BYTES,
) -> str:
    """Render deterministic bounded text for a group.

    Rendering is deliberately plain text.  A Telegram adapter may apply its
    own escaping, but it cannot accidentally omit episode/season detail when
    an admin delivery wins over a requester delivery.
    """

    chunks = render_group_chunks(
        group,
        units,
        include_request_marker=include_request_marker,
        max_bytes=max_bytes,
    )
    if len(chunks) != 1:
        raise PlanningError("group payload exceeds the message limit; use chunks")
    return chunks[0] if chunks else ""


def _truncate_utf8(value: str, limit: int) -> str:
    if limit <= 0:
        raise PlanningError("render limit must be positive")
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix = "…"
    available = max(0, limit - len(suffix.encode("utf-8")))
    return encoded[:available].decode("utf-8", "ignore") + suffix


def _safe_render_field(value: object, *, limit: int = MAX_RENDER_FIELD_BYTES) -> str:
    text = str(value or "")
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()
    text = _truncate_utf8(text, limit)
    return html.escape(text, quote=True)


def _safe_render_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw.encode("utf-8")) > MAX_RENDER_URL_BYTES:
        return None
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        private_host = False
        if hostname is not None:
            try:
                address = ipaddress.ip_address(hostname)
                private_host = bool(
                    address.is_private
                    or address.is_loopback
                    or address.is_link_local
                    or address.is_unspecified
                )
            except ValueError:
                private_host = hostname.lower() in {
                    "localhost"
                } or hostname.lower().endswith(".local")
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or hostname is None
            or private_host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(
                marker in raw.lower()
                for marker in ("%3f", "%23", "token=", "access_token", "bot_token")
            )
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw)
        ):
            return None
        # Accessing port rejects malformed numeric ports in urllib.
        _ = parsed.port
    except ValueError:
        return None
    return html.escape(_truncate_utf8(raw, MAX_RENDER_URL_BYTES), quote=True)


def render_group_chunks(
    group: NotificationGroup,
    units: Iterable[CanonicalUnit | object],
    *,
    include_request_marker: bool | None = None,
    max_bytes: int = MAX_RENDER_MESSAGE_BYTES,
) -> tuple[str, ...]:
    """Render bounded, escaped chunks without splitting a canonical unit line."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise PlanningError("max_bytes must be a positive integer")
    normalized = tuple(_collapse_duplicate_units(_as_unit(unit) for unit in units))
    unit_map = {unit.unit_id: unit for unit in normalized}
    rows = [unit_map[key] for key in group.unit_keys if key in unit_map]
    rows.sort(
        key=lambda row: (row.season_number or -1, row.episode_number or -1, row.unit_id)
    )
    if not rows:
        return ()
    title = _safe_render_field(
        rows[0].show_identity or rows[0].title or "Available media"
    )
    if rows[0].year is not None:
        title = f"{title} ({rows[0].year})"
    heading = title
    marker = (
        group.requester_detail
        if include_request_marker is None
        else include_request_marker
    )
    if group.notification_class is NotificationClass.ADMIN and marker:
        heading += "\nRequested availability"
    lines: list[str] = []
    for row in rows:
        if row.is_episode:
            detail = f"S{row.season_number:02d}E{row.episode_number:02d}"
            if row.title:
                detail += f" — {_safe_render_field(row.title)}"
        else:
            detail = _safe_render_field(row.title or "Movie")
        if row.quality:
            detail += f" [{_safe_render_field(row.quality)}]"
        safe_url = _safe_render_url(row.plex_url)
        if safe_url:
            detail += f" — {safe_url}"
        if len(detail.encode("utf-8")) > max_bytes:
            raise PlanningError("canonical unit render line exceeds message limit")
        lines.append(detail)
    chunks: list[str] = []
    current = heading
    for line in lines:
        candidate = f"{current}\n{line}"
        if len(candidate.encode("utf-8")) <= max_bytes:
            current = candidate
            continue
        if current == heading:
            raise PlanningError("render heading exceeds message limit")
        chunks.append(current)
        current = f"{heading}\n{line}"
        if len(current.encode("utf-8")) > max_bytes:
            raise PlanningError("canonical unit render line exceeds message limit")
    if current:
        chunks.append(current)
    return tuple(chunks)


def assemble_completed_seasons(
    groups: Iterable[NotificationGroup],
) -> tuple[NotificationGroup, ...]:
    """Optionally combine same-window completed TV seasons for one destination.

    Membership and accounting remain per-season in the source groups.  The
    returned synthetic group is only an assembly/rendering envelope and keeps
    every source obligation key, so an incomplete season cannot block another
    completed season.
    """

    rows = tuple(groups)
    buckets: dict[tuple[str, str, str | None], list[list[NotificationGroup]]] = (
        defaultdict(list)
    )
    passthrough: list[NotificationGroup] = []
    for group in sorted(rows, key=lambda row: (row.first_seen_at, row.idempotency_key)):
        if (
            group.state != "ready"
            or not group.completion_ready
            or group.season_number is None
        ):
            passthrough.append(group)
            continue
        base = (
            group.destination,
            _class_value(group.notification_class),
            group.show_identity,
        )
        windows = buckets[base]
        for window in windows:
            anchor = min(row.first_seen_at for row in window)
            if group.first_seen_at < anchor + GROUP_WINDOW:
                window.append(group)
                break
        else:
            windows.append([group])
    result: list[NotificationGroup] = []
    for windows in buckets.values():
        for bucket in windows:
            if len(bucket) <= 1 or len({row.season_number for row in bucket}) != len(
                bucket
            ):
                result.extend(bucket)
                continue
            first = min(row.first_seen_at for row in bucket)
            # Keep season numbers in each source group's obligation membership;
            # the envelope uses season=None to signal multi-season rendering.
            result.append(
                NotificationGroup(
                    destination=bucket[0].destination,
                    notification_class=bucket[0].notification_class,
                    show_identity=bucket[0].show_identity,
                    season_number=None,
                    window_generation=min(row.window_generation for row in bucket),
                    first_seen_at=first,
                    due_at=first + GROUP_WINDOW,
                    unit_keys=tuple(key for row in bucket for key in row.unit_keys),
                    obligation_keys=tuple(
                        key for row in bucket for key in row.obligation_keys
                    ),
                    state="ready",
                    requester_detail=any(row.requester_detail for row in bucket),
                    completion_ready=True,
                    source_group_keys=tuple(
                        sorted(row.idempotency_key for row in bucket)
                    ),
                )
            )
    result.extend(passthrough)
    return tuple(
        sorted(
            result,
            key=lambda row: (row.first_seen_at, row.destination, row.idempotency_key),
        )
    )


@dataclass(frozen=True, slots=True)
class OracleResult:
    """No-loss accounting result at obligation grain."""

    complete: bool
    residual: tuple[ObligationKey, ...] = ()
    duplicates: tuple[ObligationKey, ...] = ()
    invalid_states: tuple[tuple[ObligationKey, str], ...] = ()
    reason: str | None = None

    @property
    def ready(self) -> bool:
        return (
            self.complete
            and not self.residual
            and not self.duplicates
            and not self.invalid_states
        )

    @property
    def residual_count(self) -> int:
        return len(self.residual) + len(self.duplicates) + len(self.invalid_states)


def _accounted_value(value: object) -> tuple[str, int]:
    if isinstance(value, str):
        return value, 1
    if isinstance(value, Obligation):
        return value.state, 1
    if isinstance(value, Mapping):
        state = value.get("state", value.get("status"))
        count = value.get("count", 1)
        return str(state), int(count)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) == 0:
            return "", 0
        return str(value[0]), len(value)
    return str(value), 1


def _obligation_key(value: Obligation | object) -> ObligationKey:
    if isinstance(value, Obligation):
        return value.key
    raw_key = _value(value, "key")
    if raw_key is None:
        raw_key = (
            _value(value, "unit_key", "unit_id"),
            _value(value, "destination", "chat_id"),
            _value(value, "notification_class", "class"),
            _value(value, "subscription_generation", "generation"),
        )
    if not isinstance(raw_key, tuple) or len(raw_key) != 4:
        raise PlanningError("obligation key must have four components")
    unit_key, destination, notification_class, generation = raw_key
    if not isinstance(unit_key, str) or not isinstance(destination, str):
        raise PlanningError("obligation key unit/destination must be strings")
    if isinstance(notification_class, NotificationClass):
        notification_class = notification_class.value
    if not isinstance(notification_class, str):
        raise PlanningError("obligation key class must be a string")
    if generation is not None and not isinstance(generation, (int, str)):
        raise PlanningError("obligation key generation must be an integer or namespace")
    if isinstance(generation, bool):
        raise PlanningError("obligation key generation must not be boolean")
    return unit_key, destination, notification_class, generation


def evaluate_oracle(
    obligations: Iterable[Obligation | object],
    accounted: Mapping[ObligationKey, object] | Iterable[tuple[ObligationKey, object]],
    *,
    scan_complete: bool = True,
    full_sweep_complete: bool = True,
    scans_fresh: bool = True,
) -> OracleResult:
    """Verify that every obligation has exactly one accounted state.

    ``scan_complete``/``full_sweep_complete``/``scans_fresh`` intentionally
    gate readiness.  A partial page cannot make a vacuous zero residual pass.
    """

    if any(
        not isinstance(flag, bool)
        for flag in (scan_complete, full_sweep_complete, scans_fresh)
    ):
        raise PlanningError("scan evidence flags must be boolean")
    rows = tuple(obligations)
    expected: dict[ObligationKey, int] = defaultdict(int)
    for value in rows:
        key = _obligation_key(value)
        expected[key] += 1
    actual_values: Iterable[tuple[ObligationKey, object]]
    if isinstance(accounted, Mapping):
        actual_values = cast(Iterable[tuple[ObligationKey, object]], accounted.items())
    else:
        actual_values = cast(Iterable[tuple[ObligationKey, object]], accounted)
    actual: dict[ObligationKey, tuple[str, int]] = {}
    duplicates: list[ObligationKey] = []
    invalid: list[tuple[ObligationKey, str]] = []
    for key, value in actual_values:
        normalized_key = _obligation_key({"key": key})
        if normalized_key in actual:
            duplicates.append(normalized_key)
            continue
        state, count = _accounted_value(value)
        actual[normalized_key] = (state, count)
        if state not in _ACCOUNTED_STATES or count != 1:
            invalid.append((normalized_key, state))
    expected_keys = set(expected)
    actual_keys = set(actual)

    def key_sort(row: ObligationKey) -> tuple[str, str, str, str]:
        return row[0], row[1], row[2], str(row[3])

    residual = sorted(expected_keys - actual_keys, key=key_sort)
    duplicates.extend(sorted(actual_keys - expected_keys, key=key_sort))
    # Duplicate expected keys are a planner bug even if a single account row
    # happens to exist, because the grain is not unique.
    duplicates.extend(
        sorted(
            (key for key, count in expected.items() if count != 1),
            key=key_sort,
        )
    )
    reason: str | None = None
    complete = True
    if not scan_complete:
        complete = False
        reason = "incremental scan incomplete"
    elif not full_sweep_complete:
        complete = False
        reason = "full identity sweep incomplete"
    elif not scans_fresh:
        complete = False
        reason = "scan evidence is stale"
    elif residual or duplicates or invalid:
        complete = False
        reason = "obligation accounting residual"
    return OracleResult(
        complete=complete,
        residual=tuple(residual),
        duplicates=tuple(dict.fromkeys(duplicates)),
        invalid_states=tuple(invalid),
        reason=reason,
    )


class NoLossOracle:
    """Small transaction-boundary-friendly oracle accumulator."""

    def __init__(self) -> None:
        self._obligations: dict[ObligationKey, Obligation] = {}
        self._accounted: dict[ObligationKey, object] = {}

    def add_obligation(self, obligation: Obligation) -> None:
        if obligation.key in self._obligations:
            raise PlanningError("duplicate obligation key")
        self._obligations[obligation.key] = obligation

    def account(self, key: ObligationKey, state: str) -> None:
        if key in self._accounted:
            raise PlanningError("obligation was accounted more than once")
        self._accounted[key] = state

    def evaluate(
        self,
        *,
        scan_complete: bool = True,
        full_sweep_complete: bool = True,
        scans_fresh: bool = True,
    ) -> OracleResult:
        return evaluate_oracle(
            self._obligations.values(),
            self._accounted,
            scan_complete=scan_complete,
            full_sweep_complete=full_sweep_complete,
            scans_fresh=scans_fresh,
        )


def __getattr__(name: str) -> Any:
    """Lazily re-export delivery guards without creating an import cycle."""

    if name in {"ClockGuard", "LeaderEpoch", "LeaderLease"}:
        from . import delivery

        return getattr(delivery, name)
    raise AttributeError(name)


__all__ = [
    "GROUP_WINDOW",
    "GROUP_WINDOW_SECONDS",
    "MAX_RENDER_FIELD_BYTES",
    "MAX_RENDER_MESSAGE_BYTES",
    "MAX_RENDER_URL_BYTES",
    "ActivationBaseline",
    "ActivationDecision",
    "ActivationDisposition",
    "ActivationError",
    "ActivationPass",
    "CanonicalUnit",
    "ClockGuard",
    "ClockRollbackError",
    "IncompleteScanError",
    "LeaderEpoch",
    "LeaderLease",
    "NoLossOracle",
    "NotificationGroup",
    "NotificationPlan",
    "Obligation",
    "ObligationKey",
    "ObligationState",
    "OracleResult",
    "PlanningError",
    "Subscription",
    "assemble_completed_seasons",
    "build_obligations",
    "classify_activation",
    "classify_activation_candidate",
    "classify_activation_item",
    "evaluate_oracle",
    "plan_groups",
    "plan_notifications",
    "render_group_text",
    "render_group_chunks",
]
