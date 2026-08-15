"""Small, typed normalized records shared by companion workflows.

These records intentionally contain only fields the companion owns.  Provider
objects, filesystem paths, queue IDs, and bearer-bearing URLs do not cross this
module's boundary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

from .errors import ModelValidationError


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return str(self.value)


class MediaType(_StringEnum):
    MOVIE = "movie"
    SERIES = "series"
    EPISODE = "episode"
    TV = "series"


class RequestMode(_StringEnum):
    MOVIE = "movie"
    AIRING_EPISODE = "airing_episode"
    SEASON_COMPLETION = "season_completion"


class RequestStatus(_StringEnum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    DOWNLOADING = "downloading"
    IMPORTED_TO_ARR = "imported_to_arr"
    VISIBLE_IN_PLEX = "visible_in_plex"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    DELIVERED = "delivered"


class QueueState(_StringEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    IMPORTING = "importing"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class DeliveryState(_StringEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SENDING = "sending"
    SENT = "sent"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DELIVERY_BLOCKED = "delivery_blocked"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"
    ASSUMED_SENT = "assumed_sent"


class NotificationClass(_StringEnum):
    ADMIN = "admin"
    REQUESTER = "requester"


class ServiceName(_StringEnum):
    UPSTREAM = "upstream"
    PLEX = "plex"
    RADARR = "radarr"
    SONARR = "sonarr"
    TMDB = "tmdb"
    TELEGRAM = "telegram"


# Alternate names are useful at adapter boundaries and preserve one wire value.
MediaKind = MediaType
RequestState = RequestStatus
DeliveryStatus = DeliveryState
QueueStatus = QueueState


def _text(value: object, field_name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ModelValidationError(f"{field_name} must be a string")
    result = value.strip()
    if not result:
        if optional:
            return None
        raise ModelValidationError(f"{field_name} must not be blank")
    return result


EnumT = TypeVar("EnumT", bound=_StringEnum)


def _enum(value: object, enum_type: type[EnumT], field_name: str) -> EnumT:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise ModelValidationError(f"{field_name} is invalid")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ModelValidationError(f"{field_name} is invalid") from exc


def _positive_int(
    value: object, field_name: str, *, optional: bool = False
) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ModelValidationError(f"{field_name} must be a positive integer")
    return value


def _nonnegative_int(
    value: object, field_name: str, *, optional: bool = False
) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelValidationError(f"{field_name} must be a non-negative integer")
    return value


def _telegram_id(
    value: object, field_name: str, *, optional: bool = False
) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise ModelValidationError(f"{field_name} must be a non-zero integer")
    return value


def _year(value: object, field_name: str = "year") -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1800 <= value <= 3000
    ):
        raise ModelValidationError(f"{field_name} must be a valid year")
    return value


def _timestamp(
    value: object, field_name: str, *, optional: bool = False
) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ModelValidationError(f"{field_name} must be timezone-aware")
    return value


def _seasons(value: Iterable[object] | None, *, required: bool) -> tuple[int, ...]:
    if value is None:
        if required:
            raise ModelValidationError("seasons must contain at least one integer")
        return ()
    values = tuple(value)
    if not values and required:
        raise ModelValidationError("seasons must contain at least one integer")
    result: set[int] = set()
    for season in values:
        if isinstance(season, bool) or not isinstance(season, int) or season < 0:
            raise ModelValidationError("seasons must contain non-negative integers")
        result.add(season)
    if len(result) > 50:
        raise ModelValidationError("seasons cannot contain more than 50 values")
    return tuple(sorted(result))


def canonical_rating_key(value: object) -> str:
    """Validate a Plex rating key before constructing a metadata path."""

    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise ModelValidationError("rating_key must be a canonical decimal string")
    return value


@dataclass(frozen=True, slots=True)
class MediaIdentity:
    """Stable provider identity used for matching and deduplication."""

    media_type: MediaType
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None
    provider_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "media_type", _enum(self.media_type, MediaType, "media_type")
        )
        for field_name in ("tmdb_id", "tvdb_id"):
            object.__setattr__(
                self,
                field_name,
                _positive_int(getattr(self, field_name), field_name, optional=True),
            )
        imdb = _text(self.imdb_id, "imdb_id", optional=True)
        if imdb is not None and not re.fullmatch(r"tt[0-9]+", imdb, re.IGNORECASE):
            raise ModelValidationError(
                "imdb_id must use the canonical tt-prefixed form"
            )
        object.__setattr__(self, "imdb_id", imdb)
        object.__setattr__(
            self,
            "provider_id",
            _text(self.provider_id, "provider_id", optional=True),
        )

    @property
    def stable(self) -> bool:
        return any((self.tmdb_id, self.tvdb_id, self.imdb_id, self.provider_id))

    @property
    def key(self) -> str:
        parts = [self.media_type.value]
        for field_name in ("tmdb_id", "tvdb_id", "imdb_id", "provider_id"):
            value = getattr(self, field_name)
            if value is not None:
                parts.append(f"{field_name}={value}")
        return ":".join(parts)


@dataclass(frozen=True, slots=True)
class MediaCandidate:
    """Sanitized search result suitable for a bounded shared response."""

    media_type: MediaType
    provider_id: int | str
    title: str
    year: int | None = None
    overview: str | None = None
    identity: MediaIdentity | None = None
    # Optional short-lived actor-bound reference used by the request seam.
    # Provider IDs remain server-side and are resolved from this handle.
    candidate_handle: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "media_type", _enum(self.media_type, MediaType, "media_type")
        )
        provider_id = self.provider_id
        if isinstance(provider_id, bool) or not isinstance(provider_id, (int, str)):
            raise ModelValidationError("provider_id must be an integer or string")
        if isinstance(provider_id, int) and provider_id <= 0:
            raise ModelValidationError("provider_id must be positive")
        if isinstance(provider_id, str) and not provider_id.strip():
            raise ModelValidationError("provider_id must not be blank")
        object.__setattr__(self, "provider_id", str(provider_id).strip())
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(self, "year", _year(self.year))
        object.__setattr__(
            self, "overview", _text(self.overview, "overview", optional=True)
        )

        if self.identity is None:
            provider_text = str(self.provider_id)
            if provider_text.isdigit():
                if self.media_type is MediaType.MOVIE:
                    identity = MediaIdentity(
                        self.media_type,
                        tmdb_id=int(provider_text),
                        provider_id=provider_text,
                    )
                else:
                    identity = MediaIdentity(
                        self.media_type,
                        tvdb_id=int(provider_text),
                        provider_id=provider_text,
                    )
            else:
                identity = MediaIdentity(
                    self.media_type,
                    provider_id=provider_text,
                )
            object.__setattr__(self, "identity", identity)
        elif not isinstance(self.identity, MediaIdentity):
            raise ModelValidationError("identity must be MediaIdentity")
        if self.candidate_handle is not None:
            if (
                not isinstance(self.candidate_handle, str)
                or not 20 <= len(self.candidate_handle) <= 128
                or any(
                    character.isspace()
                    or ord(character) < 0x20
                    or ord(character) == 0x7F
                    for character in self.candidate_handle
                )
                or re.fullmatch(r"[A-Za-z0-9_-]+", self.candidate_handle) is None
            ):
                raise ModelValidationError("candidate_handle is invalid")


@dataclass(frozen=True, slots=True)
class MediaRequest:
    """Durable request intent after trusted actor fields are applied."""

    request_id: int | None = None
    media_type: MediaType = MediaType.MOVIE
    provider_id: int | str = ""
    title: str = ""
    year: int | None = None
    seasons: tuple[int, ...] = ()
    requested_by_user_id: int | None = None
    requested_by_chat_id: int | None = None
    requested_by_username: str | None = None
    mode: RequestMode | None = None
    status: RequestStatus = RequestStatus.REQUESTED
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.request_id is not None:
            object.__setattr__(
                self, "request_id", _positive_int(self.request_id, "request_id")
            )
        object.__setattr__(
            self, "media_type", _enum(self.media_type, MediaType, "media_type")
        )
        if isinstance(self.provider_id, bool) or not isinstance(
            self.provider_id, (int, str)
        ):
            raise ModelValidationError("provider_id must be an integer or string")
        if isinstance(self.provider_id, int) and self.provider_id <= 0:
            raise ModelValidationError("provider_id must be positive")
        provider_id = str(self.provider_id).strip()
        if not provider_id:
            raise ModelValidationError("provider_id must not be blank")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(self, "year", _year(self.year))
        object.__setattr__(
            self,
            "seasons",
            _seasons(self.seasons, required=self.media_type is MediaType.SERIES),
        )
        object.__setattr__(
            self,
            "requested_by_user_id",
            _telegram_id(
                self.requested_by_user_id, "requested_by_user_id", optional=True
            ),
        )
        object.__setattr__(
            self,
            "requested_by_chat_id",
            _telegram_id(
                self.requested_by_chat_id, "requested_by_chat_id", optional=True
            ),
        )
        object.__setattr__(
            self,
            "requested_by_username",
            _text(self.requested_by_username, "requested_by_username", optional=True),
        )
        if self.mode is None:
            mode: RequestMode = (
                RequestMode.MOVIE
                if self.media_type is MediaType.MOVIE
                else RequestMode.SEASON_COMPLETION
            )
        else:
            mode = _enum(self.mode, RequestMode, "mode")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "status", _enum(self.status, RequestStatus, "status"))
        object.__setattr__(
            self, "created_at", _timestamp(self.created_at, "created_at", optional=True)
        )


@dataclass(frozen=True, slots=True)
class PlexItem:
    """Verified Plex visibility snapshot for a movie or episode."""

    rating_key: str
    media_type: MediaType
    title: str
    year: int | None = None
    library_key: str | None = None
    library_name: str | None = None
    show_title: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    quality: str | None = None
    plex_url: str | None = None
    added_at: datetime | None = None
    machine_identifier: str | None = None
    provider_identity: MediaIdentity | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rating_key", canonical_rating_key(self.rating_key))
        object.__setattr__(
            self, "media_type", _enum(self.media_type, MediaType, "media_type")
        )
        if self.media_type not in {MediaType.MOVIE, MediaType.EPISODE}:
            raise ModelValidationError("PlexItem media_type must be movie or episode")
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(self, "year", _year(self.year))
        for field_name in (
            "library_key",
            "library_name",
            "show_title",
            "quality",
            "plex_url",
            "machine_identifier",
        ):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name, optional=True),
            )
        for field_name in ("season_number", "episode_number"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ModelValidationError(
                    f"{field_name} must be a non-negative integer"
                )
        if self.media_type is MediaType.EPISODE and (
            self.season_number is None or self.episode_number is None
        ):
            raise ModelValidationError(
                "episode Plex items require season and episode numbers"
            )
        object.__setattr__(
            self, "added_at", _timestamp(self.added_at, "added_at", optional=True)
        )
        if self.provider_identity is not None and not isinstance(
            self.provider_identity, MediaIdentity
        ):
            raise ModelValidationError("provider_identity must be MediaIdentity")


@dataclass(frozen=True, slots=True)
class QueueItem:
    """Sanitized queue entry; provider/download IDs are intentionally absent."""

    service: ServiceName
    title: str
    state: QueueState
    progress_percent: float | None = None
    eta_seconds: int | None = None
    error: str | None = None
    media_type: MediaType | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "service", _enum(self.service, ServiceName, "service"))
        if self.service not in {ServiceName.RADARR, ServiceName.SONARR}:
            raise ModelValidationError("queue service must be radarr or sonarr")
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(self, "state", _enum(self.state, QueueState, "state"))
        if self.progress_percent is not None:
            if isinstance(self.progress_percent, bool) or not isinstance(
                self.progress_percent, (int, float)
            ):
                raise ModelValidationError("progress_percent must be numeric")
            if not 0 <= float(self.progress_percent) <= 100:
                raise ModelValidationError("progress_percent must be between 0 and 100")
            object.__setattr__(self, "progress_percent", float(self.progress_percent))
        if self.eta_seconds is not None:
            object.__setattr__(
                self,
                "eta_seconds",
                _nonnegative_int(self.eta_seconds, "eta_seconds", optional=True),
            )
        object.__setattr__(self, "error", _text(self.error, "error", optional=True))
        if self.media_type is not None:
            object.__setattr__(
                self, "media_type", _enum(self.media_type, MediaType, "media_type")
            )


@dataclass(frozen=True, slots=True)
class MediaStatus:
    """Bounded availability response for a normalized media identity."""

    identity: MediaIdentity
    available: bool
    title: str | None = None
    year: int | None = None
    quality: str | None = None
    plex_url: str | None = None
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, MediaIdentity):
            raise ModelValidationError("identity must be MediaIdentity")
        if not isinstance(self.available, bool):
            raise ModelValidationError("available must be a boolean")
        object.__setattr__(self, "title", _text(self.title, "title", optional=True))
        object.__setattr__(self, "year", _year(self.year))
        object.__setattr__(
            self, "quality", _text(self.quality, "quality", optional=True)
        )
        object.__setattr__(
            self, "plex_url", _text(self.plex_url, "plex_url", optional=True)
        )
        object.__setattr__(
            self, "as_of", _timestamp(self.as_of, "as_of", optional=True)
        )


@dataclass(frozen=True, slots=True)
class PartialError:
    """Safe dependency error summary returned alongside partial results."""

    service: ServiceName
    code: str
    message: str
    retryable: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "service", _enum(self.service, ServiceName, "service"))
        object.__setattr__(self, "code", _text(self.code, "code"))
        object.__setattr__(self, "message", _text(self.message, "message"))
        if not isinstance(self.retryable, bool):
            raise ModelValidationError("retryable must be a boolean")


RecordT = TypeVar("RecordT")


@dataclass(frozen=True, slots=True)
class Page(Generic[RecordT]):
    """A bounded normalized page shared by safe read tools."""

    items: tuple[RecordT, ...] = field(default_factory=tuple)
    as_of: datetime | None = None
    next_cursor: str | None = None
    truncated: bool = False
    total: int | None = None
    partial_errors: tuple[PartialError, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(
            self, "as_of", _timestamp(self.as_of, "as_of", optional=True)
        )
        object.__setattr__(
            self, "next_cursor", _text(self.next_cursor, "next_cursor", optional=True)
        )
        if not isinstance(self.truncated, bool):
            raise ModelValidationError("truncated must be a boolean")
        if self.total is not None:
            object.__setattr__(
                self, "total", _nonnegative_int(self.total, "total", optional=True)
            )
        object.__setattr__(self, "partial_errors", tuple(self.partial_errors))
        if any(not isinstance(error, PartialError) for error in self.partial_errors):
            raise ModelValidationError(
                "partial_errors must contain PartialError records"
            )


__all__ = [
    "Candidate",
    "DeliveryState",
    "DeliveryStatus",
    "MediaCandidate",
    "MediaIdentity",
    "MediaKind",
    "MediaRequest",
    "MediaStatus",
    "MediaType",
    "NotificationClass",
    "Page",
    "PartialError",
    "PlexItem",
    "PlexRecord",
    "QueueItem",
    "QueueRecord",
    "QueueState",
    "QueueStatus",
    "RequestMode",
    "Request",
    "RequestState",
    "RequestStatus",
    "ServiceName",
    "Service",
    "Status",
    "Identity",
    "canonical_rating_key",
]


# Concise aliases keep adapter code readable while all aliases retain the same
# validated dataclass and enum implementations.
Candidate = MediaCandidate
Identity = MediaIdentity
Request = MediaRequest
Status = MediaStatus
PlexRecord = PlexItem
QueueRecord = QueueItem
Service = ServiceName
