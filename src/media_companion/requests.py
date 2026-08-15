"""Durable request intent and provider-command workflow.

The workflow persists intent and idempotency keys before it mutates Radarr or
Sonarr.  Provider clients are protocols, so the application can plug in the
configured adapters (or deterministic fakes) without coupling authorization
or business logic to a particular HTTP implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import secrets
from collections.abc import Mapping, Sequence
from pathlib import Path
import sqlite3
from typing import Protocol, cast

from .db import ClaimToken, Database, utc_timestamp
from .errors import ConflictError, DependencyError, NotFoundError
from .enumeration import (
    EpisodeEnumeration,
    EpisodeRecord,
    EpisodeState,
    EnumerationError,
    enumerate_episodes,
)
from .models import MediaIdentity, MediaRequest, MediaType, RequestMode, RequestStatus
from .redaction import redact_text


MAX_TITLE_BYTES = 512
MAX_USERNAME_BYTES = 256
MAX_COMMAND_PAYLOAD_BYTES = 64 * 1024
MAX_REQUESTS_PAGE = 250
MAX_REQUESTED_SEASONS = 50
DEFAULT_COMMAND_CLAIM_SECONDS = 300
DEFAULT_CANDIDATE_TTL_SECONDS = 300
DEFAULT_PROVIDER_OPERATION_TTL_SECONDS = 900


class MovieProvider(Protocol):
    def find_existing_movie(self, tmdb_id: int) -> object | None: ...

    def lookup_movie(
        self, tmdb_id: int, *, query: str | None = None
    ) -> Sequence[object]: ...

    def add_movie(
        self, movie: object | Mapping[str, object], *, tmdb_id: int | None = None
    ) -> object: ...


class SeriesProvider(Protocol):
    def find_existing_series(self, tvdb_id: int) -> object | None: ...

    def lookup_series(
        self, tvdb_id: int, *, query: str | None = None
    ) -> Sequence[object]: ...

    def add_series(
        self,
        series: object | Mapping[str, object],
        *,
        tvdb_id: int | None = None,
        seasons: Sequence[int],
        anime: bool = False,
    ) -> object: ...

    def update_series(
        self,
        series: object | Mapping[str, object],
        *,
        seasons: Sequence[int],
        preserve_existing: bool = True,
    ) -> object: ...

    def search_season(
        self, series_id: int, season_number: int
    ) -> Mapping[str, object]: ...


class PlexVisibilityProvider(Protocol):
    def status_for_identity(self, identity: object) -> object: ...


class RequestStoreProtocol(Protocol):
    """Repository contract consumed by :class:`RequestWorkflow`."""

    def create_or_get_intent(
        self, intent: "IntentInput"
    ) -> tuple["RequestIntent", bool]: ...

    def get_intent(self, request_id: int) -> "RequestIntent": ...

    def create_command(self, command: "CommandInput") -> "RequestCommand": ...

    def list_commands(self, request_id: int) -> tuple["RequestCommand", ...]: ...

    def claim_command(
        self, command_id: int, *, lease_seconds: int = DEFAULT_COMMAND_CLAIM_SECONDS
    ) -> ClaimToken | None: ...

    def complete_command(
        self, command_id: int, claim: ClaimToken, *, external_id: str | None = None
    ) -> bool: ...

    def fail_command(
        self,
        command_id: int,
        claim: ClaimToken,
        message: str,
        *,
        retryable: bool = True,
    ) -> bool: ...

    def set_intent_state(
        self,
        request_id: int,
        status: RequestStatus,
        *,
        provider_item_id: str | None = None,
        error: str | None = None,
        expected_version: int | None = None,
    ) -> "RequestIntent": ...

    def ensure_subscription(
        self,
        intent: "RequestIntent",
        *,
        season_number: int | None = None,
        mode: RequestMode | None = None,
    ) -> int | None: ...

    def create_candidate(self, candidate: "CandidateInput") -> str: ...

    def resolve_candidate(self, handle: str) -> "CandidateHandle": ...

    def claim_provider_operation(
        self, operation: "ProviderOperationInput"
    ) -> tuple[bool, str | None, str]: ...

    def complete_provider_operation(
        self, operation_key: str, *, external_id: str | None = None
    ) -> None: ...

    def mark_provider_operation_unknown(self, operation_key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class RequestActor:
    """Trusted requester fields supplied by the actor assertion layer."""

    user_id: int
    chat_id: int
    username: str | None = None
    update_id: int | None = None
    chat_type: str = "private"
    update_type: str = "message"
    allowlist_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.user_id, bool)
            or not isinstance(self.user_id, int)
            or self.user_id <= 0
        ):
            raise ValueError("user_id must be a positive integer")
        if (
            isinstance(self.chat_id, bool)
            or not isinstance(self.chat_id, int)
            or self.chat_id == 0
        ):
            raise ValueError("chat_id must be a non-zero integer")
        if self.username is not None:
            object.__setattr__(
                self,
                "username",
                _safe_text(self.username, max_bytes=MAX_USERNAME_BYTES),
            )
        if self.update_id is not None:
            _nonnegative_int(self.update_id, "update_id")
        if not isinstance(self.chat_type, str):
            raise ValueError("chat_type is invalid")
        object.__setattr__(self, "chat_type", self.chat_type.strip().casefold())
        if self.chat_type not in {"private", "group", "supergroup", "channel"}:
            raise ValueError("chat_type is invalid")
        if not isinstance(self.update_type, str) or not self.update_type.strip():
            raise ValueError("update_type must not be blank")
        object.__setattr__(self, "update_type", self.update_type.strip()[:64])
        if self.allowlist_fingerprint is not None:
            object.__setattr__(
                self,
                "allowlist_fingerprint",
                _safe_text(self.allowlist_fingerprint, max_bytes=128),
            )


@dataclass(frozen=True, slots=True)
class CandidateInput:
    actor: RequestActor
    media_type: MediaType
    provider_id: int
    title: str
    query_hash: str
    year: int | None = None
    ttl_seconds: int = DEFAULT_CANDIDATE_TTL_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.actor, RequestActor):
            raise ValueError("candidate actor is required")
        if not isinstance(self.media_type, MediaType):
            object.__setattr__(self, "media_type", MediaType(self.media_type))
        object.__setattr__(
            self, "provider_id", _positive_int(self.provider_id, "provider_id")
        )
        object.__setattr__(
            self,
            "title",
            _safe_text(
                self.title,
                fallback=self.media_type.value.title(),
                max_bytes=MAX_TITLE_BYTES,
            ),
        )
        if (
            not isinstance(self.query_hash, str)
            or len(self.query_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.query_hash)
        ):
            raise ValueError("query_hash must be a SHA-256 hex digest")
        if self.year is not None:
            object.__setattr__(self, "year", _year(self.year))
        if (
            isinstance(self.ttl_seconds, bool)
            or not isinstance(self.ttl_seconds, int)
            or not 1 <= self.ttl_seconds <= DEFAULT_CANDIDATE_TTL_SECONDS
        ):
            raise ValueError("candidate TTL is invalid")


@dataclass(frozen=True, slots=True)
class CandidateHandle:
    handle: str
    actor: RequestActor
    media_type: MediaType
    provider_id: int
    title: str
    query_hash: str
    year: int | None
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderOperationInput:
    service: str
    provider: str
    provider_id: int
    request_id: int
    season_number: int | None = None
    ttl_seconds: int = DEFAULT_PROVIDER_OPERATION_TTL_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.service, str) or not self.service.strip():
            raise ValueError("service must not be blank")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must not be blank")
        object.__setattr__(self, "service", self.service.strip().casefold())
        object.__setattr__(self, "provider", self.provider.strip().casefold())
        _positive_int(self.provider_id, "provider_id")
        _positive_int(self.request_id, "request_id")
        if self.season_number is not None:
            _nonnegative_int(self.season_number, "season_number")
        if (
            isinstance(self.ttl_seconds, bool)
            or not isinstance(self.ttl_seconds, int)
            or not 1 <= self.ttl_seconds <= 3600
        ):
            raise ValueError("provider operation TTL is invalid")

    @property
    def operation_key(self) -> str:
        return _hash_key(
            "provider-operation",
            self.service,
            self.provider,
            self.provider_id,
            self.season_number,
        )


@dataclass(frozen=True, slots=True)
class IntentInput:
    media_type: MediaType
    provider_id: int
    title: str
    year: int | None = None
    seasons: tuple[int, ...] = ()
    actor: RequestActor | None = None
    idempotency_key: str | None = None
    mode: RequestMode | None = None
    anime: bool = False
    request_key: str | None = None
    candidate_handle: str | None = None
    query_hash: str | None = None
    plex_baseline: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.media_type, MediaType):
            object.__setattr__(self, "media_type", MediaType(self.media_type))
        if self.actor is not None and not isinstance(self.actor, RequestActor):
            raise ValueError("actor must be a RequestActor")
        parsed_provider = _positive_int(self.provider_id, "provider_id")
        object.__setattr__(self, "provider_id", parsed_provider)
        object.__setattr__(
            self,
            "title",
            _safe_text(
                self.title,
                fallback=self.media_type.value.title(),
                max_bytes=MAX_TITLE_BYTES,
            ),
        )
        if self.year is not None:
            object.__setattr__(self, "year", _year(self.year))
        object.__setattr__(
            self,
            "seasons",
            _seasons(self.seasons, required=self.media_type is MediaType.SERIES),
        )
        if self.mode is not None and not isinstance(self.mode, RequestMode):
            object.__setattr__(self, "mode", RequestMode(self.mode))
        if self.media_type is MediaType.MOVIE and self.mode not in {
            None,
            RequestMode.MOVIE,
        }:
            raise ValueError("movie intents require movie mode")
        if self.media_type is MediaType.SERIES and self.mode is RequestMode.MOVIE:
            raise ValueError("series intents cannot use movie mode")
        if not isinstance(self.anime, bool):
            raise ValueError("anime must be a boolean")
        if self.idempotency_key is not None:
            key = _safe_text(self.idempotency_key, max_bytes=512)
            if not key:
                raise ValueError("idempotency_key must not be blank")
            object.__setattr__(self, "idempotency_key", key)
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
                or any(
                    character
                    not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                    for character in self.candidate_handle
                )
            ):
                raise ValueError("candidate_handle is invalid")
        if self.query_hash is not None and (
            not isinstance(self.query_hash, str)
            or len(self.query_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.query_hash)
        ):
            raise ValueError("query_hash must be a SHA-256 hex digest")
        if self.plex_baseline is not None and not isinstance(
            self.plex_baseline, Mapping
        ):
            raise ValueError("plex_baseline must be a mapping")


@dataclass(frozen=True, slots=True)
class RequestIntent:
    request_id: int
    request_key: str
    idempotency_key: str
    media_type: MediaType
    provider_id: int
    title: str
    year: int | None
    seasons: tuple[int, ...]
    actor: RequestActor | None
    mode: RequestMode
    status: RequestStatus
    provider_item_id: str | None = None
    created_at: datetime | None = None
    error: str | None = None
    version: int = 0
    enumeration_versions: tuple[int, ...] = ()
    plex_baseline: Mapping[str, object] | None = None

    @property
    def media_request(self) -> MediaRequest:
        return MediaRequest(
            request_id=self.request_id,
            media_type=self.media_type,
            provider_id=self.provider_id,
            title=self.title,
            year=self.year,
            seasons=self.seasons,
            requested_by_user_id=self.actor.user_id if self.actor else None,
            requested_by_chat_id=self.actor.chat_id if self.actor else None,
            requested_by_username=self.actor.username if self.actor else None,
            mode=self.mode,
            status=self.status,
            created_at=self.created_at,
        )


@dataclass(frozen=True, slots=True)
class CommandInput:
    request_id: int
    command_type: str
    service: str
    idempotency_key: str
    provider_id: int | None = None
    season_number: int | None = None
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.request_id, bool)
            or not isinstance(self.request_id, int)
            or self.request_id <= 0
        ):
            raise ValueError("request_id must be positive")
        for field_name in ("command_type", "service", "idempotency_key"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be blank")
            object.__setattr__(self, field_name, value.strip())
        if self.provider_id is not None:
            object.__setattr__(
                self, "provider_id", _positive_int(self.provider_id, "provider_id")
            )
        if self.season_number is not None:
            object.__setattr__(
                self,
                "season_number",
                _nonnegative_int(self.season_number, "season_number"),
            )
        if not isinstance(self.payload, Mapping):
            raise ValueError("command payload must be a mapping")
        encoded = _json_bytes(self.payload)
        if len(encoded) > MAX_COMMAND_PAYLOAD_BYTES:
            raise ValueError("command payload exceeds the bounded size")


@dataclass(frozen=True, slots=True)
class RequestCommand:
    command_id: int
    request_id: int
    command_type: str
    service: str
    idempotency_key: str
    provider_id: int | None
    season_number: int | None
    payload: Mapping[str, object]
    status: str
    attempts: int = 0
    external_id: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class RequestWorkflowResult:
    intent: RequestIntent
    created: bool
    commands: tuple[RequestCommand, ...] = ()
    subscriptions: tuple[int, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.intent.status in {
            RequestStatus.REQUESTED,
            RequestStatus.ACCEPTED,
            RequestStatus.DOWNLOADING,
            RequestStatus.IMPORTED_TO_ARR,
            RequestStatus.VISIBLE_IN_PLEX,
            RequestStatus.DELIVERED,
        }


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _year(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1800 <= value <= 3000
    ):
        raise ValueError("year must be between 1800 and 3000")
    return value


def _seasons(value: Sequence[int], *, required: bool) -> tuple[int, ...]:
    if value is None:  # type: ignore[comparison-overlap]
        if required:
            raise ValueError("seasons must be an explicit non-empty list")
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError("seasons must contain integers")
    values = tuple(value)
    if not values and required:
        raise ValueError("seasons must be an explicit non-empty list")
    result: set[int] = set()
    for season in values:
        result.add(_nonnegative_int(season, "season"))
    if len(result) > MAX_REQUESTED_SEASONS:
        raise ValueError(
            f"seasons cannot contain more than {MAX_REQUESTED_SEASONS} values"
        )
    return tuple(sorted(result))


def _safe_text(value: object, *, fallback: str = "", max_bytes: int) -> str:
    if not isinstance(value, str):
        return fallback
    result = redact_text(value, max_bytes=max_bytes).strip()
    return result or fallback


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not bounded JSON") from exc


def _hash_key(*parts: object) -> str:
    payload = _json_bytes(parts)
    return hashlib.sha256(payload).hexdigest()


def query_hash(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must not be blank")
    return hashlib.sha256(
        query.strip().casefold().encode("utf-8", "strict")
    ).hexdigest()


def _actor_input(
    value: RequestActor | Mapping[str, object] | object | None,
    *,
    user_id: int | None = None,
    chat_id: int | None = None,
    username: str | None = None,
    update_id: int | None = None,
    chat_type: str = "private",
    update_type: str = "message",
    allowlist_fingerprint: str | None = None,
) -> RequestActor | None:
    if value is not None:
        if isinstance(value, RequestActor):
            return value
        if isinstance(value, Mapping):
            if user_id is None:
                raw_user_id = value.get("user_id", value.get("requested_by_user_id"))
                user_id = (
                    raw_user_id
                    if isinstance(raw_user_id, int)
                    and not isinstance(raw_user_id, bool)
                    else None
                )
            if chat_id is None:
                raw_chat_id = value.get("chat_id", value.get("requested_by_chat_id"))
                chat_id = (
                    raw_chat_id
                    if isinstance(raw_chat_id, int)
                    and not isinstance(raw_chat_id, bool)
                    else None
                )
            if username is None:
                raw_username = value.get("username", value.get("requested_by_username"))
                username = raw_username if isinstance(raw_username, str) else None
            if update_id is None:
                raw_update_id = value.get("update_id", value.get("actor_update_id"))
                update_id = (
                    raw_update_id
                    if isinstance(raw_update_id, int)
                    and not isinstance(raw_update_id, bool)
                    else None
                )
            if chat_type == "private" and isinstance(value.get("chat_type"), str):
                chat_type = str(value["chat_type"])
            if update_type == "message" and isinstance(value.get("update_type"), str):
                update_type = str(value["update_type"])
            if allowlist_fingerprint is None and isinstance(
                value.get("allowlist_fingerprint"), str
            ):
                allowlist_fingerprint = str(value["allowlist_fingerprint"])
        else:
            user_id = (
                getattr(
                    value, "user_id", getattr(value, "requested_by_user_id", user_id)
                )
                if user_id is None
                else user_id
            )
            chat_id = (
                getattr(
                    value, "chat_id", getattr(value, "requested_by_chat_id", chat_id)
                )
                if chat_id is None
                else chat_id
            )
            username = (
                getattr(
                    value, "username", getattr(value, "requested_by_username", username)
                )
                if username is None
                else username
            )
            update_id = (
                getattr(
                    value, "update_id", getattr(value, "actor_update_id", update_id)
                )
                if update_id is None
                else update_id
            )
            chat_type = getattr(value, "chat_type", chat_type)
            update_type = getattr(value, "update_type", update_type)
            allowlist_fingerprint = getattr(
                value, "allowlist_fingerprint", allowlist_fingerprint
            )
    if user_id is None and chat_id is None:
        return None
    if user_id is None or chat_id is None:
        raise ValueError("user_id and chat_id are required together")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("user_id must be a positive integer")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
        raise ValueError("chat_id must be a non-zero integer")
    return RequestActor(
        user_id,
        chat_id,
        username if isinstance(username, str) else None,
        update_id,
        chat_type,
        update_type,
        allowlist_fingerprint,
    )


def _object_field(value: object, *names: str) -> object | None:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _provider_id(value: object, *names: str) -> int | None:
    raw = _object_field(value, *names)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def _stable_provider_id(value: object, media_type: MediaType) -> int | None:
    """Read only the requested namespaced provider identity."""

    field = "tmdb_id" if media_type is MediaType.MOVIE else "tvdb_id"
    api_field = "tmdbId" if media_type is MediaType.MOVIE else "tvdbId"
    return _provider_id(value, field, api_field)


def _call_supported(function: object, *args: object, **kwargs: object) -> object:
    if not callable(function):
        raise DependencyError("provider method is unavailable")
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args, **kwargs)  # type: ignore[misc]
    accepted = signature.parameters
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in accepted.values()
    ):
        return function(*args, **kwargs)  # type: ignore[misc]
    filtered = {key: value for key, value in kwargs.items() if key in accepted}
    return function(*args, **filtered)  # type: ignore[misc]


def _lookup_movie(
    provider: MovieProvider, provider_id: int, title: str
) -> Sequence[object]:
    result = _call_supported(
        provider.lookup_movie, provider_id, query=f"tmdb:{provider_id}"
    )
    return cast(Sequence[object], result)


def _add_movie(provider: MovieProvider, metadata: object, provider_id: int) -> object:
    return _call_supported(provider.add_movie, metadata, tmdb_id=provider_id)


def _lookup_series(
    provider: SeriesProvider, provider_id: int, title: str
) -> Sequence[object]:
    result = _call_supported(
        provider.lookup_series, provider_id, query=f"tvdb:{provider_id}"
    )
    return cast(Sequence[object], result)


def _add_series(
    provider: SeriesProvider,
    metadata: object,
    provider_id: int,
    seasons: Sequence[int],
    anime: bool,
) -> object:
    return _call_supported(
        provider.add_series, metadata, tvdb_id=provider_id, seasons=seasons, anime=anime
    )


class SQLiteRequestStore:
    """SQLite implementation of the request workflow repository protocol."""

    def __init__(
        self, database: Database | str | Path, *, migrate: bool = True
    ) -> None:
        self.database = (
            database if isinstance(database, Database) else Database(Path(database))
        )
        if migrate:
            self.database.migrate()

    def create_candidate(self, candidate: CandidateInput) -> str:
        """Persist a short-lived normalized search candidate.

        Only a hash of the opaque handle is stored.  The handle therefore
        cannot be enumerated from the database or logs, while its actor/query
        binding remains durable across worker restarts.
        """

        raw_handle = secrets.token_urlsafe(32)
        handle_hash = hashlib.sha256(raw_handle.encode("ascii")).hexdigest()
        now = datetime.now(timezone.utc)
        issued = utc_timestamp(now)
        expires = utc_timestamp(now + timedelta(seconds=candidate.ttl_seconds))
        payload = _json_bytes(
            {
                "title": candidate.title,
                "year": candidate.year,
                "chat_type": candidate.actor.chat_type,
                "update_type": candidate.actor.update_type,
                "allowlist_fingerprint": candidate.actor.allowlist_fingerprint,
            }
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO request_candidates(
                    handle_hash, actor_user_id, actor_chat_id, actor_update_id,
                    media_type, provider_id, title, year, query_hash,
                    payload_json, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handle_hash,
                    candidate.actor.user_id,
                    candidate.actor.chat_id,
                    candidate.actor.update_id,
                    candidate.media_type.value,
                    str(candidate.provider_id),
                    candidate.title,
                    candidate.year,
                    candidate.query_hash,
                    payload.decode("utf-8"),
                    issued,
                    expires,
                ),
            )
        return raw_handle

    def resolve_candidate(self, handle: str) -> CandidateHandle:
        if not isinstance(handle, str) or not 20 <= len(handle) <= 128:
            raise ConflictError("candidate handle is invalid")
        try:
            handle_hash = hashlib.sha256(handle.encode("ascii", "strict")).hexdigest()
        except UnicodeError as exc:
            raise ConflictError("candidate handle is invalid") from exc
        now = utc_timestamp()
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM request_candidates WHERE handle_hash = ? AND expires_at > ?",
                (handle_hash, now),
            ).fetchone()
        if row is None:
            raise ConflictError("candidate handle is expired or unknown")
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConflictError("candidate handle payload is invalid") from exc
        title = payload.get("title") if isinstance(payload, Mapping) else row["title"]
        year = payload.get("year") if isinstance(payload, Mapping) else row["year"]
        chat_type = (
            payload.get("chat_type", "private")
            if isinstance(payload, Mapping)
            else "private"
        )
        update_type = (
            payload.get("update_type", "message")
            if isinstance(payload, Mapping)
            else "message"
        )
        allowlist_fingerprint = (
            payload.get("allowlist_fingerprint")
            if isinstance(payload, Mapping)
            else None
        )
        stored_query_hash = row["query_hash"]
        if (
            not isinstance(stored_query_hash, str)
            or len(stored_query_hash) != 64
            or any(
                character not in "0123456789abcdef" for character in stored_query_hash
            )
        ):
            raise ConflictError("candidate handle query binding is invalid")
        return CandidateHandle(
            handle,
            RequestActor(
                int(row["actor_user_id"]),
                int(row["actor_chat_id"]),
                None,
                int(row["actor_update_id"])
                if row["actor_update_id"] is not None
                else None,
                chat_type if isinstance(chat_type, str) else "private",
                update_type if isinstance(update_type, str) else "message",
                allowlist_fingerprint
                if isinstance(allowlist_fingerprint, str)
                else None,
            ),
            MediaType(str(row["media_type"])),
            _positive_int(int(str(row["provider_id"])), "provider_id"),
            _safe_text(
                title,
                fallback=MediaType(str(row["media_type"])).value.title(),
                max_bytes=MAX_TITLE_BYTES,
            ),
            stored_query_hash,
            _year(year) if isinstance(year, int) else None,
            _parse_timestamp(row["issued_at"]) or datetime.now(timezone.utc),
            _parse_timestamp(row["expires_at"]) or datetime.now(timezone.utc),
        )

    def claim_provider_operation(
        self, operation: ProviderOperationInput
    ) -> tuple[bool, str | None, str]:
        now = datetime.now(timezone.utc)
        now_text = utc_timestamp(now)
        expires = utc_timestamp(now + timedelta(seconds=operation.ttl_seconds))
        key = operation.operation_key
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status, external_id, expires_at FROM provider_operations WHERE operation_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO provider_operations(
                        operation_key, service, provider, provider_id,
                        season_number, status, owner_request_id, expires_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        key,
                        operation.service,
                        operation.provider,
                        str(operation.provider_id),
                        operation.season_number,
                        operation.request_id,
                        expires,
                        now_text,
                        now_text,
                    ),
                )
                return True, None, "pending"
            status = str(row["status"])
            if status == "succeeded":
                return (
                    False,
                    str(row["external_id"]) if row["external_id"] is not None else None,
                    status,
                )
            if status == "unknown":
                # A provider command whose response was lost is never blindly
                # resent.  The reconciliation worker must establish provider
                # truth first or an operator must explicitly recover it.
                return False, None, status
            if status == "pending":
                if str(row["expires_at"]) > now_text:
                    return False, None, status
                # An expired owner may have completed the provider mutation
                # immediately before losing its response.  Quarantine the
                # operation for stable-ID reconciliation instead of issuing a
                # duplicate add/search.
                connection.execute(
                    "UPDATE provider_operations SET status = 'unknown', updated_at = ? WHERE operation_key = ?",
                    (now_text, key),
                )
                return False, None, "unknown"
            raise ConflictError("provider operation has an invalid state")

    def complete_provider_operation(
        self, operation_key: str, *, external_id: str | None = None
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE provider_operations SET status = 'succeeded', external_id = ?, updated_at = ? WHERE operation_key = ?",
                (external_id, utc_timestamp(), operation_key),
            )

    def mark_provider_operation_unknown(self, operation_key: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE provider_operations SET status = 'unknown', updated_at = ? WHERE operation_key = ?",
                (utc_timestamp(), operation_key),
            )

    def create_or_get_intent(self, intent: IntentInput) -> tuple[RequestIntent, bool]:
        request_key = intent.request_key or _hash_key(
            "request",
            intent.media_type.value,
            intent.provider_id,
            intent.title,
            intent.year,
            intent.seasons,
            intent.anime,
            intent.mode.value if intent.mode is not None else "",
            intent.actor.user_id if intent.actor else "",
            intent.actor.chat_id if intent.actor else "",
            intent.actor.update_id if intent.actor else "",
            intent.query_hash or "",
        )
        # Scope a caller-provided retry key to the canonical intent identity
        # and trusted actor.  A key reused by another chat/user or for another
        # provider object must not alias the first request, while retries of
        # the exact same intent still resolve to one durable row.
        idem = _hash_key(
            "idempotency",
            intent.idempotency_key or request_key,
            intent.media_type.value,
            intent.provider_id,
            intent.title,
            intent.year,
            intent.seasons,
            intent.actor.user_id if intent.actor else "",
            intent.actor.chat_id if intent.actor else "",
            intent.actor.update_id if intent.actor else "",
        )
        mode = intent.mode or (
            RequestMode.MOVIE
            if intent.media_type is MediaType.MOVIE
            else RequestMode.SEASON_COMPLETION
        )
        now = utc_timestamp()
        payload: dict[str, object] = {"anime": intent.anime, "request_key_version": 2}
        if intent.actor is not None:
            payload["actor_context"] = {
                "chat_type": intent.actor.chat_type,
                "update_type": intent.actor.update_type,
                "allowlist_fingerprint": intent.actor.allowlist_fingerprint,
            }
        if intent.query_hash:
            payload["query_hash"] = intent.query_hash
        if intent.candidate_handle:
            payload["candidate_handle_hash"] = hashlib.sha256(
                intent.candidate_handle.encode("utf-8")
            ).hexdigest()
        if intent.plex_baseline is not None:
            payload["plex_baseline"] = dict(intent.plex_baseline)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE idempotency_key = ?", (idem,)
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM requests WHERE request_key = ?", (request_key,)
                ).fetchone()
            if row is not None:
                if intent.actor is not None:
                    if (
                        row["requested_by_user_id"] != intent.actor.user_id
                        or row["requested_by_chat_id"] != intent.actor.chat_id
                    ):
                        raise ConflictError("request key is bound to another actor")
                    if row["actor_update_id"] != intent.actor.update_id:
                        raise ConflictError("request key is bound to another update")
                elif (
                    row["requested_by_user_id"] is not None
                    or row["requested_by_chat_id"] is not None
                ):
                    raise ConflictError("request key requires the originating actor")
                if str(row["media_type"]) != intent.media_type.value or str(
                    row["provider_id"]
                ) != str(intent.provider_id):
                    raise ConflictError(
                        "request key is bound to another media identity"
                    )
                if str(row["title"]) != intent.title or row["year"] != intent.year:
                    raise ConflictError(
                        "request key is bound to another media description"
                    )
                stored_seasons = row["seasons_json"]
                try:
                    parsed_seasons = (
                        json.loads(stored_seasons)
                        if isinstance(stored_seasons, str) and stored_seasons
                        else []
                    )
                except json.JSONDecodeError:
                    parsed_seasons = []
                if tuple(parsed_seasons) != intent.seasons:
                    raise ConflictError("request key is bound to another season scope")
                try:
                    stored_payload = (
                        json.loads(row["payload_json"])
                        if isinstance(row["payload_json"], str)
                        else {}
                    )
                except json.JSONDecodeError:
                    stored_payload = {}
                if isinstance(stored_payload, Mapping):
                    if (
                        intent.query_hash is not None
                        and stored_payload.get("query_hash") != intent.query_hash
                    ):
                        raise ConflictError(
                            "request key is bound to another search query"
                        )
                    if intent.candidate_handle is not None:
                        expected_candidate_hash = hashlib.sha256(
                            intent.candidate_handle.encode("utf-8")
                        ).hexdigest()
                        if (
                            stored_payload.get("candidate_handle_hash")
                            != expected_candidate_hash
                        ):
                            raise ConflictError(
                                "request key is bound to another candidate"
                            )
                return _intent_from_row(row), False
            if intent.actor is not None and intent.actor.update_id is not None:
                prior = connection.execute(
                    "SELECT * FROM requests WHERE requested_by_user_id = ? AND requested_by_chat_id = ? AND actor_update_id = ?",
                    (
                        intent.actor.user_id,
                        intent.actor.chat_id,
                        intent.actor.update_id,
                    ),
                ).fetchone()
                if prior is not None:
                    if str(prior["idempotency_key"]) == idem:
                        return _intent_from_row(prior), False
                    raise ConflictError(
                        "one safe request mutation is allowed per actor update"
                    )
            try:
                values: dict[str, object] = {
                    "request_key": request_key,
                    "user_id": intent.actor.user_id if intent.actor else None,
                    "chat_id": intent.actor.chat_id if intent.actor else None,
                    "username": intent.actor.username if intent.actor else None,
                    "requested_by_user_id": intent.actor.user_id
                    if intent.actor
                    else None,
                    "requested_by_chat_id": intent.actor.chat_id
                    if intent.actor
                    else None,
                    "requested_by_username": intent.actor.username
                    if intent.actor
                    else None,
                    "actor_update_id": intent.actor.update_id if intent.actor else None,
                    "media_type": intent.media_type.value,
                    "provider_id": str(intent.provider_id),
                    "tmdb_id": intent.provider_id
                    if intent.media_type is MediaType.MOVIE
                    else None,
                    "tvdb_id": intent.provider_id
                    if intent.media_type is MediaType.SERIES
                    else None,
                    "title": intent.title,
                    "year": intent.year,
                    "seasons_json": json.dumps(
                        list(intent.seasons), separators=(",", ":")
                    )
                    if intent.seasons
                    else None,
                    "mode": mode.value,
                    "status": RequestStatus.REQUESTED.value,
                    "idempotency_key": idem,
                    "payload_json": json.dumps(payload, separators=(",", ":")),
                    "plex_baseline_json": json.dumps(
                        intent.plex_baseline, separators=(",", ":")
                    )
                    if intent.plex_baseline is not None
                    else None,
                    "created_at": now,
                    "updated_at": now,
                }
                columns = {
                    str(info[1])
                    for info in connection.execute(
                        "PRAGMA table_info(requests)"
                    ).fetchall()
                }
                insert_values = {
                    key: value for key, value in values.items() if key in columns
                }
                if (
                    "request_key" not in insert_values
                    or "media_type" not in insert_values
                ):
                    raise ConflictError(
                        "request ledger schema is missing required columns"
                    )
                names = tuple(insert_values)
                placeholders = ", ".join("?" for _ in names)
                cursor = connection.execute(
                    f"INSERT INTO requests ({', '.join(names)}) VALUES ({placeholders})",
                    tuple(insert_values[name] for name in names),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT * FROM requests WHERE idempotency_key = ? OR request_key = ?",
                    (idem, request_key),
                ).fetchone()
                if row is None:
                    raise ConflictError("request idempotency conflict")
                return _intent_from_row(row), False
            row = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            if row is None:
                raise ConflictError("request intent was not persisted")
            return _intent_from_row(row), True

    def get_intent(self, request_id: int) -> RequestIntent:
        request_id = _positive_int(request_id, "request_id")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("request intent was not found")
        return _intent_from_row(row)

    def create_command(self, command: CommandInput) -> RequestCommand:
        payload_json = _json_bytes(command.payload).decode("utf-8")
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO request_commands (
                    request_id, command_type, service, idempotency_key,
                    provider_id, season_number, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    command.request_id,
                    command.command_type,
                    command.service,
                    command.idempotency_key,
                    command.provider_id,
                    command.season_number,
                    payload_json,
                ),
            )
            row = connection.execute(
                "SELECT * FROM request_commands WHERE idempotency_key = ?",
                (command.idempotency_key,),
            ).fetchone()
        if row is None:
            raise ConflictError("request command was not persisted")
        if int(row[1]) != command.request_id:
            raise ConflictError(
                "request command idempotency key belongs to another request"
            )
        return _command_from_row(row)

    def list_commands(self, request_id: int) -> tuple[RequestCommand, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM request_commands WHERE request_id = ? ORDER BY id",
                (request_id,),
            ).fetchall()
        return tuple(_command_from_row(row) for row in rows)

    def claim_command(
        self, command_id: int, *, lease_seconds: int = DEFAULT_COMMAND_CLAIM_SECONDS
    ) -> ClaimToken | None:
        return self.database.claim(
            "request_commands",
            _positive_int(command_id, "command_id"),
            lease_seconds=lease_seconds,
            allowed_statuses=("pending", "retry_wait", "claimed", "running"),
        )

    def complete_command(
        self, command_id: int, claim: ClaimToken, *, external_id: str | None = None
    ) -> bool:
        updates = {"external_id": external_id} if external_id is not None else None
        return self.database.complete_claim(
            "request_commands", command_id, claim, status="succeeded", updates=updates
        )

    def mark_command_unknown(
        self, command_id: int, claim: ClaimToken, message: str
    ) -> bool:
        return self.database.complete_claim(
            "request_commands",
            command_id,
            claim,
            status="unknown",
            updates={"last_error": redact_text(message, max_bytes=512)},
        )

    def resolve_unknown_command(self, command_id: int, *, external_id: str) -> bool:
        with self.database.transaction() as connection:
            result = connection.execute(
                "UPDATE request_commands SET status = 'succeeded', external_id = ?, claim_token = NULL, claim_expires_at = NULL, last_error = NULL, version = version + 1, updated_at = ? WHERE id = ? AND status = 'unknown'",
                (str(external_id), utc_timestamp(), command_id),
            )
            return result.rowcount == 1

    def fail_command(
        self,
        command_id: int,
        claim: ClaimToken,
        message: str,
        *,
        retryable: bool = True,
    ) -> bool:
        safe = redact_text(message, max_bytes=512)
        return self.database.release_claim(
            "request_commands",
            command_id,
            claim,
            status="retry_wait" if retryable else "failed",
            error=safe,
        )

    def set_intent_state(
        self,
        request_id: int,
        status: RequestStatus,
        *,
        provider_item_id: str | None = None,
        error: str | None = None,
        expected_version: int | None = None,
    ) -> RequestIntent:
        if not isinstance(status, RequestStatus):
            status = RequestStatus(status)
        request_id = _positive_int(request_id, "request_id")
        if expected_version is not None:
            _nonnegative_int(expected_version, "expected_version")
        updated_at = utc_timestamp()
        with self.database.transaction() as connection:
            assignments = ["status = ?", "updated_at = ?", "version = version + 1"]
            params: list[object] = [status.value, updated_at]
            if provider_item_id is not None:
                assignments.append("provider_item_id = ?")
                params.append(str(provider_item_id))
            if error is not None:
                current_row = connection.execute(
                    "SELECT payload_json FROM requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                current_payload: dict[str, object] = {}
                if current_row is not None and isinstance(current_row[0], str):
                    try:
                        parsed_payload = json.loads(current_row[0])
                        if isinstance(parsed_payload, Mapping):
                            current_payload.update(parsed_payload)
                    except json.JSONDecodeError:
                        pass
                current_payload["error"] = redact_text(error, max_bytes=512)
                assignments.append("payload_json = ?")
                params.append(json.dumps(current_payload, separators=(",", ":")))
            params.append(request_id)
            predicate = "id = ?"
            if expected_version is not None:
                predicate += " AND version = ?"
                params.append(expected_version)
            result = connection.execute(
                f"UPDATE requests SET {', '.join(assignments)} WHERE {predicate}",
                params,
            )
            if result.rowcount != 1:
                raise ConflictError("request intent state was fenced by another worker")
        return self.get_intent(request_id)

    def set_intent_mode(
        self, request_id: int, mode: RequestMode, *, expected_version: int | None = None
    ) -> RequestIntent:
        if not isinstance(mode, RequestMode):
            mode = RequestMode(mode)
        request_id = _positive_int(request_id, "request_id")
        if expected_version is not None:
            _nonnegative_int(expected_version, "expected_version")
        with self.database.transaction() as connection:
            params: list[object] = [mode.value, utc_timestamp(), request_id]
            predicate = "id = ?"
            if expected_version is not None:
                predicate += " AND version = ?"
                params.append(expected_version)
            result = connection.execute(
                f"UPDATE requests SET mode = ?, updated_at = ?, version = version + 1 WHERE {predicate}",
                params,
            )
            if result.rowcount != 1:
                raise ConflictError("request mode was fenced by another worker")
        return self.get_intent(request_id)

    def record_enumeration_version(
        self, request_id: int, version: int
    ) -> RequestIntent:
        """Append an authoritative enumeration version under the intent fence."""

        request_id = _positive_int(request_id, "request_id")
        _positive_int(version, "enumeration_version")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("request intent was not found")
            try:
                payload = json.loads(row[0]) if isinstance(row[0], str) else {}
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            raw_versions = payload.get("enumeration_versions", [])
            versions: list[int] = []
            if isinstance(raw_versions, list):
                versions = [
                    item
                    for item in raw_versions
                    if isinstance(item, int) and not isinstance(item, bool) and item > 0
                ]
            if version not in versions:
                versions.append(version)
                versions.sort()
                payload["enumeration_versions"] = versions
                connection.execute(
                    "UPDATE requests SET payload_json = ?, version = version + 1, updated_at = ? WHERE id = ?",
                    (
                        json.dumps(payload, separators=(",", ":")),
                        utc_timestamp(),
                        request_id,
                    ),
                )
        return self.get_intent(request_id)

    def ensure_subscription(
        self,
        intent: RequestIntent,
        *,
        season_number: int | None = None,
        mode: RequestMode | None = None,
    ) -> int | None:
        if intent.actor is None:
            return None
        if intent.media_type is MediaType.SERIES:
            season_number = (
                _nonnegative_int(season_number, "season_number")
                if season_number is not None
                else None
            )
            if season_number is None:
                raise ValueError("series subscriptions require a season")
        elif season_number is not None:
            raise ValueError("movie subscriptions do not have a season")
        effective_mode = mode or intent.mode
        if not isinstance(effective_mode, RequestMode):
            effective_mode = RequestMode(effective_mode)
        baseline = (
            1
            if intent.plex_baseline and intent.plex_baseline.get("available") is True
            else 0
        )
        provider_id = str(intent.provider_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM subscriptions WHERE user_id = ? AND chat_id = ? AND destination = ? AND notification_class = 'requester' AND provider_id = ? AND media_type = ? AND season_number IS ? AND status IN ('active', 'fulfilled') ORDER BY generation DESC LIMIT 1",
                (
                    intent.actor.user_id,
                    intent.actor.chat_id,
                    str(intent.actor.chat_id),
                    provider_id,
                    intent.media_type.value,
                    season_number,
                ),
            ).fetchone()
            if row is not None:
                subscription_id = int(row[0])
                if baseline:
                    connection.execute(
                        "UPDATE subscriptions SET baseline = 1, updated_at = ? WHERE id = ? AND baseline = 0",
                        (utc_timestamp(), subscription_id),
                    )
            else:
                generation_row = connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) FROM subscriptions WHERE user_id = ? AND chat_id = ? AND destination = ? AND notification_class = 'requester' AND provider_id = ? AND media_type = ? AND season_number IS ?",
                    (
                        intent.actor.user_id,
                        intent.actor.chat_id,
                        str(intent.actor.chat_id),
                        provider_id,
                        intent.media_type.value,
                        season_number,
                    ),
                ).fetchone()
                generation = int(generation_row[0] or 0) + 1
                cursor = connection.execute(
                    """
                    INSERT INTO subscriptions (
                        request_id, user_id, chat_id, destination, notification_class,
                        media_type, provider_id, tmdb_id, tvdb_id, season_number,
                        mode, generation, baseline, status
                    ) VALUES (?, ?, ?, ?, 'requester', ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        intent.request_id,
                        intent.actor.user_id,
                        intent.actor.chat_id,
                        str(intent.actor.chat_id),
                        intent.media_type.value,
                        provider_id,
                        intent.provider_id
                        if intent.media_type is MediaType.MOVIE
                        else None,
                        intent.provider_id
                        if intent.media_type is MediaType.SERIES
                        else None,
                        season_number,
                        effective_mode.value,
                        generation,
                        baseline,
                    ),
                )
                if cursor.lastrowid is None:
                    raise ConflictError("subscription was not persisted")
                subscription_id = int(cursor.lastrowid)
            unit_key = (
                f"{intent.media_type.value}:{intent.provider_id}"
                if season_number is None
                else f"{intent.media_type.value}:{intent.provider_id}:season:{season_number}"
            )
            connection.execute(
                "INSERT INTO subscription_units (subscription_id, logical_unit_key, unit_type, provider_id, season_number, expected) VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(subscription_id, logical_unit_key) DO NOTHING",
                (
                    subscription_id,
                    unit_key,
                    "movie" if season_number is None else "season",
                    provider_id,
                    season_number,
                ),
            )
        return subscription_id

    def ensure_episode_units(
        self, subscription_id: int, enumeration: EpisodeEnumeration
    ) -> None:
        """Materialize one versioned episode set under a shared subscription."""

        subscription_id = _positive_int(subscription_id, "subscription_id")
        with self.database.transaction() as connection:
            for episode in enumeration.episodes:
                connection.execute(
                    """
                    INSERT INTO subscription_units(
                        subscription_id, logical_unit_key, unit_type, provider_id,
                        season_number, episode_number, expected,
                        enumeration_version, metadata_json
                    ) VALUES (?, ?, 'episode', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(subscription_id, logical_unit_key) DO UPDATE SET
                        expected = excluded.expected,
                        enumeration_version = excluded.enumeration_version,
                        metadata_json = excluded.metadata_json,
                        updated_at = excluded.updated_at,
                        version = subscription_units.version + 1
                    """,
                    (
                        subscription_id,
                        episode.unit_key,
                        episode.provider_id,
                        episode.season_number,
                        episode.episode_number,
                        0 if episode.state.terminal_exclusion else 1,
                        enumeration.version,
                        json.dumps(
                            episode.as_dict(), sort_keys=True, separators=(",", ":")
                        ),
                    ),
                )

    def save_enumeration(
        self, provider: str, provider_id: int, snapshot: EpisodeEnumeration
    ) -> None:
        episodes_json = snapshot.sanitized_json()
        evidence = json.dumps(
            {
                "diagnostics": list(snapshot.diagnostics),
                "snapshot_hash": snapshot.snapshot_hash,
            },
            separators=(",", ":"),
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO episode_enumerations(
                    provider, provider_id, season_number, version,
                    episodes_json, expected_count, authoritative, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, provider_id, season_number, version) DO UPDATE SET
                    episodes_json = excluded.episodes_json,
                    expected_count = excluded.expected_count,
                    authoritative = excluded.authoritative,
                    evidence_json = excluded.evidence_json
                """,
                (
                    provider,
                    str(provider_id),
                    snapshot.season_number,
                    snapshot.version,
                    episodes_json,
                    snapshot.expected_count,
                    int(snapshot.authoritative),
                    evidence,
                ),
            )
            for reason in snapshot.diagnostics:
                row_id = f"{provider_id}:{snapshot.season_number}:{snapshot.version}"
                connection.execute(
                    """
                    INSERT INTO quarantined_records(
                        source, source_name, source_table, source_id, source_row_id,
                        record_type, reason_code, reason, detail_json, payload_json
                    )
                    SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM quarantined_records
                        WHERE source = ? AND source_table = ? AND source_row_id = ?
                              AND reason_code = ? AND status = 'open'
                    )
                    """,
                    (
                        provider,
                        "episode_enumeration",
                        "episode_enumerations",
                        str(provider_id),
                        row_id,
                        "episode_enumeration",
                        reason,
                        "episode enumeration requires reconciliation",
                        evidence,
                        episodes_json,
                        provider,
                        "episode_enumerations",
                        row_id,
                        reason,
                    ),
                )

    def latest_enumeration(
        self, provider: str, provider_id: int, season_number: int
    ) -> EpisodeEnumeration | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM episode_enumerations WHERE provider = ? AND provider_id = ? AND season_number = ? ORDER BY version DESC LIMIT 1",
                (provider, str(provider_id), season_number),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["episodes_json"]))
            if not isinstance(payload, Mapping):
                return None
            records = payload.get("episodes", [])
            if not isinstance(records, Sequence) or isinstance(
                records, (str, bytes, bytearray)
            ):
                return None
            episodes: list[EpisodeRecord] = []
            for record in records:
                if not isinstance(record, Mapping):
                    return None
                raw_season = record.get("season_number")
                raw_episode = record.get("episode_number")
                if (
                    isinstance(raw_season, bool)
                    or not isinstance(raw_season, int)
                    or isinstance(raw_episode, bool)
                    or not isinstance(raw_episode, int)
                ):
                    return None
                episodes.append(
                    EpisodeRecord(
                        str(record.get("provider_id")),
                        raw_season,
                        raw_episode,
                        record.get("title"),
                        record.get("air_date"),
                        EpisodeState(str(record.get("state"))),
                        record.get("has_file"),
                        record.get("cancellation_reason"),
                        bool(record.get("authoritative")),
                        record.get("monitored"),
                        record.get("fingerprint"),
                    )
                )
            return EpisodeEnumeration(
                provider=provider,
                provider_id=str(provider_id),
                season_number=season_number,
                version=int(row["version"]),
                episodes=tuple(episodes),
                expected_count=row["expected_count"],
                authoritative=bool(row["authoritative"]),
                season_ended=payload.get("season_ended") is True,
                requested_explicitly=payload.get("requested_explicitly") is True,
                source=str(payload.get("source") or "stored"),
                diagnostics=tuple(
                    item
                    for item in payload.get("diagnostics", ())
                    if isinstance(item, str)
                ),
                snapshot_hash=str(payload.get("snapshot_hash") or ""),
                mode=RequestMode(
                    str(payload.get("mode") or RequestMode.AIRING_EPISODE.value)
                ),
            )
        except (ValueError, TypeError, EnumerationError, json.JSONDecodeError):
            return None

    def subscription_ids(self, request_id: int) -> tuple[int, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM subscriptions WHERE request_id = ? ORDER BY id",
                (request_id,),
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def subscription_ids_for_intent(self, intent: RequestIntent) -> tuple[int, ...]:
        if intent.actor is None:
            return self.subscription_ids(intent.request_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM subscriptions WHERE (request_id = ? OR (user_id = ? AND chat_id = ? AND destination = ? AND notification_class = 'requester' AND provider_id = ? AND media_type = ?)) ORDER BY id",
                (
                    intent.request_id,
                    intent.actor.user_id,
                    intent.actor.chat_id,
                    str(intent.actor.chat_id),
                    str(intent.provider_id),
                    intent.media_type.value,
                ),
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def pending_intents(self, limit: int = MAX_REQUESTS_PAGE) -> tuple[int, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_REQUESTS_PAGE
        ):
            raise ValueError(f"limit must be between 1 and {MAX_REQUESTS_PAGE}")
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM requests WHERE status IN ('requested', 'accepted', 'downloading') ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(int(row[0]) for row in rows)


class RequestWorkflow:
    """Persist-first request workflow for Radarr movies and Sonarr series."""

    def __init__(
        self,
        *,
        store: RequestStoreProtocol,
        radarr: MovieProvider | None = None,
        sonarr: SeriesProvider | None = None,
        plex: PlexVisibilityProvider | None = None,
        require_candidate_context: bool = True,
    ) -> None:
        self.store = store
        self.radarr = radarr
        self.sonarr = sonarr
        self.plex = plex
        self.require_candidate_context = require_candidate_context

    def _candidate_for_request(
        self,
        *,
        actor: RequestActor | None,
        candidate_handle: str | None,
        media_type: MediaType,
        provider_id: int | None,
        title: str,
        query_hash_value: str | None,
        year: int | None,
    ) -> tuple[str | None, str | None, str, int | None, int]:
        if actor is None:
            if self.require_candidate_context:
                raise ConflictError("trusted actor context is required")
            if provider_id is None:
                raise ConflictError("a candidate handle is required")
            return candidate_handle, query_hash_value, title, year, provider_id
        if self.require_candidate_context and actor.update_id is None:
            raise ConflictError("trusted actor update context is required")
        if candidate_handle is None:
            if self.require_candidate_context:
                raise ConflictError("a fresh search candidate handle is required")
            if provider_id is None:
                raise ConflictError("a candidate handle is required")
            return None, query_hash_value, title, year, provider_id
        resolver = getattr(self.store, "resolve_candidate", None)
        if not callable(resolver):
            raise DependencyError("candidate handle store is unavailable")
        candidate = cast(CandidateHandle, resolver(candidate_handle))
        if (
            candidate.actor.user_id != actor.user_id
            or candidate.actor.chat_id != actor.chat_id
        ):
            raise ConflictError("candidate handle belongs to another actor")
        if candidate.actor.update_id != actor.update_id:
            raise ConflictError("candidate handle belongs to another update")
        if (
            candidate.actor.chat_type != actor.chat_type
            or candidate.actor.update_type != actor.update_type
        ):
            raise ConflictError("candidate handle provenance does not match")
        if candidate.actor.allowlist_fingerprint != actor.allowlist_fingerprint:
            raise ConflictError("candidate handle policy fingerprint does not match")
        if candidate.media_type is not media_type:
            raise ConflictError(
                "candidate handle does not match the requested identity"
            )
        if provider_id is not None and candidate.provider_id != provider_id:
            raise ConflictError(
                "candidate handle does not match the requested identity"
            )
        if query_hash_value is not None and candidate.query_hash != query_hash_value:
            raise ConflictError("candidate query binding does not match")
        if title and candidate.title.casefold() != title.casefold():
            raise ConflictError("candidate title binding does not match")
        if year is not None and candidate.year != year:
            raise ConflictError("candidate year binding does not match")
        return (
            candidate_handle,
            candidate.query_hash,
            candidate.title,
            candidate.year if year is None else year,
            candidate.provider_id,
        )

    def issue_candidate(
        self,
        *,
        actor: RequestActor,
        media_type: MediaType,
        provider_id: int,
        title: str,
        query: str,
        year: int | None = None,
    ) -> str:
        if self.require_candidate_context and actor.update_id is None:
            raise ConflictError("trusted actor update context is required")
        creator = getattr(self.store, "create_candidate", None)
        if not callable(creator):
            raise DependencyError("candidate handle store is unavailable")
        return str(
            creator(
                CandidateInput(
                    actor, media_type, provider_id, title, query_hash(query), year
                )
            )
        )

    @staticmethod
    def _intent_baseline_available(intent: RequestIntent) -> bool:
        return bool(
            intent.plex_baseline and intent.plex_baseline.get("available") is True
        )

    def _plex_baseline(
        self, media_type: MediaType, provider_id: int
    ) -> Mapping[str, object] | None:
        if self.plex is None:
            return None
        try:
            identity = MediaIdentity(
                media_type,
                tmdb_id=provider_id if media_type is MediaType.MOVIE else None,
                tvdb_id=provider_id if media_type is MediaType.SERIES else None,
            )
            status = self.plex.status_for_identity(identity)
            available = _object_field(status, "available", "exists")
            if not isinstance(available, bool):
                raise DependencyError("Plex visibility response is invalid")
            if available is True:
                return {
                    "available": True,
                    "title": _object_field(status, "title", "name"),
                    "year": _object_field(status, "year"),
                    "plex_url": _object_field(status, "plex_url", "plexUrl"),
                    "observed_at": utc_timestamp(),
                }
        except DependencyError:
            raise
        except Exception as exc:
            # A configured Plex read failure must not be downgraded to
            # "unavailable": Plex is the availability authority, and an Arr
            # mutation during an unverified scope/read would create an
            # untracked request or a false historical baseline.
            raise DependencyError("Plex visibility check is unavailable") from exc
        return {"available": False, "observed_at": utc_timestamp()}

    def request_movie(
        self,
        tmdb_id: int | None = None,
        title: str | None = None,
        *,
        actor: RequestActor | Mapping[str, object] | object | None = None,
        idempotency_key: str | None = None,
        requested_by_user_id: int | None = None,
        requested_by_chat_id: int | None = None,
        requested_by_username: str | None = None,
        candidate_handle: str | None = None,
        query: str | None = None,
        year: int | None = None,
        update_id: int | None = None,
        chat_type: str = "private",
        update_type: str = "message",
        allowlist_fingerprint: str | None = None,
    ) -> RequestWorkflowResult:
        if self.radarr is None:
            raise DependencyError("Radarr is not configured")
        requested_provider_id = (
            _positive_int(tmdb_id, "tmdb_id") if tmdb_id is not None else None
        )
        trusted_actor = _actor_input(
            actor,
            user_id=requested_by_user_id,
            chat_id=requested_by_chat_id,
            username=requested_by_username,
            update_id=update_id,
            chat_type=chat_type,
            update_type=update_type,
            allowlist_fingerprint=allowlist_fingerprint,
        )
        requested_title = (
            title.strip() if isinstance(title, str) and title.strip() else ""
        )
        (
            candidate_handle,
            candidate_query_hash,
            requested_title,
            requested_year,
            tmdb_id,
        ) = self._candidate_for_request(
            actor=trusted_actor,
            candidate_handle=candidate_handle,
            media_type=MediaType.MOVIE,
            provider_id=requested_provider_id,
            title=requested_title,
            query_hash_value=query_hash(query) if query is not None else None,
            year=year,
        )
        intent_input = IntentInput(
            MediaType.MOVIE,
            tmdb_id,
            requested_title,
            year=requested_year,
            actor=trusted_actor,
            idempotency_key=idempotency_key,
            candidate_handle=candidate_handle,
            query_hash=candidate_query_hash,
            plex_baseline=self._plex_baseline(MediaType.MOVIE, tmdb_id),
        )
        intent, created = self.store.create_or_get_intent(intent_input)
        if self._intent_baseline_available(intent):
            if intent.status is RequestStatus.REQUESTED:
                setter = getattr(self.store, "set_intent_state")
                intent = setter(
                    intent.request_id,
                    RequestStatus.VISIBLE_IN_PLEX,
                    expected_version=intent.version,
                )
            subscription = self._ensure_subscription(intent)
            return self._result(
                intent,
                created,
                commands=self.store.list_commands(intent.request_id),
                subscriptions=(() if subscription is None else (subscription,)),
            )
        if not created and intent.status in {
            RequestStatus.ACCEPTED,
            RequestStatus.DOWNLOADING,
            RequestStatus.IMPORTED_TO_ARR,
            RequestStatus.VISIBLE_IN_PLEX,
            RequestStatus.DELIVERED,
        }:
            return self._result(
                intent,
                created,
                commands=self.store.list_commands(intent.request_id),
                subscriptions=self._subscription_ids(intent),
            )
        command = self.store.create_command(
            CommandInput(
                intent.request_id,
                "add_movie",
                "radarr",
                _hash_key("command", intent.request_id, "add_movie"),
                payload={
                    "tmdb_id": tmdb_id,
                    "title": intent.title,
                    "year": intent.year,
                },
            )
        )
        if command.status not in {"succeeded", "sent", "completed"}:
            self._execute_movie(command, intent)
        elif intent.status is RequestStatus.REQUESTED and command.external_id:
            # A prior worker may have committed the provider command but
            # crashed before advancing the intent row.  Reconcile from the
            # durable command result without issuing another provider call.
            intent = self.store.set_intent_state(
                intent.request_id,
                RequestStatus.ACCEPTED,
                provider_item_id=command.external_id,
                expected_version=intent.version,
            )
        intent = self.store.get_intent(intent.request_id)
        subscription = (
            self._ensure_subscription(intent)
            if intent.status is not RequestStatus.FAILED
            else None
        )
        commands = self.store.list_commands(intent.request_id)
        return self._result(
            intent,
            created,
            commands=commands,
            subscriptions=(() if subscription is None else (subscription,)),
        )

    request_movie_by_tmdb = request_movie

    def _execute_movie(self, command: RequestCommand, intent: RequestIntent) -> None:
        if self.radarr is None:
            return
        claim = self.store.claim_command(command.command_id)
        if claim is None:
            return
        mutation_started = False
        try:
            existing = self.radarr.find_existing_movie(intent.provider_id)
            operation = ProviderOperationInput(
                "radarr", "tmdb", intent.provider_id, intent.request_id
            )
            if existing is None:
                candidates = _lookup_movie(
                    self.radarr, intent.provider_id, intent.title
                )
                matching = [
                    candidate
                    for candidate in candidates
                    if _stable_provider_id(candidate, MediaType.MOVIE)
                    == intent.provider_id
                ]
                if not matching:
                    raise DependencyError(
                        "Radarr returned no metadata for the requested movie"
                    )
                claimer = getattr(self.store, "claim_provider_operation", None)
                if callable(claimer):
                    operation_claimed, known_id, _operation_status = claimer(operation)
                    if not operation_claimed:
                        if known_id is not None:
                            self.store.complete_command(
                                command.command_id, claim, external_id=known_id
                            )
                            self.store.set_intent_state(
                                intent.request_id,
                                RequestStatus.ACCEPTED,
                                provider_item_id=known_id,
                                expected_version=intent.version,
                            )
                        elif _operation_status == "unknown":
                            marker = getattr(self.store, "mark_command_unknown", None)
                            if callable(marker):
                                marker(
                                    command.command_id,
                                    claim,
                                    "provider add outcome is unknown",
                                )
                        return
                mutation_started = True
                existing = _add_movie(self.radarr, matching[0], intent.provider_id)
            if _stable_provider_id(existing, MediaType.MOVIE) != intent.provider_id:
                raise DependencyError(
                    "Radarr response did not match the requested TMDB identity"
                )
            provider_item_id = _provider_id(existing, "id", "provider_id")
            if provider_item_id is None:
                raise DependencyError("Radarr did not return a movie identifier")
            completer = getattr(self.store, "complete_provider_operation", None)
            if callable(completer):
                completer(operation.operation_key, external_id=str(provider_item_id))
            if not self.store.complete_command(
                command.command_id, claim, external_id=str(provider_item_id)
            ):
                return
            self.store.set_intent_state(
                intent.request_id,
                RequestStatus.ACCEPTED,
                provider_item_id=str(provider_item_id),
                expected_version=intent.version,
            )
        except Exception as exc:
            # An exception after the add call may mean Arr accepted the
            # mutation.  Fence the command as unknown and reconcile by stable
            # TMDB identity before any future retry.
            marker = getattr(self.store, "mark_command_unknown", None)
            if not mutation_started:
                self.store.fail_command(
                    command.command_id,
                    claim,
                    redact_text(str(exc), max_bytes=512),
                    retryable=True,
                )
            elif callable(marker):
                marker(command.command_id, claim, redact_text(str(exc), max_bytes=512))
            else:
                self.store.fail_command(
                    command.command_id,
                    claim,
                    redact_text(str(exc), max_bytes=512),
                    retryable=False,
                )
            operation = ProviderOperationInput(
                "radarr", "tmdb", intent.provider_id, intent.request_id
            )
            unknown = getattr(self.store, "mark_provider_operation_unknown", None)
            if mutation_started and callable(unknown):
                unknown(operation.operation_key)
            try:
                self.store.set_intent_state(
                    intent.request_id,
                    RequestStatus.REQUESTED,
                    error=redact_text(str(exc), max_bytes=512),
                    expected_version=intent.version,
                )
            except ConflictError:
                pass

    def request_series(
        self,
        tvdb_id: int | None = None,
        title: str | None = None,
        seasons: Sequence[int] = (),
        *,
        anime: bool = False,
        actor: RequestActor | Mapping[str, object] | object | None = None,
        idempotency_key: str | None = None,
        requested_by_user_id: int | None = None,
        requested_by_chat_id: int | None = None,
        requested_by_username: str | None = None,
        candidate_handle: str | None = None,
        query: str | None = None,
        year: int | None = None,
        mode: RequestMode | None = None,
        update_id: int | None = None,
        chat_type: str = "private",
        update_type: str = "message",
        allowlist_fingerprint: str | None = None,
    ) -> RequestWorkflowResult:
        if self.sonarr is None:
            raise DependencyError("Sonarr is not configured")
        requested_provider_id = (
            _positive_int(tvdb_id, "tvdb_id") if tvdb_id is not None else None
        )
        requested_seasons = _seasons(seasons, required=True)
        trusted_actor = _actor_input(
            actor,
            user_id=requested_by_user_id,
            chat_id=requested_by_chat_id,
            username=requested_by_username,
            update_id=update_id,
            chat_type=chat_type,
            update_type=update_type,
            allowlist_fingerprint=allowlist_fingerprint,
        )
        requested_title = (
            title.strip() if isinstance(title, str) and title.strip() else ""
        )
        (
            candidate_handle,
            candidate_query_hash,
            requested_title,
            requested_year,
            tvdb_id,
        ) = self._candidate_for_request(
            actor=trusted_actor,
            candidate_handle=candidate_handle,
            media_type=MediaType.SERIES,
            provider_id=requested_provider_id,
            title=requested_title,
            query_hash_value=query_hash(query) if query is not None else None,
            year=year,
        )
        intent_input = IntentInput(
            MediaType.SERIES,
            tvdb_id,
            requested_title,
            year=requested_year,
            seasons=requested_seasons,
            actor=trusted_actor,
            idempotency_key=idempotency_key,
            anime=anime,
            mode=mode,
            candidate_handle=candidate_handle,
            query_hash=candidate_query_hash,
            plex_baseline=self._plex_baseline(MediaType.SERIES, tvdb_id),
        )
        intent, created = self.store.create_or_get_intent(intent_input)
        if self._intent_baseline_available(intent):
            if intent.status is RequestStatus.REQUESTED:
                intent = self.store.set_intent_state(
                    intent.request_id,
                    RequestStatus.VISIBLE_IN_PLEX,
                    expected_version=intent.version,
                )
            subscriptions = tuple(
                subscription_id
                for season in requested_seasons
                if (
                    subscription_id := self._ensure_subscription(
                        intent, season_number=season
                    )
                )
                is not None
            )
            return self._result(
                intent,
                created,
                commands=self.store.list_commands(intent.request_id),
                subscriptions=subscriptions,
            )
        commands = list(self.store.list_commands(intent.request_id))
        if not created and intent.status in {
            RequestStatus.ACCEPTED,
            RequestStatus.DOWNLOADING,
            RequestStatus.IMPORTED_TO_ARR,
            RequestStatus.VISIBLE_IN_PLEX,
            RequestStatus.DELIVERED,
        }:
            return self._result(
                intent,
                created,
                commands=commands,
                subscriptions=self._subscription_ids(intent),
            )
        add_command = next(
            (
                item
                for item in commands
                if item.command_type in {"add_series", "update_series"}
            ),
            None,
        )
        if add_command is None:
            add_command = self.store.create_command(
                CommandInput(
                    intent.request_id,
                    "add_series",
                    "sonarr",
                    _hash_key("command", intent.request_id, "add_series"),
                    payload={
                        "tvdb_id": tvdb_id,
                        "title": intent.title,
                        "year": intent.year,
                        "seasons": list(requested_seasons),
                        "anime": anime,
                    },
                )
            )
        if add_command.status not in {"succeeded", "sent", "completed"}:
            self._execute_series_add(
                add_command, intent, requested_seasons, anime=anime
            )
        elif intent.status is RequestStatus.REQUESTED and add_command.external_id:
            intent = self.store.set_intent_state(
                intent.request_id,
                RequestStatus.ACCEPTED,
                provider_item_id=add_command.external_id,
                expected_version=intent.version,
            )
        intent = self.store.get_intent(intent.request_id)
        commands = list(self.store.list_commands(intent.request_id))
        provider_series_id = (
            int(intent.provider_item_id)
            if intent.provider_item_id
            and intent.provider_item_id.isdigit()
            and int(intent.provider_item_id) > 0
            else None
        )
        if intent.status is RequestStatus.ACCEPTED and provider_series_id is not None:
            for season in requested_seasons:
                enumeration = self._enumerate_series_season(
                    intent, provider_series_id, season
                )
                command = next(
                    (
                        item
                        for item in commands
                        if item.command_type == "season_search"
                        and item.season_number == season
                    ),
                    None,
                )
                if command is None:
                    command = self.store.create_command(
                        CommandInput(
                            intent.request_id,
                            "season_search",
                            "sonarr",
                            _hash_key(
                                "command", intent.request_id, "season_search", season
                            ),
                            provider_id=provider_series_id,
                            season_number=season,
                            payload={
                                "series_id": provider_series_id,
                                "season_number": season,
                            },
                        )
                    )
                if command.status not in {"succeeded", "sent", "completed"}:
                    self._execute_season_search(
                        command, intent, provider_series_id, season
                    )
                current_intent = self.store.get_intent(intent.request_id)
                subscription_id = self._ensure_subscription(
                    current_intent,
                    season_number=season,
                    # Missing/invalid enumeration is unresolved and must not
                    # inherit the request's default season-completion mode.
                    mode=enumeration.mode
                    if enumeration is not None
                    else RequestMode.AIRING_EPISODE,
                )
                if (
                    subscription_id is not None
                    and enumeration is not None
                    and enumeration.mode is RequestMode.AIRING_EPISODE
                ):
                    materialize = getattr(self.store, "ensure_episode_units", None)
                    if callable(materialize):
                        materialize(subscription_id, enumeration)
        intent = self.store.get_intent(intent.request_id)
        commands = list(self.store.list_commands(intent.request_id))
        subs = self._subscription_ids(intent)
        return self._result(intent, created, commands=commands, subscriptions=subs)

    request_series_by_tvdb = request_series

    def _enumerate_series_season(
        self, intent: RequestIntent, series_id: int, season: int
    ) -> EpisodeEnumeration | None:
        if self.sonarr is None:
            return None
        list_records = getattr(self.sonarr, "list_episode_records", None)
        if not callable(list_records):
            return None
        try:
            records = list_records(series_id, season_number=season)
            series_status: object = None
            season_status: object = None
            get_series = getattr(self.sonarr, "get_series", None)
            if callable(get_series):
                source = get_series(series_id)
                if isinstance(source, Mapping):
                    series_status = source.get("status")
                    raw_seasons = source.get("seasons")
                    if isinstance(raw_seasons, Sequence) and not isinstance(
                        raw_seasons, (str, bytes, bytearray)
                    ):
                        for raw_season in raw_seasons:
                            if (
                                isinstance(raw_season, Mapping)
                                and raw_season.get("seasonNumber") == season
                            ):
                                season_status = raw_season.get(
                                    "status", raw_season.get("seasonStatus")
                                )
                                break
                else:
                    series_status = getattr(source, "status", None)
                    for raw_season, raw_status in getattr(
                        source, "season_statuses", ()
                    ):
                        if raw_season == season:
                            season_status = raw_status
                            break
            latest_getter = getattr(self.store, "latest_enumeration", None)
            latest = (
                latest_getter("sonarr", intent.provider_id, season)
                if callable(latest_getter)
                else None
            )
            version = 1 if latest is None else latest.version + 1
            snapshot = enumerate_episodes(
                records,
                season,
                provider="sonarr",
                provider_id=intent.provider_id,
                requested_explicitly=True,
                explicit_seasons=intent.seasons,
                expected_count=len(records) if isinstance(records, Sequence) else None,
                authoritative=True,
                series_status=series_status,
                season_status=season_status,
                version=version,
                source="sonarr",
            )
            saver = getattr(self.store, "save_enumeration", None)
            if callable(saver):
                saver("sonarr", intent.provider_id, snapshot)
            recorder = getattr(self.store, "record_enumeration_version", None)
            recorded_intent = (
                recorder(intent.request_id, snapshot.version)
                if callable(recorder)
                else intent
            )
            setter = getattr(self.store, "set_intent_mode", None)
            if callable(setter) and snapshot.mode is not recorded_intent.mode:
                try:
                    setter(
                        intent.request_id,
                        snapshot.mode,
                        expected_version=recorded_intent.version,
                    )
                except ConflictError:
                    pass
            return snapshot
        except (EnumerationError, DependencyError, ValueError, TypeError):
            # Unknown/invalid enumeration cannot produce a completion unit;
            # retain the durable season command and let the resolver refresh.
            return None

    def _execute_series_add(
        self,
        command: RequestCommand,
        intent: RequestIntent,
        seasons: Sequence[int],
        *,
        anime: bool,
    ) -> None:
        if self.sonarr is None:
            return
        claim = self.store.claim_command(command.command_id)
        if claim is None:
            return
        mutation_started = False
        try:
            existing = self.sonarr.find_existing_series(intent.provider_id)
            operation = ProviderOperationInput(
                "sonarr", "tvdb", intent.provider_id, intent.request_id
            )
            if existing is None:
                candidates = _lookup_series(
                    self.sonarr, intent.provider_id, intent.title
                )
                matching = [
                    candidate
                    for candidate in candidates
                    if _stable_provider_id(candidate, MediaType.SERIES)
                    == intent.provider_id
                ]
                if not matching:
                    raise DependencyError(
                        "Sonarr returned no metadata for the requested series"
                    )
                claimer = getattr(self.store, "claim_provider_operation", None)
                if callable(claimer):
                    operation_claimed, known_id, _operation_status = claimer(operation)
                    if not operation_claimed:
                        if known_id is not None:
                            self.store.complete_command(
                                command.command_id, claim, external_id=known_id
                            )
                            self.store.set_intent_state(
                                intent.request_id,
                                RequestStatus.ACCEPTED,
                                provider_item_id=known_id,
                                expected_version=intent.version,
                            )
                        elif _operation_status == "unknown":
                            marker = getattr(self.store, "mark_command_unknown", None)
                            if callable(marker):
                                marker(
                                    command.command_id,
                                    claim,
                                    "provider add outcome is unknown",
                                )
                        return
                mutation_started = True
                existing = _add_series(
                    self.sonarr, matching[0], intent.provider_id, seasons, anime
                )
            else:
                claimer = getattr(self.store, "claim_provider_operation", None)
                if callable(claimer):
                    operation_claimed, known_id, _operation_status = claimer(operation)
                    if not operation_claimed:
                        if known_id is not None:
                            self.store.complete_command(
                                command.command_id, claim, external_id=known_id
                            )
                            self.store.set_intent_state(
                                intent.request_id,
                                RequestStatus.ACCEPTED,
                                provider_item_id=known_id,
                                expected_version=intent.version,
                            )
                        elif _operation_status == "unknown":
                            marker = getattr(self.store, "mark_command_unknown", None)
                            if callable(marker):
                                marker(
                                    command.command_id,
                                    claim,
                                    "provider series outcome is unknown",
                                )
                        return
                update = getattr(self.sonarr, "update_series", None)
                if callable(update):
                    mutation_started = True
                    updated = update(existing, seasons=seasons, preserve_existing=True)
                    # A narrow integration fake may treat a successful PUT as
                    # a side effect and return ``None``; retain the known
                    # provider identity for durable command completion.
                    if updated is not None:
                        existing = updated
            if _stable_provider_id(existing, MediaType.SERIES) != intent.provider_id:
                raise DependencyError(
                    "Sonarr response did not match the requested TVDB identity"
                )
            provider_item_id = _provider_id(existing, "id", "provider_id")
            if provider_item_id is None:
                raise DependencyError("Sonarr did not return a series identifier")
            completer = getattr(self.store, "complete_provider_operation", None)
            if callable(completer):
                completer(
                    ProviderOperationInput(
                        "sonarr", "tvdb", intent.provider_id, intent.request_id
                    ).operation_key,
                    external_id=str(provider_item_id),
                )
            if not self.store.complete_command(
                command.command_id, claim, external_id=str(provider_item_id)
            ):
                return
            self.store.set_intent_state(
                intent.request_id,
                RequestStatus.ACCEPTED,
                provider_item_id=str(provider_item_id),
                expected_version=intent.version,
            )
        except Exception as exc:
            message = redact_text(str(exc), max_bytes=512)
            marker = getattr(self.store, "mark_command_unknown", None)
            if mutation_started:
                if callable(marker):
                    marker(command.command_id, claim, message)
                else:
                    self.store.fail_command(
                        command.command_id, claim, message, retryable=False
                    )
                unknown = getattr(self.store, "mark_provider_operation_unknown", None)
                if callable(unknown):
                    unknown(
                        ProviderOperationInput(
                            "sonarr", "tvdb", intent.provider_id, intent.request_id
                        ).operation_key
                    )
            else:
                self.store.fail_command(
                    command.command_id, claim, message, retryable=True
                )
            try:
                self.store.set_intent_state(
                    intent.request_id,
                    RequestStatus.REQUESTED,
                    error=message,
                    expected_version=intent.version,
                )
            except ConflictError:
                pass

    def _execute_season_search(
        self,
        command: RequestCommand,
        intent: RequestIntent,
        series_id: int,
        season: int,
    ) -> None:
        if self.sonarr is None:
            return
        claim = self.store.claim_command(command.command_id)
        if claim is None:
            return
        try:
            operation = ProviderOperationInput(
                "sonarr", "tvdb", intent.provider_id, intent.request_id, season
            )
            claimer = getattr(self.store, "claim_provider_operation", None)
            if callable(claimer):
                operation_claimed, known_id, operation_status = claimer(operation)
                if not operation_claimed:
                    if operation_status == "succeeded":
                        self.store.complete_command(
                            command.command_id, claim, external_id=known_id
                        )
                    elif operation_status == "unknown":
                        marker = getattr(self.store, "mark_command_unknown", None)
                        if callable(marker):
                            marker(
                                command.command_id,
                                claim,
                                "provider season search outcome is unknown",
                            )
                    return
            self.sonarr.search_season(series_id, season)
            completer = getattr(self.store, "complete_provider_operation", None)
            if callable(completer):
                completer(operation.operation_key, external_id="submitted")
            self.store.complete_command(command.command_id, claim)
        except Exception as exc:
            message = redact_text(str(exc), max_bytes=512)
            marker = getattr(self.store, "mark_command_unknown", None)
            if callable(marker):
                marker(command.command_id, claim, message)
            else:
                self.store.fail_command(
                    command.command_id, claim, message, retryable=False
                )
            unknown = getattr(self.store, "mark_provider_operation_unknown", None)
            if callable(unknown):
                unknown(
                    ProviderOperationInput(
                        "sonarr", "tvdb", intent.provider_id, intent.request_id, season
                    ).operation_key
                )

    def get_request(
        self,
        request_id: int,
        *,
        actor: RequestActor | Mapping[str, object] | object | None = None,
        authorized: bool = False,
    ) -> RequestIntent:
        if not isinstance(authorized, bool):
            raise ValueError("authorized must be a boolean")
        intent = self.store.get_intent(request_id)
        if authorized:
            return intent
        trusted = _actor_input(actor)
        if trusted is None or intent.actor is None:
            raise ConflictError("request status requires an authorized actor")
        if (
            trusted.user_id != intent.actor.user_id
            or trusted.chat_id != intent.actor.chat_id
        ):
            raise ConflictError("request status is bound to another actor")
        return intent

    request_status = get_request

    def reconcile_pending(
        self, *, limit: int = MAX_REQUESTS_PAGE
    ) -> tuple[RequestWorkflowResult, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_REQUESTS_PAGE
        ):
            raise ValueError(f"limit must be between 1 and {MAX_REQUESTS_PAGE}")
        # A generic repository can provide a richer command scan; SQLite store
        # intentionally keeps this helper narrow until the worker owns claims.
        pending = getattr(self.store, "pending_intents", None)
        if not callable(pending):
            return ()
        results: list[RequestWorkflowResult] = []
        for request_id in pending(limit):
            intent = self.store.get_intent(request_id)
            self._reconcile_unknown_commands(intent)
            intent = self.store.get_intent(request_id)
            results.append(
                self._result(
                    intent,
                    False,
                    commands=self.store.list_commands(request_id),
                    subscriptions=self._subscription_ids(intent),
                )
            )
        return tuple(results)

    def _reconcile_unknown_commands(self, intent: RequestIntent) -> None:
        commands = self.store.list_commands(intent.request_id)
        resolver = getattr(self.store, "resolve_unknown_command", None)
        if not callable(resolver):
            return
        for command in commands:
            if command.status != "unknown":
                continue
            existing: object | None = None
            provider_item_id: int | None = None
            operation: ProviderOperationInput | None = None
            if (
                command.command_type in {"add_movie", "update_movie"}
                and self.radarr is not None
            ):
                existing = self.radarr.find_existing_movie(intent.provider_id)
                if (
                    existing is None
                    or _stable_provider_id(existing, MediaType.MOVIE)
                    != intent.provider_id
                ):
                    continue
                provider_item_id = _provider_id(existing, "id", "provider_id")
                operation = ProviderOperationInput(
                    "radarr", "tmdb", intent.provider_id, intent.request_id
                )
            elif (
                command.command_type in {"add_series", "update_series"}
                and self.sonarr is not None
            ):
                existing = self.sonarr.find_existing_series(intent.provider_id)
                if (
                    existing is None
                    or _stable_provider_id(existing, MediaType.SERIES)
                    != intent.provider_id
                ):
                    continue
                provider_item_id = _provider_id(existing, "id", "provider_id")
                operation = ProviderOperationInput(
                    "sonarr", "tvdb", intent.provider_id, intent.request_id
                )
            else:
                # A SeasonSearch response has no stable provider-side object to
                # inspect.  Leave it unknown rather than issuing a duplicate.
                continue
            if provider_item_id is None or operation is None:
                continue
            if resolver(command.command_id, external_id=str(provider_item_id)):
                complete = getattr(self.store, "complete_provider_operation", None)
                if callable(complete):
                    complete(operation.operation_key, external_id=str(provider_item_id))
                try:
                    self.store.set_intent_state(
                        intent.request_id,
                        RequestStatus.ACCEPTED,
                        provider_item_id=str(provider_item_id),
                        expected_version=intent.version,
                    )
                except ConflictError:
                    pass

    def _subscription_ids(self, intent: RequestIntent) -> tuple[int, ...]:
        finder_for_intent = getattr(self.store, "subscription_ids_for_intent", None)
        if callable(finder_for_intent):
            return tuple(finder_for_intent(intent))
        finder = getattr(self.store, "subscription_ids", None)
        if callable(finder):
            return tuple(finder(intent.request_id))
        return ()

    def _ensure_subscription(
        self,
        intent: RequestIntent,
        *,
        season_number: int | None = None,
        mode: RequestMode | None = None,
    ) -> int | None:
        result = _call_supported(
            self.store.ensure_subscription,
            intent,
            season_number=season_number,
            mode=mode,
        )
        return cast(int | None, result)

    def _result(
        self,
        intent: RequestIntent,
        created: bool,
        *,
        commands: Sequence[RequestCommand] = (),
        subscriptions: Sequence[int] = (),
    ) -> RequestWorkflowResult:
        return RequestWorkflowResult(
            intent, created, tuple(commands), tuple(subscriptions), intent.error
        )


def _intent_from_row(row: sqlite3.Row | Sequence[object]) -> RequestIntent:
    def get(name: str, index: int) -> object:
        if isinstance(row, sqlite3.Row):
            try:
                return row[name]
            except (IndexError, KeyError):
                return row[index]
        return row[index]

    request_id = _positive_int(get("id", 0), "request_id")
    media_type = MediaType(str(get("media_type", 6)))
    provider_id = _positive_int(int(str(get("provider_id", 7))), "provider_id")
    raw_seasons = get("seasons_json", 15)
    try:
        parsed_seasons = (
            json.loads(raw_seasons)
            if isinstance(raw_seasons, str) and raw_seasons
            else []
        )
    except json.JSONDecodeError:
        parsed_seasons = []
    seasons = _seasons(
        parsed_seasons
        if isinstance(parsed_seasons, Sequence)
        and not isinstance(parsed_seasons, (str, bytes, bytearray))
        else (),
        required=media_type is MediaType.SERIES,
    )
    raw_payload = get("payload_json", 21)
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else {}
    except json.JSONDecodeError:
        payload = {}
    user_id = get("requested_by_user_id", 2)
    chat_id = get("requested_by_chat_id", 3)
    username = get("requested_by_username", 4)
    update_raw = get("actor_update_id", 5)
    actor_payload = (
        payload.get("actor_context") if isinstance(payload, Mapping) else None
    )
    actor_chat_type = (
        actor_payload.get("chat_type", "private")
        if isinstance(actor_payload, Mapping)
        else "private"
    )
    actor_update_type = (
        actor_payload.get("update_type", "message")
        if isinstance(actor_payload, Mapping)
        else "message"
    )
    actor_fingerprint = (
        actor_payload.get("allowlist_fingerprint")
        if isinstance(actor_payload, Mapping)
        else None
    )
    actor = (
        RequestActor(
            int(user_id),
            int(chat_id),
            username if isinstance(username, str) else None,
            int(update_raw)
            if isinstance(update_raw, int) and not isinstance(update_raw, bool)
            else None,
            actor_chat_type if isinstance(actor_chat_type, str) else "private",
            actor_update_type if isinstance(actor_update_type, str) else "message",
            actor_fingerprint if isinstance(actor_fingerprint, str) else None,
        )
        if isinstance(user_id, int)
        and not isinstance(user_id, bool)
        and isinstance(chat_id, int)
        and not isinstance(chat_id, bool)
        and user_id != 0
        and chat_id != 0
        else None
    )
    created = _parse_timestamp(get("created_at", 24))
    enum_versions_raw = (
        payload.get("enumeration_versions", []) if isinstance(payload, Mapping) else []
    )
    enum_versions = (
        tuple(
            int(item)
            for item in enum_versions_raw
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        )
        if isinstance(enum_versions_raw, Sequence)
        and not isinstance(enum_versions_raw, (str, bytes, bytearray))
        else ()
    )
    baseline = (
        payload.get("plex_baseline")
        if isinstance(payload, Mapping)
        and isinstance(payload.get("plex_baseline"), Mapping)
        else None
    )
    if baseline is None:
        raw_baseline = get("plex_baseline_json", 22)
        try:
            parsed_baseline = (
                json.loads(raw_baseline)
                if isinstance(raw_baseline, str) and raw_baseline
                else None
            )
        except json.JSONDecodeError:
            parsed_baseline = None
        baseline = parsed_baseline if isinstance(parsed_baseline, Mapping) else None
    raw_version = get("version", 23)
    version = (
        int(raw_version)
        if isinstance(raw_version, int)
        and not isinstance(raw_version, bool)
        and raw_version >= 0
        else 0
    )
    return RequestIntent(
        request_id,
        str(get("request_key", 1)),
        str(get("idempotency_key", 20)),
        media_type,
        provider_id,
        _safe_text(
            get("title", 13),
            fallback=media_type.value.title(),
            max_bytes=MAX_TITLE_BYTES,
        ),
        _year(get("year", 14)) if isinstance(get("year", 14), int) else None,
        seasons,
        actor,
        RequestMode(str(get("mode", 16))),
        RequestStatus(str(get("status", 17))),
        str(get("provider_item_id", 18))
        if get("provider_item_id", 18) is not None
        else None,
        created,
        _payload_error(get("payload_json", 21)),
        version,
        enum_versions,
        cast(Mapping[str, object], baseline) if baseline is not None else None,
    )


def _command_from_row(row: sqlite3.Row | Sequence[object]) -> RequestCommand:
    def get(name: str, index: int) -> object:
        if isinstance(row, sqlite3.Row):
            try:
                return row[name]
            except (IndexError, KeyError):
                return row[index]
        return row[index]

    raw_payload = get("payload_json", 7)
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else {}
    except json.JSONDecodeError:
        payload = {}
    provider_raw = get("provider_id", 5)
    season_raw = get("season_number", 6)
    provider_value = (
        int(provider_raw)
        if isinstance(provider_raw, (int, str))
        and not isinstance(provider_raw, bool)
        and str(provider_raw).isdigit()
        and int(provider_raw) > 0
        else None
    )
    season_value = (
        int(season_raw)
        if isinstance(season_raw, (int, str))
        and not isinstance(season_raw, bool)
        and str(season_raw).isdigit()
        and int(season_raw) >= 0
        else None
    )
    raw_attempts = get("attempts", 9)
    attempts = (
        int(raw_attempts)
        if isinstance(raw_attempts, (int, str)) and str(raw_attempts).isdigit()
        else 0
    )
    return RequestCommand(
        _positive_int(get("id", 0), "command_id"),
        _positive_int(get("request_id", 1), "request_id"),
        str(get("command_type", 2)),
        str(get("service", 3)),
        str(get("idempotency_key", 4)),
        provider_value,
        season_value,
        cast(Mapping[str, object], payload) if isinstance(payload, Mapping) else {},
        str(get("status", 8)),
        attempts,
        str(get("external_id", 13)) if get("external_id", 13) is not None else None,
        redact_text(str(get("last_error", 14)), max_bytes=512)
        if get("last_error", 14) is not None
        else None,
    )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _payload_error(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    raw = payload.get("error") if isinstance(payload, Mapping) else None
    return redact_text(raw, max_bytes=512) if isinstance(raw, str) else None


# Compatibility aliases used by application wiring and focused tests.
RequestRepository = SQLiteRequestStore
RequestIntentStore = SQLiteRequestStore
MediaRequestWorkflow = RequestWorkflow
RequestService = RequestWorkflow


__all__ = [
    "CandidateHandle",
    "CandidateInput",
    "CommandInput",
    "IntentInput",
    "MediaRequestWorkflow",
    "MovieProvider",
    "PlexVisibilityProvider",
    "ProviderOperationInput",
    "RequestActor",
    "RequestCommand",
    "RequestIntent",
    "RequestIntentStore",
    "RequestRepository",
    "RequestResult",
    "RequestService",
    "RequestStoreProtocol",
    "RequestWorkflow",
    "RequestWorkflowResult",
    "SQLiteRequestStore",
    "SeriesProvider",
    "query_hash",
]


RequestResult = RequestWorkflowResult
