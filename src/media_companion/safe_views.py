"""Normalization, redaction, pagination, and response bounds for shared tools.

Only this module's typed records cross the shared-user boundary.  Provider
responses are accepted at a narrow adapter seam and are immediately converted
to allow-listed fields.  Unknown mapping keys are never copied, so a future
provider field cannot accidentally expose an API key, path, URL, or provider
object.

The pagination helpers keep at most 5,000 normalized records in a five-minute
server-side snapshot.  Every page is serialized before it is returned and is
bounded to 256 KiB, including partial errors and cursor metadata.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Final, Generic, Self, TypeVar, cast
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from .cursors import (
    MAX_SNAPSHOT_PARTIAL_ERRORS,
    MAX_SNAPSHOT_ITEMS,
    CursorBindingError,
    CursorError,
    CursorSigner,
    SnapshotRecord,
    SnapshotStore,
    binding_hash,
)
from .errors import ModelValidationError
from .models import (
    MediaCandidate,
    MediaIdentity,
    MediaStatus,
    MediaType,
    Page,
    PartialError,
    PlexItem,
    QueueItem,
    QueueState,
    ServiceName,
    canonical_rating_key,
)

MAX_RESPONSE_BYTES: Final[int] = 256 * 1024
MAX_SERIALIZED_RESPONSE_BYTES: Final[int] = MAX_RESPONSE_BYTES
RESPONSE_SIZE_LIMIT_BYTES: Final[int] = MAX_RESPONSE_BYTES
MAX_PAGE_RESPONSE_BYTES: Final[int] = MAX_RESPONSE_BYTES

DEFAULT_SEARCH_PAGE_SIZE: Final[int] = 25
MAX_SEARCH_PAGE_SIZE: Final[int] = 100
DEFAULT_STATUS_PAGE_SIZE: Final[int] = 100
MAX_STATUS_PAGE_SIZE: Final[int] = 250
MAX_PARTIAL_ERRORS: Final[int] = MAX_SNAPSHOT_PARTIAL_ERRORS
MAX_SNAPSHOT_BYTES: Final[int] = 8 * 1024 * 1024
DEFAULT_PAGE_SIZE: Final[int] = DEFAULT_SEARCH_PAGE_SIZE
MAX_PAGE_SIZE: Final[int] = MAX_STATUS_PAGE_SIZE

# Tool names are intentionally repeated here rather than derived from dynamic
# discovery.  A new shared tool must receive an explicit reviewed bound.
PAGE_BOUNDS: Final[Mapping[str, tuple[int, int]]] = MappingProxyType(
    {
        "search_media": (DEFAULT_SEARCH_PAGE_SIZE, MAX_SEARCH_PAGE_SIZE),
        "browse_library": (DEFAULT_SEARCH_PAGE_SIZE, MAX_SEARCH_PAGE_SIZE),
        "request_status": (DEFAULT_STATUS_PAGE_SIZE, MAX_STATUS_PAGE_SIZE),
        "download_status": (DEFAULT_STATUS_PAGE_SIZE, MAX_STATUS_PAGE_SIZE),
        "media_status": (DEFAULT_STATUS_PAGE_SIZE, MAX_STATUS_PAGE_SIZE),
    }
)
PAGE_SIZE_BOUNDS = PAGE_BOUNDS
DEFAULT_PAGE_SIZES: Final[Mapping[str, int]] = MappingProxyType(
    {name: bounds[0] for name, bounds in PAGE_BOUNDS.items()}
)
MAX_PAGE_SIZES: Final[Mapping[str, int]] = MappingProxyType(
    {name: bounds[1] for name, bounds in PAGE_BOUNDS.items()}
)

_UTC = timezone.utc
_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:\b(?:https?|file|ftp)://|//[a-z0-9])"
)
_URI_SCHEME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:^|[\s\"'])(?:[a-z][a-z0-9+.-]*):(?=[^\s])"
)
_UNC_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:^|[\s\"'])(?:\\\\|//)[^\\/\s\"']+(?:[\\/][^\s\"']+)+"
)
_CREDENTIAL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{8,}|basic\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret)\s*[:=]|"
    r"x-api-key\s*[:=]|authorization\s*[:=]|(?:token|capability)\s*[:=])"
)
_ABSOLUTE_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:^|[\s\"'])(?:/(?:[a-z0-9._-]+(?:/[^\s]*)?)?|"
    r"[a-z]:[\\/](?:[^\s\\/]+[\\/])*[^\s]*)"
)
_CONTROL_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")
_SENSITIVE_QUERY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "token",
        "access_token",
        "auth",
        "authorization",
        "api_key",
        "apikey",
        "password",
        "secret",
        "capability",
    }
)
_KNOWN_PLEX_HOSTS: Final[frozenset[str]] = frozenset(
    {"app.plex.tv", "plex.tv", "www.plex.tv"}
)


class SafeViewError(ValueError):
    """The input cannot be represented by a safe typed view."""


class PageSizeError(SafeViewError):
    """A page limit was absent, invalid, or above the reviewed maximum."""


class ResponseTooLargeError(SafeViewError):
    """A single normalized item cannot fit the serialized response ceiling."""


class UnsafeProviderDataError(SafeViewError):
    """Provider data contained an explicitly forbidden field/value."""


class InvalidPlexLinkError(SafeViewError):
    """A supplied or constructed Plex URL is not a validated Plex link."""


class _AuthenticatedCursor(str):
    """Cursor marker produced only after signer/context verification."""


def _authenticated_cursor(value: str) -> _AuthenticatedCursor:
    return _AuthenticatedCursor(value)


ValidationError = SafeViewError
UnsafeView = UnsafeProviderDataError
OversizedResponseError = ResponseTooLargeError


class _ValidatedPlexLink(str):
    """String link carrying the trusted origin used during validation.

    The origin metadata is process-local and is never serialized.  It lets a
    custom configured Plex host (for example ``http://plex:32400``) survive
    the final response-bound check without treating arbitrary provider URLs as
    safe merely because they happen to contain a ``/web`` path.
    """

    allowed_origins: tuple[str, ...]

    def __new__(cls, value: str, allowed_origins: Iterable[str] = ()) -> Self:
        instance = str.__new__(cls, value)
        instance.allowed_origins = tuple(allowed_origins)
        return instance


def _validated_link(
    value: object,
    *,
    allowed_origins: Iterable[str] | None = None,
    rating_key: str | None = None,
) -> str:
    origins = tuple(allowed_origins or ())
    canonical = validate_plex_link(
        value, allowed_origins=origins or None, rating_key=rating_key
    )
    return _ValidatedPlexLink(canonical, origins)


def _trusted_link(
    value: object,
    *,
    rating_key: str | None = None,
) -> str:
    if not isinstance(value, _ValidatedPlexLink):
        raise UnsafeProviderDataError(
            "provider-supplied Plex links cannot cross the safe boundary"
        )
    return _validated_link(
        value,
        allowed_origins=value.allowed_origins,
        rating_key=rating_key,
    )


def _validate_cursor_shape(value: object) -> str:
    if not isinstance(value, _AuthenticatedCursor) or not value:
        raise SafeViewError(
            "next_cursor must be generated by the authenticated paginator"
        )
    try:
        CursorSigner.decode(value)
    except CursorError as exc:
        raise SafeViewError("next_cursor is not a signed cursor") from exc
    return value


def page_bounds(tool: str) -> tuple[int, int]:
    """Return ``(default, maximum)`` for one reviewed shared tool."""

    if not isinstance(tool, str):
        raise PageSizeError("tool must be text")
    try:
        return PAGE_BOUNDS[tool]
    except KeyError as exc:
        raise PageSizeError("unknown paginated shared tool") from exc


def normalize_page_size(tool: str, limit: int | None = None) -> int:
    default, maximum = page_bounds(tool)
    if limit is None:
        return default
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise PageSizeError("limit must be a positive integer")
    if limit > maximum:
        raise PageSizeError(f"limit cannot exceed {maximum}")
    return limit


validate_page_size = normalize_page_size
bounded_page_size = normalize_page_size


def _text(value: object, field_name: str, *, max_bytes: int = 512) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    result = value.strip()
    if not result or len(result.encode("utf-8", "ignore")) > max_bytes:
        return None
    if (
        _CONTROL_RE.search(result)
        or _URL_RE.search(result)
        or _URI_SCHEME_RE.search(result)
        or _UNC_PATH_RE.search(result)
        or _CREDENTIAL_RE.search(result)
    ):
        return None
    if _ABSOLUTE_PATH_RE.search(result):
        return None
    return result


def _required_text(value: object, field_name: str, *, max_bytes: int = 512) -> str:
    result = _text(value, field_name, max_bytes=max_bytes)
    if result is None:
        raise UnsafeProviderDataError(f"{field_name} is not safe text")
    return result


def _int(
    value: object, *, minimum: int | None = None, maximum: int | None = None
) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _float(
    value: object, *, minimum: float | None = None, maximum: float | None = None
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    if minimum is not None and result < minimum:
        return None
    if maximum is not None and result > maximum:
        return None
    return result


def _value(source: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in source:
            return source[name]
    return None


def _media_type(value: object, *, default: MediaType | None = None) -> MediaType:
    result: MediaType | None
    if isinstance(value, MediaType):
        result = value
    elif isinstance(value, str):
        normalized = value.strip().lower()
        aliases = {
            "movie": MediaType.MOVIE,
            "film": MediaType.MOVIE,
            "series": MediaType.SERIES,
            "series_item": MediaType.SERIES,
            "show": MediaType.SERIES,
            "tv": MediaType.SERIES,
            "episode": MediaType.EPISODE,
        }
        result = aliases.get(normalized, default)
    else:
        result = default
    if result is None:
        raise UnsafeProviderDataError("media_type is missing or unsupported")
    return result


def _provider_id(source: Mapping[str, Any], media_type: MediaType) -> str:
    raw = _value(
        source,
        "provider_id",
        "providerId",
        "tmdb_id",
        "tmdbId",
        "tvdb_id",
        "tvdbId",
        "imdb_id",
        "imdbId",
        "id",
    )
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise UnsafeProviderDataError("provider identity is missing")
    if isinstance(raw, int):
        if raw <= 0:
            raise UnsafeProviderDataError("provider identity is invalid")
        return str(raw)
    result = raw.strip()
    if (
        not result
        or len(result) > 128
        or _URL_RE.search(result)
        or _URI_SCHEME_RE.search(result)
        or _UNC_PATH_RE.search(result)
        or _CREDENTIAL_RE.search(result)
        or _ABSOLUTE_PATH_RE.search(result)
    ):
        raise UnsafeProviderDataError("provider identity is unsafe")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", result):
        raise UnsafeProviderDataError("provider identity is not canonical")
    return result


def _identity(
    source: Mapping[str, Any], media_type: MediaType, provider_id: str
) -> MediaIdentity:
    tmdb = _int(_value(source, "tmdb_id", "tmdbId"), minimum=1)
    tvdb = _int(_value(source, "tvdb_id", "tvdbId"), minimum=1)
    imdb_raw = _value(source, "imdb_id", "imdbId")
    imdb = imdb_raw.strip() if isinstance(imdb_raw, str) else None
    if imdb is not None and not re.fullmatch(r"tt[0-9]+", imdb, re.IGNORECASE):
        imdb = None
    try:
        return MediaIdentity(
            media_type=media_type,
            tmdb_id=tmdb,
            tvdb_id=tvdb,
            imdb_id=imdb,
            provider_id=provider_id,
        )
    except ModelValidationError as exc:
        raise UnsafeProviderDataError("provider identity is invalid") from exc


def _year(source: Mapping[str, Any]) -> int | None:
    value = _value(source, "year", "release_year", "releaseYear")
    result = _int(value, minimum=1800, maximum=3000)
    if result is not None:
        return result
    for name in ("release_date", "releaseDate", "first_air_date", "firstAirDate"):
        date_value = source.get(name)
        if isinstance(date_value, str):
            match = re.match(r"^([0-9]{4})(?:-|$)", date_value.strip())
            if match:
                parsed = int(match.group(1))
                if 1800 <= parsed <= 3000:
                    return parsed
    return None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(_UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = _float(value, minimum=0)
        if parsed is None:
            return None
        try:
            return datetime.fromtimestamp(parsed, tz=_UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed_datetime = datetime.fromisoformat(text)
        except ValueError:
            return None
        return (
            None if parsed_datetime.tzinfo is None else parsed_datetime.astimezone(_UTC)
        )
    return None


def _service(value: object) -> ServiceName:
    if isinstance(value, ServiceName):
        result = value
    elif isinstance(value, str):
        normalized = value.strip().lower()
        try:
            result = ServiceName(normalized)
        except ValueError as exc:
            raise UnsafeProviderDataError(
                "queue service must be radarr or sonarr"
            ) from exc
    else:
        raise UnsafeProviderDataError("queue service is missing")
    if result not in {ServiceName.RADARR, ServiceName.SONARR}:
        raise UnsafeProviderDataError("queue service must be radarr or sonarr")
    return result


def _dependency_service(value: object) -> ServiceName:
    """Parse any configured dependency for partial-error records."""

    if isinstance(value, ServiceName):
        return value
    if isinstance(value, str):
        try:
            return ServiceName(value.strip().lower())
        except ValueError as exc:
            raise UnsafeProviderDataError("partial error service is invalid") from exc
    raise UnsafeProviderDataError("partial error service is missing")


def _queue_state(value: object) -> QueueState:
    if isinstance(value, QueueState):
        return value
    if not isinstance(value, str):
        return QueueState.UNKNOWN
    normalized = value.strip().lower().replace("_", "").replace("-", "")
    aliases = {
        "queued": QueueState.QUEUED,
        "pending": QueueState.QUEUED,
        "downloading": QueueState.DOWNLOADING,
        "download": QueueState.DOWNLOADING,
        "importing": QueueState.IMPORTING,
        "importpending": QueueState.IMPORTING,
        "paused": QueueState.PAUSED,
        "failed": QueueState.FAILED,
        "error": QueueState.FAILED,
        "completed": QueueState.COMPLETED,
        "complete": QueueState.COMPLETED,
    }
    return aliases.get(normalized, QueueState.UNKNOWN)


def _eta_seconds(value: object) -> int | None:
    result = _int(value, minimum=0)
    if result is not None:
        return result
    if isinstance(value, str):
        match = re.fullmatch(r"\s*([0-9]+):([0-9]{1,2}):([0-9]{1,2})\s*", value)
        if match:
            hours, minutes, seconds = (int(part) for part in match.groups())
            total = hours * 3600 + minutes * 60 + seconds
            return total
    return None


def _plex_origin_parts(origin: str) -> tuple[str, str, int | None]:
    if not isinstance(origin, str):
        raise InvalidPlexLinkError("Plex origin must be text")
    text = origin.strip()
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise InvalidPlexLinkError("Plex origin must be HTTP(S)")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidPlexLinkError(
            "Plex origin cannot contain credentials or arguments"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidPlexLinkError("Plex origin has an invalid port") from exc
    host = parsed.hostname.lower().rstrip(".")
    return parsed.scheme.lower(), host, port


def validate_plex_link(
    value: object,
    *,
    allowed_origins: Iterable[str] | None = None,
    allowed_hosts: Iterable[str] | None = None,
    rating_key: str | None = None,
) -> str:
    """Validate and canonicalize a server-generated Plex Web link.

    Only configured Plex origins or the public Plex Web hosts are accepted.
    Credentials, sensitive query keys, path traversal, controls, and arbitrary
    provider URLs are rejected.  ``rating_key`` can be supplied to bind the
    link to a canonical Plex metadata key.
    """

    if not isinstance(value, str) or not value.strip():
        raise InvalidPlexLinkError("Plex link must be a non-empty URL")
    text = value.strip()
    if any(ord(char) < 0x20 or char.isspace() for char in text):
        raise InvalidPlexLinkError("Plex link contains whitespace or controls")
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise InvalidPlexLinkError("Plex link must use HTTP(S) with a host")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidPlexLinkError("Plex link cannot contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidPlexLinkError("Plex link has an invalid port") from exc
    host = parsed.hostname.lower().rstrip(".")

    accepted: set[tuple[str, str, int | None]] = set()
    if allowed_origins is not None:
        accepted.update(_plex_origin_parts(origin) for origin in allowed_origins)
    if allowed_hosts is not None:
        for raw_host in allowed_hosts:
            if not isinstance(raw_host, str) or not raw_host.strip():
                continue
            candidate = raw_host.strip().lower().rstrip(".")
            if "://" in candidate:
                _scheme, candidate_host, candidate_port = _plex_origin_parts(candidate)
                accepted.add((_scheme, candidate_host, candidate_port))
            else:
                accepted.add((scheme, candidate, None))
    if accepted:
        if (scheme, host, port) not in accepted and (
            scheme,
            host,
            None,
        ) not in accepted:
            raise InvalidPlexLinkError("Plex link origin is not configured")
    elif host not in _KNOWN_PLEX_HOSTS:
        raise InvalidPlexLinkError("Plex link host is not a validated Plex host")

    path = parsed.path
    decoded_path = unquote(path)
    if (
        "\x00" in path
        or ".." in decoded_path.split("/")
        or "%2f" in path.lower()
        or "%5c" in path.lower()
    ):
        raise InvalidPlexLinkError("Plex link path is unsafe")
    path_segments = {segment for segment in decoded_path.lower().split("/") if segment}
    if not path_segments.intersection({"web", "desktop"}):
        raise InvalidPlexLinkError("Plex link is not a Plex Web route")

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.casefold() in _SENSITIVE_QUERY_KEYS for key, _value in query_pairs):
        raise InvalidPlexLinkError("Plex link query contains credentials")
    metadata_keys: list[str] = []
    for key, query_value in query_pairs:
        if key.casefold() != "key":
            raise InvalidPlexLinkError("Plex link query contains unsupported data")
        # A Plex metadata key is an intentionally bounded exception below; no
        # other query value may carry a path or URL.
        if key.casefold() == "key" and not re.fullmatch(
            r"/library/metadata/[1-9][0-9]*", unquote(query_value)
        ):
            raise InvalidPlexLinkError("Plex link metadata key is invalid")
        if key.casefold() == "key":
            metadata_keys.append(unquote(query_value))
        if key.casefold() != "key" and (
            _URL_RE.search(query_value) or _ABSOLUTE_PATH_RE.search(query_value)
        ):
            raise InvalidPlexLinkError("Plex link query contains unsafe data")
    full_lower = text.casefold()
    if any(
        marker in full_lower
        for marker in ("token=", "apikey=", "api_key=", "password=", "access_token=")
    ):
        raise InvalidPlexLinkError("Plex link contains credential material")
    fragment = unquote(parsed.fragment)
    if _URL_RE.search(fragment) or _CREDENTIAL_RE.search(fragment):
        raise InvalidPlexLinkError("Plex link fragment contains unsafe data")
    fragment_route, separator, fragment_query = fragment.partition("?")
    if fragment and not fragment_route:
        raise InvalidPlexLinkError("Plex link fragment route is invalid")
    if fragment_route and not re.fullmatch(
        r"!/server/[A-Za-z0-9._:%-]{1,128}/details", fragment_route
    ):
        raise InvalidPlexLinkError("Plex link fragment route is invalid")
    if separator:
        fragment_pairs = parse_qsl(fragment_query, keep_blank_values=True)
        if any(key.casefold() != "key" for key, _value in fragment_pairs):
            raise InvalidPlexLinkError("Plex link fragment has unsupported data")
    embedded_keys = re.findall(r"(?i)(?:[?&]key=)([^&#]+)", fragment)
    for embedded_key in embedded_keys:
        metadata_key = unquote(embedded_key)
        if not re.fullmatch(r"/library/metadata/[1-9][0-9]*", metadata_key):
            raise InvalidPlexLinkError("Plex link metadata key is invalid")
        metadata_keys.append(metadata_key)
    if not metadata_keys:
        raise InvalidPlexLinkError("Plex link is not bound to Plex metadata")
    if len(metadata_keys) != 1 or len(set(metadata_keys)) != 1:
        raise InvalidPlexLinkError("Plex link contains conflicting metadata keys")
    if rating_key is not None:
        try:
            canonical = canonical_rating_key(rating_key)
        except ModelValidationError as exc:
            raise InvalidPlexLinkError("rating_key is not canonical") from exc
        expected = f"/library/metadata/{canonical}"
        if metadata_keys != [expected]:
            raise InvalidPlexLinkError("Plex link does not match rating_key")

    # Preserve the fragment/query route while canonicalizing scheme and host.
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((scheme, netloc, path, parsed.query, parsed.fragment))


validate_plex_url = validate_plex_link
validated_plex_link = validate_plex_link


def build_plex_link(
    origin: str,
    machine_identifier: str,
    rating_key: str,
) -> str:
    """Build a Plex Web URL from trusted configured values only."""

    scheme, host, port = _plex_origin_parts(origin)
    if not isinstance(machine_identifier, str) or not re.fullmatch(
        r"[A-Za-z0-9._:-]{1,128}", machine_identifier
    ):
        raise InvalidPlexLinkError("machine identifier is not canonical")
    try:
        canonical_key = canonical_rating_key(rating_key)
    except ModelValidationError as exc:
        raise InvalidPlexLinkError("rating_key is not canonical") from exc
    netloc = host if port is None else f"{host}:{port}"
    fragment = (
        "!/server/"
        + quote(machine_identifier, safe="")
        + "/details?key=/library/metadata/"
        + canonical_key
    )
    candidate = urlunsplit((scheme, netloc, "/web/index.html", "", fragment))
    canonical = validate_plex_link(
        candidate, allowed_origins=(origin,), rating_key=canonical_key
    )
    return _ValidatedPlexLink(canonical, (origin,))


make_plex_link = build_plex_link


def sanitize_media_candidate(
    value: MediaCandidate | Mapping[str, Any],
    *,
    media_type: MediaType | str | None = None,
    plex_origins: Iterable[str] | None = None,
) -> MediaCandidate:
    """Convert one provider result into an allow-listed search candidate."""

    if isinstance(value, MediaCandidate):
        # Reconstruct to avoid retaining an arbitrary subclass/object supplied
        # by an adapter and to enforce the same bounded text checks.
        source: Mapping[str, Any] = {
            "media_type": value.media_type,
            "provider_id": value.provider_id,
            "title": value.title,
            "year": value.year,
            "overview": value.overview,
            "candidate_handle": value.candidate_handle,
            "tmdb_id": value.identity.tmdb_id if value.identity is not None else None,
            "tvdb_id": value.identity.tvdb_id if value.identity is not None else None,
            "imdb_id": value.identity.imdb_id if value.identity is not None else None,
        }
        candidate_handle = value.candidate_handle
    elif isinstance(value, Mapping):
        source = value
        # Provider mappings cannot inject an actionable reference, but a
        # previously issued handle may be restored from a durable snapshot.
        candidate_handle = _value(source, "candidate_handle", "candidateHandle")
        if candidate_handle is not None and not isinstance(candidate_handle, str):
            raise UnsafeProviderDataError("candidate_handle must be text")
    else:
        raise UnsafeProviderDataError("search result must be a mapping")
    selected_type = _media_type(
        _value(source, "media_type", "mediaType", "type"),
        default=None if media_type is None else _media_type(media_type),
    )
    if selected_type is MediaType.EPISODE:
        selected_type = MediaType.SERIES
    provider_id = _provider_id(source, selected_type)
    title = _required_text(
        _value(source, "title", "name", "original_title", "originalTitle"), "title"
    )
    overview = _text(
        _value(source, "overview", "summary", "description"),
        "overview",
        max_bytes=16 * 1024,
    )
    try:
        candidate = MediaCandidate(
            media_type=selected_type,
            provider_id=provider_id,
            title=title,
            year=_year(source),
            overview=overview,
            identity=_identity(source, selected_type, provider_id),
            candidate_handle=candidate_handle,
        )
    except ModelValidationError as exc:
        raise UnsafeProviderDataError(
            "search result is not a valid normalized candidate"
        ) from exc
    # A search result may carry a Plex link, but only the dedicated validated
    # field is allowed to retain it.  MediaCandidate does not retain that field
    # intentionally; validate it so malformed input fails closed.
    raw_link = _value(source, "plex_url", "plexUrl")
    if raw_link is not None:
        validate_plex_link(raw_link, allowed_origins=plex_origins)
    return candidate


sanitize_search_result = sanitize_media_candidate
normalize_media_candidate = sanitize_media_candidate


def sanitize_queue_item(
    value: QueueItem | Mapping[str, Any],
    *,
    service: ServiceName | str | None = None,
) -> QueueItem:
    """Convert a Radarr/Sonarr queue object to safe title/progress/ETA data."""

    if isinstance(value, QueueItem):
        source: Mapping[str, Any] = {
            "service": value.service,
            "title": value.title,
            "state": value.state,
            "progress_percent": value.progress_percent,
            "eta_seconds": value.eta_seconds,
            "error": value.error,
            "media_type": value.media_type,
        }
    elif isinstance(value, Mapping):
        source = value
    else:
        raise UnsafeProviderDataError("queue item must be a mapping")
    selected_service = _service(
        _value(source, "service", "source", "app") if service is None else service
    )
    title = _required_text(
        _value(source, "title", "series_title", "movie_title", "name"), "title"
    )
    state = _queue_state(_value(source, "state", "status", "tracked_download_state"))
    progress = _float(
        _value(source, "progress_percent", "progressPercent"), minimum=0, maximum=100
    )
    if progress is None:
        size = _float(_value(source, "size", "size_bytes"), minimum=0)
        left = _float(_value(source, "sizeleft", "size_left", "remaining"), minimum=0)
        if size is not None and left is not None and size > 0:
            progress = min(100.0, max(0.0, (size - left) * 100 / size))
    eta = _eta_seconds(_value(source, "eta_seconds", "eta", "time_left", "timeLeft"))
    error = _text(
        _value(source, "error", "error_message", "errorMessage", "message"),
        "error",
        max_bytes=2 * 1024,
    )
    media_raw = _value(source, "media_type", "mediaType", "type")
    selected_media = (
        None if media_raw is None else _media_type(media_raw, default=MediaType.MOVIE)
    )
    if selected_media is MediaType.EPISODE:
        selected_media = MediaType.SERIES
    try:
        return QueueItem(
            service=selected_service,
            title=title,
            state=state,
            progress_percent=progress,
            eta_seconds=eta,
            error=error,
            media_type=selected_media,
        )
    except ModelValidationError as exc:
        raise UnsafeProviderDataError(
            "queue item is not a valid normalized record"
        ) from exc


normalize_queue_item = sanitize_queue_item


@dataclass(frozen=True, slots=True)
class SafeLibraryItem:
    """A Plex library item without rating-key/path/provider internals."""

    media_type: MediaType
    title: str
    year: int | None = None
    library_name: str | None = None
    show_title: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    quality: str | None = None
    plex_url: str | None = None
    added_at: datetime | None = None

    def __post_init__(self) -> None:
        selected = _media_type(self.media_type)
        if selected not in {MediaType.MOVIE, MediaType.SERIES, MediaType.EPISODE}:
            raise UnsafeProviderDataError("library media type is invalid")
        object.__setattr__(self, "media_type", selected)
        object.__setattr__(self, "title", _required_text(self.title, "title"))
        year = _int(self.year, minimum=1800, maximum=3000)
        object.__setattr__(self, "year", year)
        for name in ("library_name", "show_title", "quality"):
            object.__setattr__(
                self, name, _text(getattr(self, name), name, max_bytes=512)
            )
        for name in ("season_number", "episode_number"):
            parsed = _int(getattr(self, name), minimum=0)
            object.__setattr__(self, name, parsed)
        if self.plex_url is not None:
            if isinstance(self.plex_url, _ValidatedPlexLink):
                object.__setattr__(
                    self,
                    "plex_url",
                    _validated_link(
                        self.plex_url,
                        allowed_origins=self.plex_url.allowed_origins,
                    ),
                )
            else:
                raise InvalidPlexLinkError(
                    "Plex links must be generated from trusted server context"
                )
        if self.added_at is not None:
            parsed_dt = _parse_datetime(self.added_at)
            if parsed_dt is None:
                raise UnsafeProviderDataError("added_at must be timezone-aware")
            object.__setattr__(self, "added_at", parsed_dt)


LibraryItem = SafeLibraryItem
SafePlexItem = SafeLibraryItem


def sanitize_library_item(
    value: SafeLibraryItem | PlexItem | Mapping[str, Any],
    *,
    plex_origins: Iterable[str] | None = None,
    plex_origin: str | None = None,
    machine_identifier: str | None = None,
) -> SafeLibraryItem:
    """Normalize a Plex item and retain only safe display/link fields."""

    source: Mapping[str, Any]
    if isinstance(value, SafeLibraryItem):
        source = {
            "media_type": value.media_type,
            "title": value.title,
            "year": value.year,
            "library_name": value.library_name,
            "show_title": value.show_title,
            "season_number": value.season_number,
            "episode_number": value.episode_number,
            "quality": value.quality,
            "plex_url": value.plex_url,
            "added_at": value.added_at,
        }
    elif isinstance(value, PlexItem):
        source = {
            "rating_key": value.rating_key,
            "media_type": value.media_type,
            "title": value.title,
            "year": value.year,
            "library_name": value.library_name,
            "show_title": value.show_title,
            "season_number": value.season_number,
            "episode_number": value.episode_number,
            "quality": value.quality,
            "plex_url": value.plex_url,
            "added_at": value.added_at,
        }
    elif isinstance(value, Mapping):
        source = value
    else:
        raise UnsafeProviderDataError("library item must be a mapping")

    raw_type = _value(source, "media_type", "mediaType", "type")
    selected_type = _media_type(raw_type, default=MediaType.MOVIE)
    if selected_type is MediaType.TV:
        selected_type = MediaType.SERIES
    title = _required_text(_value(source, "title", "name"), "title")
    rating_raw = _value(source, "rating_key", "ratingKey")
    rating_key: str | None = None
    if rating_raw is not None:
        if not isinstance(rating_raw, str):
            rating_raw = str(rating_raw)
        try:
            rating_key = canonical_rating_key(rating_raw)
        except ModelValidationError as exc:
            raise UnsafeProviderDataError("rating_key is not canonical") from exc

    raw_link = _value(source, "plex_url", "plexUrl", "web_url", "webUrl")
    link: str | None = None
    if raw_link is not None:
        if not isinstance(raw_link, _ValidatedPlexLink):
            raise UnsafeProviderDataError(
                "provider-supplied Plex links cannot cross the safe boundary"
            )
        link = _validated_link(
            raw_link,
            allowed_origins=raw_link.allowed_origins,
            rating_key=rating_key,
        )
    elif (
        plex_origin is not None
        and machine_identifier is not None
        and rating_key is not None
    ):
        link = _validated_link(
            build_plex_link(plex_origin, machine_identifier, rating_key),
            allowed_origins=(plex_origin,),
            rating_key=rating_key,
        )

    raw_added_at = _value(source, "added_at", "addedAt")
    added_at = _parse_datetime(raw_added_at)
    if raw_added_at is not None and added_at is None:
        raise UnsafeProviderDataError("added_at must be a timezone-aware timestamp")

    try:
        return SafeLibraryItem(
            media_type=selected_type,
            title=title,
            year=_year(source),
            library_name=_text(
                _value(source, "library_name", "libraryName"), "library_name"
            ),
            show_title=_text(
                _value(source, "show_title", "grandparentTitle", "series_title"),
                "show_title",
            ),
            season_number=_int(
                _value(source, "season_number", "parentIndex", "season"), minimum=0
            ),
            episode_number=_int(
                _value(source, "episode_number", "index", "episode"), minimum=0
            ),
            quality=_text(_value(source, "quality", "quality_profile"), "quality"),
            plex_url=link,
            added_at=added_at,
        )
    except (ModelValidationError, InvalidPlexLinkError) as exc:
        raise UnsafeProviderDataError(
            "library item is not a valid normalized record"
        ) from exc


sanitize_plex_item = sanitize_library_item
normalize_library_item = sanitize_library_item


def sanitize_media_status(
    value: MediaStatus | Mapping[str, Any],
    *,
    plex_origins: Iterable[str] | None = None,
) -> MediaStatus:
    """Normalize availability status and validate its optional Plex link."""

    if isinstance(value, MediaStatus):
        source: Mapping[str, Any] = {
            "identity": value.identity,
            "available": value.available,
            "title": value.title,
            "year": value.year,
            "quality": value.quality,
            "plex_url": value.plex_url,
            "as_of": value.as_of,
        }
    elif isinstance(value, Mapping):
        source = value
    else:
        raise UnsafeProviderDataError("media status must be a mapping")
    identity_raw = _value(source, "identity")
    if isinstance(identity_raw, MediaIdentity):
        selected_type = _media_type(identity_raw.media_type)
        identity_source: Mapping[str, Any] = {
            "provider_id": identity_raw.provider_id,
            "tmdb_id": identity_raw.tmdb_id,
            "tvdb_id": identity_raw.tvdb_id,
            "imdb_id": identity_raw.imdb_id,
        }
        provider_id = _provider_id(identity_source, selected_type)
        identity = _identity(identity_source, selected_type, provider_id)
    elif isinstance(identity_raw, Mapping):
        selected_type = _media_type(
            _value(identity_raw, "media_type", "mediaType", "type"),
            default=MediaType.MOVIE,
        )
        provider_id = _provider_id(identity_raw, selected_type)
        identity = _identity(identity_raw, selected_type, provider_id)
    else:
        selected_type = _media_type(
            _value(source, "media_type", "mediaType", "type"), default=MediaType.MOVIE
        )
        provider_id = _provider_id(source, selected_type)
        identity = _identity(source, selected_type, provider_id)
    available = _value(source, "available", "exists")
    if not isinstance(available, bool):
        raise UnsafeProviderDataError("available must be a boolean")
    raw_link = _value(source, "plex_url", "plexUrl", "web_url", "webUrl")
    link = None if raw_link is None else _trusted_link(raw_link)
    raw_as_of = _value(source, "as_of", "asOf")
    as_of = _parse_datetime(raw_as_of)
    if raw_as_of is not None and as_of is None:
        raise UnsafeProviderDataError("as_of must be a timezone-aware timestamp")
    try:
        status = MediaStatus(
            identity=identity,
            available=available,
            title=_text(_value(source, "title", "name"), "title"),
            year=_year(source),
            quality=_text(_value(source, "quality", "quality_profile"), "quality"),
            plex_url=link,
            as_of=as_of,
        )
        if isinstance(link, _ValidatedPlexLink):
            object.__setattr__(status, "plex_url", link)
        return status
    except ModelValidationError as exc:
        raise UnsafeProviderDataError(
            "media status is not a valid normalized record"
        ) from exc


normalize_media_status = sanitize_media_status


def sanitize_partial_error(value: PartialError | Mapping[str, Any]) -> PartialError:
    if isinstance(value, PartialError):
        source: Mapping[str, Any] = {
            "service": value.service,
            "code": value.code,
            "message": value.message,
            "retryable": value.retryable,
        }
    elif isinstance(value, Mapping):
        source = value
    else:
        raise UnsafeProviderDataError("partial error must be a mapping")
    selected_service = _dependency_service(_value(source, "service", "dependency"))
    code = _required_text(_value(source, "code", "error_code"), "code", max_bytes=128)
    message = _required_text(
        _value(source, "message", "error"), "message", max_bytes=2 * 1024
    )
    retryable = _value(source, "retryable", "is_retryable")
    if retryable is None:
        retryable = True
    if not isinstance(retryable, bool):
        raise UnsafeProviderDataError("partial error retryable must be boolean")
    try:
        return PartialError(selected_service, code, message, retryable)
    except ModelValidationError as exc:
        raise UnsafeProviderDataError("partial error is invalid") from exc


normalize_partial_error = sanitize_partial_error


@dataclass(frozen=True, slots=True)
class SafeServiceHealth:
    """Typed, credential-free dependency health summary."""

    service: ServiceName
    ok: bool
    version: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "service", _dependency_service(self.service))
        if not isinstance(self.ok, bool):
            raise UnsafeProviderDataError("health ok must be boolean")
        object.__setattr__(
            self, "version", _text(self.version, "version", max_bytes=128)
        )
        object.__setattr__(
            self, "message", _text(self.message, "message", max_bytes=512)
        )


ServiceHealth = SafeServiceHealth


def sanitize_service_health(
    value: SafeServiceHealth | Mapping[str, Any],
) -> SafeServiceHealth:
    if isinstance(value, SafeServiceHealth):
        value = {
            "service": value.service,
            "ok": value.ok,
            "version": value.version,
            "message": value.message,
        }
    if not isinstance(value, Mapping):
        raise UnsafeProviderDataError("service health must be a mapping")
    service = _dependency_service(_value(value, "service", "name"))
    ok = _value(value, "ok", "healthy", "connected")
    if not isinstance(ok, bool):
        raise UnsafeProviderDataError("health ok must be boolean")
    return SafeServiceHealth(
        service=service,
        ok=ok,
        version=_text(
            _value(value, "version", "app_version"), "version", max_bytes=128
        ),
        message=_text(_value(value, "message", "status"), "message", max_bytes=512),
    )


normalize_service_health = sanitize_service_health


@dataclass(frozen=True, slots=True)
class SafeRequestStatus:
    """Bounded request status record without provider/download internals."""

    title: str
    media_type: MediaType
    status: str
    year: int | None = None
    progress_percent: float | None = None
    eta_seconds: int | None = None
    quality: str | None = None
    plex_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _required_text(self.title, "title"))
        object.__setattr__(
            self, "media_type", _media_type(self.media_type, default=MediaType.MOVIE)
        )
        object.__setattr__(
            self, "status", _required_text(self.status, "status", max_bytes=128)
        )
        object.__setattr__(self, "year", _int(self.year, minimum=1800, maximum=3000))
        object.__setattr__(
            self,
            "progress_percent",
            _float(self.progress_percent, minimum=0, maximum=100),
        )
        object.__setattr__(self, "eta_seconds", _eta_seconds(self.eta_seconds))
        object.__setattr__(self, "quality", _text(self.quality, "quality"))
        if self.plex_url is not None:
            if isinstance(self.plex_url, _ValidatedPlexLink):
                object.__setattr__(
                    self,
                    "plex_url",
                    _validated_link(
                        self.plex_url,
                        allowed_origins=self.plex_url.allowed_origins,
                    ),
                )
            else:
                raise InvalidPlexLinkError(
                    "Plex links must be generated from trusted server context"
                )


RequestStatus = SafeRequestStatus


def sanitize_request_status(
    value: SafeRequestStatus | Mapping[str, Any],
    *,
    plex_origins: Iterable[str] | None = None,
) -> SafeRequestStatus:
    if isinstance(value, SafeRequestStatus):
        value = {
            "title": value.title,
            "media_type": value.media_type,
            "status": value.status,
            "year": value.year,
            "progress_percent": value.progress_percent,
            "eta_seconds": value.eta_seconds,
            "quality": value.quality,
            "plex_url": value.plex_url,
        }
    if not isinstance(value, Mapping):
        raise UnsafeProviderDataError("request status must be a mapping")
    raw_link = _value(value, "plex_url", "plexUrl", "web_url", "webUrl")
    link = None if raw_link is None else _trusted_link(raw_link)
    return SafeRequestStatus(
        title=_value(value, "title", "name"),
        media_type=_media_type(
            _value(value, "media_type", "mediaType", "type"), default=MediaType.MOVIE
        ),
        status=_value(value, "status", "state"),
        year=_year(value),
        progress_percent=_value(value, "progress_percent", "progressPercent"),
        eta_seconds=_value(value, "eta_seconds", "eta", "time_left"),
        quality=_value(value, "quality", "quality_profile"),
        plex_url=link,
    )


normalize_request_status = sanitize_request_status


_SAFE_RECORD_TYPES: Final[tuple[type[Any], ...]] = (
    MediaCandidate,
    QueueItem,
    SafeLibraryItem,
    MediaStatus,
    PartialError,
    SafeServiceHealth,
    SafeRequestStatus,
)


def _assert_typed_record(value: object) -> None:
    """Reject provider models/mappings before they enter a safe snapshot."""

    # Exact concrete records keep adapter/provider subclasses from smuggling
    # an overridden property or serializer across the boundary.
    if type(value) not in _SAFE_RECORD_TYPES:
        raise UnsafeProviderDataError(
            "response item is not a known typed normalized record"
        )


def _canonical_record(value: object) -> object:
    """Rebuild a typed record so model-constructed values are revalidated."""

    _assert_typed_record(value)
    if type(value) is MediaCandidate:
        return sanitize_media_candidate(value)
    if type(value) is QueueItem:
        return sanitize_queue_item(value)
    if type(value) is SafeLibraryItem:
        return sanitize_library_item(value)
    if type(value) is MediaStatus:
        return sanitize_media_status(value)
    if type(value) is PartialError:
        return sanitize_partial_error(value)
    if type(value) is SafeServiceHealth:
        return sanitize_service_health(value)
    if type(value) is SafeRequestStatus:
        return sanitize_request_status(value)
    # ``_assert_typed_record`` above makes this unreachable, but retaining a
    # fail-closed branch keeps future additions from silently bypassing it.
    raise UnsafeProviderDataError("response item cannot be canonicalized")


def _sanitize_partial_errors(
    values: Iterable[PartialError | Mapping[str, Any]],
) -> tuple[PartialError, ...]:
    result: list[PartialError] = []
    for index, error in enumerate(values):
        if index >= MAX_PARTIAL_ERRORS:
            raise UnsafeProviderDataError(
                f"partial_errors cannot contain more than {MAX_PARTIAL_ERRORS} entries"
            )
        result.append(sanitize_partial_error(error))
    return tuple(result)


RecordT = TypeVar("RecordT")


@dataclass(frozen=True, slots=True)
class SafePage(Generic[RecordT]):
    """Serialized-shape contract shared by all bounded safe views."""

    items: tuple[RecordT, ...] = field(default_factory=tuple)
    as_of: datetime = field(default_factory=lambda: datetime.now(_UTC))
    next_cursor: str | None = None
    truncated: bool = False
    total: int | None = None
    partial_errors: tuple[PartialError, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        bounded_items: list[RecordT] = []
        for item in self.items:
            if len(bounded_items) >= MAX_SNAPSHOT_ITEMS:
                raise SafeViewError("page exceeds the 5,000-item snapshot cap")
            bounded_items.append(cast(RecordT, _canonical_record(item)))
        object.__setattr__(self, "items", tuple(bounded_items))
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            raise SafeViewError("as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(_UTC))
        if self.next_cursor is not None:
            object.__setattr__(
                self, "next_cursor", _validate_cursor_shape(self.next_cursor)
            )
        if not isinstance(self.truncated, bool):
            raise SafeViewError("truncated must be boolean")
        if self.total is not None:
            if (
                isinstance(self.total, bool)
                or not isinstance(self.total, int)
                or self.total < 0
            ):
                raise SafeViewError("total must be a non-negative integer")
            if self.total > MAX_SNAPSHOT_ITEMS:
                object.__setattr__(self, "total", MAX_SNAPSHOT_ITEMS)
                object.__setattr__(self, "truncated", True)
            if self.total < len(self.items):
                object.__setattr__(self, "total", len(self.items))
            if self.total > len(self.items):
                object.__setattr__(self, "truncated", True)
        object.__setattr__(
            self,
            "partial_errors",
            _sanitize_partial_errors(self.partial_errors),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "as_of": self.as_of.isoformat().replace("+00:00", "Z"),
            "items": [serialize_record(item) for item in self.items],
            "truncated": self.truncated,
        }
        if self.next_cursor is not None:
            # Keep the authentication marker process-local.  JSON/text callers
            # receive only the signed wire value; a caller cannot manufacture
            # a value accepted by ``SafePage`` by round-tripping it back in.
            result["next_cursor"] = str(self.next_cursor)
        if self.total is not None:
            result["total"] = self.total
        if self.partial_errors:
            result["partial_errors"] = [
                serialize_record(error) for error in self.partial_errors
            ]
        return result


BoundedPage = SafePage
SafeResponsePage = SafePage


def _identity_dict(identity: MediaIdentity) -> dict[str, Any]:
    # The identity is useful to the trusted companion for matching, but the
    # shared response contract does not disclose provider identifiers.  A
    # candidate handle/request workflow owns any opaque action reference.
    return {"media_type": identity.media_type.value}


def serialize_record(value: object) -> dict[str, Any]:
    """Serialize only known normalized record types."""

    if type(value) in _SAFE_RECORD_TYPES:
        value = _canonical_record(value)
    if isinstance(value, MediaCandidate):
        result: dict[str, Any] = {
            "media_type": value.media_type.value,
            "title": value.title,
        }
        if value.year is not None:
            result["year"] = value.year
        if value.overview is not None:
            result["overview"] = value.overview
        if value.candidate_handle is not None:
            result["candidate_handle"] = value.candidate_handle
        # Provider identities stay server-side.  Request wrappers must attach
        # an actor/update-bound opaque candidate handle at their own seam;
        # exposing a stable provider ID here would let the model invent a
        # request target outside the current search result.
        return result
    if isinstance(value, QueueItem):
        result = {
            "service": value.service.value,
            "title": value.title,
            "state": value.state.value,
        }
        if value.progress_percent is not None:
            result["progress_percent"] = value.progress_percent
        if value.eta_seconds is not None:
            result["eta_seconds"] = value.eta_seconds
        if value.error is not None:
            result["error"] = value.error
        if value.media_type is not None:
            result["media_type"] = value.media_type.value
        return result
    if isinstance(value, SafeLibraryItem):
        result = {
            "media_type": value.media_type.value,
            "title": value.title,
        }
        for name in (
            "year",
            "library_name",
            "show_title",
            "season_number",
            "episode_number",
            "quality",
            "plex_url",
        ):
            field_value = getattr(value, name)
            if field_value is not None:
                result[name] = field_value
        if value.added_at is not None:
            result["added_at"] = value.added_at.isoformat().replace("+00:00", "Z")
        return result
    if isinstance(value, PlexItem):
        return serialize_record(sanitize_library_item(value))
    if isinstance(value, MediaStatus):
        if value.plex_url is not None and not isinstance(
            value.plex_url, _ValidatedPlexLink
        ):
            raise UnsafeProviderDataError(
                "media status contains a provider-supplied Plex link"
            )
        result = {
            "identity": _identity_dict(value.identity),
            "available": value.available,
        }
        for name in ("title", "year", "quality", "plex_url"):
            field_value = getattr(value, name)
            if field_value is not None:
                result[name] = field_value
        if value.as_of is not None:
            result["as_of"] = (
                value.as_of.astimezone(_UTC).isoformat().replace("+00:00", "Z")
            )
        return result
    if isinstance(value, PartialError):
        return {
            "service": value.service.value,
            "code": value.code,
            "message": value.message,
            "retryable": value.retryable,
        }
    if isinstance(value, SafeServiceHealth):
        result = {"service": value.service.value, "ok": value.ok}
        if value.version is not None:
            result["version"] = value.version
        if value.message is not None:
            result["message"] = value.message
        return result
    if isinstance(value, SafeRequestStatus):
        result = {
            "media_type": value.media_type.value,
            "title": value.title,
            "status": value.status,
        }
        for name in ("year", "progress_percent", "eta_seconds", "quality", "plex_url"):
            field_value = getattr(value, name)
            if field_value is not None:
                result[name] = field_value
        return result
    raise UnsafeProviderDataError(
        "response item is not a known typed normalized record"
    )


to_safe_dict = serialize_record
serialize_safe_record = serialize_record


def _assert_safe_value(value: object, *, key: str | None = None) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise UnsafeProviderDataError("response keys must be text")
            _assert_safe_value(child, key=raw_key)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _assert_safe_value(child, key=key)
        return
    if isinstance(value, str):
        if key in {"plex_url", "plex_link"}:
            if not isinstance(value, _ValidatedPlexLink):
                raise UnsafeProviderDataError(
                    "Plex links must be generated from trusted server context"
                )
            validate_plex_link(value, allowed_origins=value.allowed_origins)
            return
        if (
            _URL_RE.search(value)
            or _URI_SCHEME_RE.search(value)
            or _UNC_PATH_RE.search(value)
            or _CREDENTIAL_RE.search(value)
            or _ABSOLUTE_PATH_RE.search(value)
        ):
            raise UnsafeProviderDataError("serialized response contains forbidden data")
        if _CONTROL_RE.search(value):
            raise UnsafeProviderDataError("serialized response contains controls")
        return
    if isinstance(value, (int, float, bool)) or value is None:
        if isinstance(value, float) and not math.isfinite(value):
            raise UnsafeProviderDataError(
                "serialized response contains a non-finite number"
            )
        return
    raise UnsafeProviderDataError("serialized response contains an unsupported value")


def response_dict(
    value: SafePage[Any] | Page[Any] | Mapping[str, Any] | object,
) -> dict[str, Any]:
    """Return an allow-listed response mapping before JSON serialization."""

    if isinstance(value, SafePage):
        result = value.to_dict()
    elif isinstance(value, Page):
        result = SafePage(
            items=value.items,
            as_of=value.as_of or datetime.now(_UTC),
            next_cursor=value.next_cursor,
            truncated=value.truncated,
            total=value.total,
            partial_errors=value.partial_errors,
        ).to_dict()
    elif isinstance(value, Mapping):
        # A generic mapping is never recursively forwarded.  It is accepted
        # only when it already has the exact page envelope and each item is a
        # known typed record; arbitrary provider dictionaries fail closed.
        allowed = {
            "items",
            "as_of",
            "next_cursor",
            "truncated",
            "total",
            "partial_errors",
        }
        if set(value) - allowed or "items" not in value:
            raise UnsafeProviderDataError(
                "raw provider response cannot cross the safe-view boundary"
            )
        items = value["items"]
        if not isinstance(items, Sequence) or isinstance(
            items, (str, bytes, bytearray)
        ):
            raise UnsafeProviderDataError("response items must be a sequence")
        raw_as_of = value.get("as_of")
        if raw_as_of is None:
            as_of = datetime.now(_UTC)
        else:
            parsed_as_of = _parse_datetime(raw_as_of)
            if parsed_as_of is None:
                raise UnsafeProviderDataError("response as_of must be a timestamp")
            as_of = parsed_as_of
        raw_truncated = value.get("truncated", False)
        if not isinstance(raw_truncated, bool):
            raise UnsafeProviderDataError("response truncated must be boolean")
        bounded_items: list[Any] = []
        for item in items:
            if len(bounded_items) >= MAX_SNAPSHOT_ITEMS:
                raise SafeViewError("page exceeds the 5,000-item snapshot cap")
            bounded_items.append(item)
        result = SafePage[Any](
            items=tuple(bounded_items),
            as_of=as_of,
            next_cursor=value.get("next_cursor"),
            truncated=raw_truncated,
            total=value.get("total"),
            partial_errors=tuple(value.get("partial_errors", ())),
        ).to_dict()
    else:
        raise UnsafeProviderDataError("response is not a bounded typed page")
    _assert_safe_value(result)
    return result


def serialize_response(
    value: SafePage[Any] | Page[Any] | Mapping[str, Any] | object,
) -> bytes:
    """Serialize a typed response and enforce the 256 KiB hard ceiling."""

    result = response_dict(value)
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise UnsafeProviderDataError(
            "response could not be serialized safely"
        ) from exc
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ResponseTooLargeError("serialized response exceeds 256 KiB")
    return encoded


bounded_json = serialize_response
serialize_safe_response = serialize_response
to_json_bytes = serialize_response


def response_size(value: SafePage[Any] | Page[Any] | Mapping[str, Any] | object) -> int:
    return len(serialize_response(value))


def _page_as_of(snapshot: SnapshotRecord) -> datetime:
    stored = snapshot.as_of
    if isinstance(stored, datetime) and stored.tzinfo is not None:
        return stored.astimezone(_UTC)
    return datetime.fromtimestamp(snapshot.issued_at, tz=_UTC)


def _page_total(snapshot: SnapshotRecord) -> int:
    return min(MAX_SNAPSHOT_ITEMS, max(len(snapshot.items), snapshot.total))


def _safe_record_size(value: object) -> int:
    """Validate one typed record and return its compact wire size.

    This is deliberately done before a record is handed to ``SnapshotStore``.
    A provider object that fails normalization or redaction must therefore
    never remain in a server-side continuation snapshot.
    """

    _assert_typed_record(value)
    serialized = serialize_record(value)
    _assert_safe_value(serialized)
    try:
        return len(
            json.dumps(
                serialized,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8", "strict")
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise UnsafeProviderDataError(
            "normalized record could not be serialized safely"
        ) from exc


def _make_page(
    snapshot: SnapshotRecord,
    *,
    offset: int,
    limit: int,
    next_cursor: str | None,
    partial_errors: tuple[PartialError, ...],
    as_of: datetime | None,
    total: int | None,
) -> SafePage[Any]:
    remaining = snapshot.items[offset : offset + limit]
    return SafePage(
        items=remaining,
        as_of=_page_as_of(snapshot) if as_of is None else as_of,
        next_cursor=next_cursor,
        truncated=snapshot.truncated,
        total=_page_total(snapshot) if total is None else total,
        partial_errors=partial_errors,
    )


def _fit_page(
    snapshot: SnapshotRecord,
    *,
    offset: int,
    requested_limit: int,
    next_cursor_factory: Any,
    partial_errors: tuple[PartialError, ...],
    as_of: datetime | None,
    total: int | None,
) -> SafePage[Any]:
    """Choose the largest prefix that fits the serialized response bound."""

    available = snapshot.items[offset : offset + requested_limit]
    if not available:
        return _make_page(
            snapshot,
            offset=offset,
            limit=0,
            next_cursor=None,
            partial_errors=partial_errors,
            as_of=as_of,
            total=total,
        )
    selected: list[Any] = []
    for item in available:
        candidate_items = selected + [item]
        remaining_after = offset + len(candidate_items) < len(snapshot.items)
        candidate_cursor = (
            next_cursor_factory(offset + len(candidate_items))
            if remaining_after
            else None
        )
        candidate = SafePage(
            items=tuple(candidate_items),
            as_of=_page_as_of(snapshot) if as_of is None else as_of,
            next_cursor=candidate_cursor,
            truncated=snapshot.truncated,
            total=_page_total(snapshot) if total is None else total,
            partial_errors=partial_errors,
        )
        try:
            serialize_response(candidate)
        except ResponseTooLargeError:
            break
        selected.append(item)
    if not selected:
        raise ResponseTooLargeError(
            "one normalized item exceeds the 256 KiB response ceiling"
        )
    offset_after = offset + len(selected)
    next_cursor = (
        next_cursor_factory(offset_after)
        if offset_after < len(snapshot.items)
        else None
    )
    return SafePage(
        items=tuple(selected),
        as_of=_page_as_of(snapshot) if as_of is None else as_of,
        next_cursor=next_cursor,
        truncated=snapshot.truncated,
        total=_page_total(snapshot) if total is None else total,
        partial_errors=partial_errors,
    )


class SafeViewPaginator:
    """Create and continue bounded actor-bound snapshots."""

    def __init__(
        self,
        signer: CursorSigner,
        *,
        snapshots: SnapshotStore | None = None,
        clock: object = time.time,
    ) -> None:
        if not isinstance(signer, CursorSigner):
            raise TypeError("signer must be CursorSigner")
        if snapshots is not None and not isinstance(snapshots, SnapshotStore):
            raise TypeError("snapshots must be SnapshotStore")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.signer = signer
        self.snapshots = (
            SnapshotStore(signer, clock=clock) if snapshots is None else snapshots
        )
        self.clock = clock

    def _now(self, now: float | None) -> int:
        current = float(self.clock() if now is None else now)  # type: ignore[operator]
        if not math.isfinite(current) or current < 0:
            raise ValueError("pagination time must be finite and non-negative")
        return int(current)

    def page(
        self,
        items: Iterable[Any] | None = None,
        *,
        tool: str,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        query: object | None = None,
        filter_hash: str | None = None,
        normalize: Any | None = None,
        partial_errors: Iterable[PartialError | Mapping[str, Any]] = (),
        as_of: datetime | None = None,
        total: int | None = None,
        now: float | None = None,
    ) -> SafePage[Any]:
        if (
            user_id is not None
            and actor_user_id is not None
            and user_id != actor_user_id
        ):
            raise SafeViewError("conflicting actor user IDs")
        if (
            chat_id is not None
            and actor_chat_id is not None
            and chat_id != actor_chat_id
        ):
            raise SafeViewError("conflicting actor chat IDs")
        selected_user = user_id if user_id is not None else actor_user_id
        selected_chat = chat_id if chat_id is not None else actor_chat_id
        if selected_user is None or selected_chat is None:
            raise SafeViewError("actor user_id and chat_id are required")
        page_limit = normalize_page_size(tool, limit)
        current = self._now(now)
        if cursor is None:
            if items is None:
                raise SafeViewError("items are required for the first page")
            if not callable(normalize):
                raise SafeViewError(
                    "an explicit normalizer is required before snapshot caching"
                )
            normalizer = normalize
            errors = _sanitize_partial_errors(partial_errors)
            normalized_items: list[Any] = []
            snapshot_bytes = 0
            truncated = False
            snapshot_item_cap = min(MAX_SNAPSHOT_ITEMS, self.snapshots.max_items)
            for raw_item in items:
                if len(normalized_items) >= snapshot_item_cap:
                    truncated = True
                    break
                normalized_item = _canonical_record(normalizer(raw_item))
                item_bytes = _safe_record_size(normalized_item)
                if item_bytes > MAX_SNAPSHOT_BYTES:
                    raise ResponseTooLargeError(
                        "one normalized item exceeds the snapshot size ceiling"
                    )
                if snapshot_bytes + item_bytes > MAX_SNAPSHOT_BYTES:
                    truncated = True
                    break
                normalized_items.append(normalized_item)
                snapshot_bytes += item_bytes
            if filter_hash is None:
                filter_hash = binding_hash(None if query is None else query)
            normalized_as_of = (
                datetime.fromtimestamp(current, tz=_UTC)
                if as_of is None
                else _parse_datetime(as_of)
            )
            if normalized_as_of is None:
                raise SafeViewError("as_of must be timezone-aware")
            if total is None:
                reported_total = len(normalized_items)
            elif isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise SafeViewError("total must be a non-negative integer")
            else:
                reported_total = min(MAX_SNAPSHOT_ITEMS, total)
                truncated = truncated or total > MAX_SNAPSHOT_ITEMS
            if reported_total < len(normalized_items):
                reported_total = len(normalized_items)
            if reported_total > len(normalized_items):
                truncated = True
            snapshot = self.snapshots.create(
                normalized_items,
                user_id=selected_user,
                chat_id=selected_chat,
                tool=tool,
                filter_hash=filter_hash,
                as_of=normalized_as_of,
                total=reported_total,
                partial_errors=errors,
                truncated=truncated,
                now=current,
            )
            offset = 0
        else:
            if (
                filter_hash is not None
                and query is not None
                and filter_hash != binding_hash(query)
            ):
                raise CursorBindingError("conflicting cursor filter bindings")
            continuation_filter = (
                filter_hash
                if filter_hash is not None
                else binding_hash(None if query is None else query)
            )
            claims = self.signer.verify(
                cursor,
                user_id=selected_user,
                chat_id=selected_chat,
                expected_tool=tool,
                expected_filter_hash=continuation_filter,
                now=current,
            )
            snapshot = self.snapshots.get(
                cursor,
                user_id=selected_user,
                chat_id=selected_chat,
                expected_tool=tool,
                expected_filter_hash=continuation_filter,
                expected_page_size=None,
                now=current,
            )
            offset = claims.offset
            if claims.page_size is not None:
                if limit is not None and limit != claims.page_size:
                    raise PageSizeError(
                        "continuation limit must match the original page"
                    )
                page_limit = claims.page_size
            # Continuation metadata is immutable snapshot state.  In
            # particular, do not let a caller replace the original timestamp,
            # totals, or dependency errors by supplying new values here.
            errors = tuple(snapshot.partial_errors)
            as_of = None
            total = None

        def make_cursor(offset_after: int) -> str:
            return _authenticated_cursor(
                self.snapshots.cursor(
                    snapshot, offset=offset_after, page_size=page_limit, now=current
                )
            )

        return _fit_page(
            snapshot,
            offset=offset,
            requested_limit=page_limit,
            next_cursor_factory=make_cursor,
            partial_errors=errors,
            as_of=as_of,
            total=total,
        )

    paginate = page
    build_page = page


Paginator = SafeViewPaginator
BoundedPaginator = SafeViewPaginator


def paginate(
    items: Iterable[Any] | None = None,
    **kwargs: Any,
) -> SafePage[Any]:
    """Functional convenience wrapper around :class:`SafeViewPaginator`."""

    signer = kwargs.pop("signer", None)
    if not isinstance(signer, CursorSigner):
        raise SafeViewError("signer is required for actor-bound pagination")
    snapshots = kwargs.pop("snapshots", None)
    return SafeViewPaginator(signer, snapshots=snapshots).page(items, **kwargs)


paginate_items = paginate
bounded_paginate = paginate


def bounded_page(
    items: Iterable[Any],
    *,
    tool: str = "search_media",
    limit: int | None = None,
    as_of: datetime | None = None,
    next_cursor: str | None = None,
    truncated: bool = False,
    total: int | None = None,
    partial_errors: Iterable[PartialError | Mapping[str, Any]] = (),
) -> SafePage[Any]:
    """Build a response page without a cursor store (useful for direct reads)."""

    page_limit = normalize_page_size(tool, limit)
    if next_cursor is not None:
        raise SafeViewError(
            "direct pages cannot accept caller-supplied continuation cursors"
        )
    selected: list[Any] = []
    iterator = iter(items)
    for item in iterator:
        if len(selected) >= page_limit:
            truncated = True
            break
        selected.append(item)
    page = SafePage(
        items=tuple(selected),
        as_of=datetime.now(_UTC) if as_of is None else as_of,
        next_cursor=next_cursor,
        truncated=truncated,
        total=total if total is not None else len(selected),
        partial_errors=_sanitize_partial_errors(partial_errors),
    )
    if len(serialize_response(page)) > MAX_RESPONSE_BYTES:
        raise ResponseTooLargeError("serialized response exceeds 256 KiB")
    return page


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_PAGE_SIZES",
    "DEFAULT_SEARCH_PAGE_SIZE",
    "DEFAULT_STATUS_PAGE_SIZE",
    "MAX_PAGE_RESPONSE_BYTES",
    "MAX_PAGE_SIZE",
    "MAX_PAGE_SIZES",
    "MAX_RESPONSE_BYTES",
    "MAX_SEARCH_PAGE_SIZE",
    "MAX_SERIALIZED_RESPONSE_BYTES",
    "MAX_PARTIAL_ERRORS",
    "MAX_SNAPSHOT_BYTES",
    "MAX_SNAPSHOT_ITEMS",
    "MAX_STATUS_PAGE_SIZE",
    "PAGE_BOUNDS",
    "PAGE_SIZE_BOUNDS",
    "RESPONSE_SIZE_LIMIT_BYTES",
    "BoundedPage",
    "BoundedPaginator",
    "InvalidPlexLinkError",
    "LibraryItem",
    "ModelValidationError",
    "OversizedResponseError",
    "PageSizeError",
    "Paginator",
    "RequestStatus",
    "ResponseTooLargeError",
    "SafeLibraryItem",
    "SafePage",
    "SafePlexItem",
    "SafeRequestStatus",
    "SafeServiceHealth",
    "SafeViewError",
    "SafeViewPaginator",
    "ServiceHealth",
    "UnsafeProviderDataError",
    "UnsafeView",
    "binding_hash",
    "bounded_json",
    "bounded_page",
    "bounded_page_size",
    "bounded_paginate",
    "build_plex_link",
    "make_plex_link",
    "normalize_library_item",
    "normalize_media_candidate",
    "normalize_media_status",
    "normalize_page_size",
    "normalize_partial_error",
    "normalize_queue_item",
    "normalize_request_status",
    "normalize_service_health",
    "page_bounds",
    "paginate",
    "paginate_items",
    "response_dict",
    "response_size",
    "sanitize_library_item",
    "sanitize_media_candidate",
    "sanitize_media_status",
    "sanitize_partial_error",
    "sanitize_plex_item",
    "sanitize_queue_item",
    "sanitize_request_status",
    "sanitize_search_result",
    "sanitize_service_health",
    "serialize_record",
    "serialize_response",
    "serialize_safe_record",
    "serialize_safe_response",
    "to_json_bytes",
    "to_safe_dict",
    "validate_page_size",
    "validate_plex_link",
    "validate_plex_url",
    "validated_plex_link",
]
