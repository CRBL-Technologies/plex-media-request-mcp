"""Plex-authoritative, configured-origin client.

Plex is the availability authority for the companion.  This client only builds
metadata paths from canonical decimal rating keys and only constructs Plex Web
links from the configured machine identifier.  A token is sent as a header and
is never accepted in a URL or returned in a normalized record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Callable, Iterator, cast
import xml.etree.ElementTree as ET

import requests

from ..config import SecretFileRef, ServiceEndpoint, TimeoutConfig
from ..models import (
    MediaCandidate,
    MediaIdentity,
    MediaStatus,
    MediaType,
    Page,
    PlexItem,
    canonical_rating_key,
)
from ..safe_views import _ValidatedPlexLink, build_plex_link
from .radarr import (
    AdapterCircuitOpenError,
    AdapterConfigurationError,
    AdapterError,
    AdapterHTTPError,
    AdapterResponseError,
    AdapterTimeoutError,
    AdapterTransportError,
    ConfiguredHTTPTransport,
    HTTPResponse,
    HttpTransport,
    MAX_PROVIDER_RESPONSE_BYTES,
    SecretReader,
    _ConfiguredHTTPTransport,
    _endpoint_url,
    _nonnegative_int,
    _positive_int,
    _response_body,
    _response_json,
    _response_status,
    _secret_value,
    _text,
)


MAX_LIBRARY_ITEMS = 5_000
MAX_SEARCH_RESULTS = 100
MAX_POSTER_BYTES = 8 * 1024 * 1024
PLEX_WEB_ORIGIN = "https://app.plex.tv/desktop"
PLEX_WEB_LINK_ORIGIN = "https://app.plex.tv"
MAX_PLEX_GUID_CHARS = 1_024
_IMAGE_SIGNATURES: tuple[tuple[str, bytes, bytes | None], ...] = (
    ("image/jpeg", b"\xff\xd8\xff", None),
    ("image/png", b"\x89PNG\r\n\x1a\n", None),
    ("image/gif", b"GIF8", None),
    ("image/webp", b"RIFF", b"WEBP"),
)


def image_mime_type(body: bytes, content_type: object = None) -> str | None:
    """Return a MIME only when both the bytes and declared type are safe."""

    if not isinstance(body, bytes) or not body:
        return None
    declared = (
        str(content_type).split(";", 1)[0].strip().lower() if content_type else None
    )
    detected: str | None = None
    for mime, prefix, suffix in _IMAGE_SIGNATURES:
        if body.startswith(prefix) and (suffix is None or body[8:12] == suffix):
            detected = mime
            break
    if detected is None:
        return None
    if declared is not None and declared != detected:
        return None
    return detected


@dataclass(frozen=True, slots=True)
class PlexLibrary:
    """Configured/allowlisted Plex library metadata."""

    key: str
    title: str
    media_type: str
    uuid: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _library_key(self.key))
        title = _text(self.title, fallback=None, max_bytes=256)
        if title is None:
            raise ValueError("library title is invalid")
        object.__setattr__(self, "title", title)
        kind = (
            self.media_type.value
            if isinstance(self.media_type, MediaType)
            else self.media_type
        )
        if kind not in {"movie", "series"}:
            raise ValueError("library media_type is invalid")
        object.__setattr__(self, "media_type", kind)
        object.__setattr__(self, "uuid", _text(self.uuid, max_bytes=128))


@dataclass(frozen=True, slots=True)
class PlexSnapshotEvidence:
    """Bounded, path-free evidence from one verified Plex metadata snapshot.

    Plex's ``Media[].Part[].file`` values are useful as an availability proof,
    but are filesystem paths and must never cross the adapter boundary.  Keep
    only counts, so a caller can distinguish a title-only/webhook hint from a
    re-fetched playable snapshot without retaining provider paths.
    """

    media_count: int
    part_count: int
    file_count: int

    def __post_init__(self) -> None:
        for field_name in ("media_count", "part_count", "file_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.part_count > 0 and self.media_count == 0:
            raise ValueError("parts require media evidence")
        if self.file_count > self.part_count:
            raise ValueError("file evidence cannot exceed part evidence")

    @property
    def verified(self) -> bool:
        """Whether the response had a bounded Media/Part shape."""

        return self.media_count > 0 and self.part_count > 0

    @property
    def playable(self) -> bool:
        """Whether at least one Part carried a non-empty file marker."""

        return self.verified and self.file_count > 0


@dataclass(frozen=True, slots=True)
class PlexMetadata:
    """Typed metadata plus explicit verified/playable snapshot evidence."""

    item: PlexItem
    provider_identity: MediaIdentity | None = None
    snapshot: PlexSnapshotEvidence | None = None
    snapshot_verified: bool = False
    playable: bool = False

    def __post_init__(self) -> None:
        if self.provider_identity is not None and not isinstance(
            self.provider_identity, MediaIdentity
        ):
            raise ValueError("provider_identity must be MediaIdentity")
        if not isinstance(self.snapshot_verified, bool) or not isinstance(
            self.playable, bool
        ):
            raise ValueError("snapshot evidence flags must be boolean")
        if self.snapshot is None:
            if self.snapshot_verified or self.playable:
                raise ValueError("snapshot flags require snapshot evidence")
            return
        if not isinstance(self.snapshot, PlexSnapshotEvidence):
            raise ValueError("snapshot must be PlexSnapshotEvidence")
        if self.snapshot_verified != self.snapshot.verified:
            raise ValueError("snapshot_verified does not match snapshot evidence")
        if self.playable != self.snapshot.playable:
            raise ValueError("playable does not match snapshot evidence")


def _library_key(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("library key is invalid")
    if not all(character.isalnum() or character in "_-" for character in value):
        raise ValueError("library key is invalid")
    return value


def _library_identifier(value: object) -> str | None:
    """Normalize Plex's numeric section ID or stable section UUID."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _library_key(value.strip())
    except ValueError:
        return None


def _first_non_none(value: Mapping[str, object], *field_names: str) -> object:
    for field_name in field_names:
        candidate = value.get(field_name)
        if candidate is not None:
            return candidate
    return None


def _provider_identity(
    value: Mapping[str, object], media_type: MediaType
) -> MediaIdentity | None:
    tmdb_values: set[int] = set()
    tvdb_values: set[int] = set()
    imdb_values: set[str] = set()
    guids = value.get("Guid", value.get("guid", value.get("guids")))
    values: list[object] = []
    if isinstance(guids, Sequence) and not isinstance(guids, (str, bytes, bytearray)):
        values.extend(guids)
    elif guids is not None:
        values.append(guids)
    for guid in values:
        raw: object = guid.get("id") if isinstance(guid, Mapping) else guid
        if not isinstance(raw, str):
            if isinstance(guid, Mapping) and raw is not None:
                return None
            continue
        if len(raw) > MAX_PLEX_GUID_CHARS or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in raw
        ):
            return None
        lower = raw.lower()
        if lower.startswith("tmdb://"):
            identifier = lower[7:]
            if (
                not identifier.isdigit()
                or not 0 < len(identifier) <= 18
                or int(identifier) <= 0
            ):
                return None
            tmdb_values.add(int(identifier))
        elif lower.startswith("tvdb://"):
            identifier = lower[7:]
            if (
                not identifier.isdigit()
                or not 0 < len(identifier) <= 18
                or int(identifier) <= 0
            ):
                return None
            tvdb_values.add(int(identifier))
        elif lower.startswith("imdb://"):
            identifier = lower[7:]
            if (
                not re.fullmatch(r"tt[0-9]+", identifier, re.IGNORECASE)
                or len(identifier) > 14
            ):
                return None
            imdb_values.add(identifier)
        elif lower.startswith("tmdb-") and lower[5:].isdigit():
            identifier = lower[5:]
            if not 0 < len(identifier) <= 18 or int(identifier) <= 0:
                return None
            tmdb_values.add(int(identifier))
        elif lower.startswith("tvdb-") and lower[5:].isdigit():
            identifier = lower[5:]
            if not 0 < len(identifier) <= 18 or int(identifier) <= 0:
                return None
            tvdb_values.add(int(identifier))
    # Direct fields are cross-checked with GUIDs.  Multiple values in one
    # namespace are ambiguous and must not become a requester match.
    raw_tmdb = value.get("tmdbId")
    raw_tvdb = value.get("tvdbId")
    direct_tmdb = _plex_id(raw_tmdb, "tmdb_id")
    direct_tvdb = _plex_id(raw_tvdb, "tvdb_id")
    if (raw_tmdb is not None and direct_tmdb is None) or (
        raw_tvdb is not None and direct_tvdb is None
    ):
        return None
    direct_imdb = value.get("imdbId")
    if direct_imdb is not None and (
        not isinstance(direct_imdb, str)
        or not re.fullmatch(r"tt[0-9]+", direct_imdb, re.IGNORECASE)
        or len(direct_imdb) > 14
    ):
        return None
    if direct_tmdb is not None:
        tmdb_values.add(direct_tmdb)
    if direct_tvdb is not None:
        tvdb_values.add(direct_tvdb)
    if isinstance(direct_imdb, str) and direct_imdb.lower().startswith("tt"):
        imdb_values.add(direct_imdb.lower())
    if len(tmdb_values) > 1 or len(tvdb_values) > 1 or len(imdb_values) > 1:
        return None
    tmdb = next(iter(tmdb_values), None)
    tvdb = next(iter(tvdb_values), None)
    imdb = next(iter(imdb_values), None)
    if not any((tmdb, tvdb, imdb)):
        return None
    # ``provider_id`` is reserved for records that have no namespaced stable
    # identifier.  A Plex rating key or Arr internal id is never substituted.
    return MediaIdentity(media_type, tmdb_id=tmdb, tvdb_id=tvdb, imdb_id=imdb)


def _identity_conflicted(value: Mapping[str, object]) -> bool:
    """Whether one Plex record contains contradictory same-namespace IDs."""

    return _provider_identity(
        value,
        _media_type(value.get("type", value.get("media_type"))) or MediaType.MOVIE,
    ) is None and bool(
        value.get("Guid", value.get("guid", value.get("guids")))
        or value.get("tmdbId") is not None
        or value.get("tvdbId") is not None
        or value.get("imdbId") is not None
    )


def _plex_id(value: object, field_name: str) -> int | None:
    if isinstance(value, str) and value.isdigit():
        if len(value) > 18:
            return None
        value = int(value)
    elif isinstance(value, int) and len(str(value)) > 18:
        return None
    return _positive_int(value, field_name, optional=True)


def _media_type(value: object) -> MediaType | None:
    if value in {"movie", "Movie"}:
        return MediaType.MOVIE
    if value in {"show", "Show", "series", "Series", "tv", "TV"}:
        return MediaType.SERIES
    if value in {"episode", "Episode"}:
        return MediaType.EPISODE
    return None


def _candidate_from_show(
    value: Mapping[str, object], *, machine_identifier: str | None
) -> MediaCandidate | None:
    title = _text(value.get("title"), fallback="Series") or "Series"
    year = _positive_int(value.get("year"), "year", optional=True)
    identity = _provider_identity(value, MediaType.SERIES)
    if identity is None:
        return None
    provider = (
        identity.tvdb_id or identity.tmdb_id or identity.imdb_id or identity.provider_id
    )
    if provider is None:
        return None
    return MediaCandidate(
        MediaType.SERIES,
        provider,
        title,
        year,
        _text(value.get("summary"), max_bytes=2048),
        identity,
    )


def _mapping_children(
    value: Mapping[str, object], *field_names: str
) -> tuple[Mapping[str, object], ...] | None:
    """Read one bounded Plex child collection without retaining raw objects."""

    for field_name in field_names:
        if field_name not in value:
            continue
        raw = value[field_name]
        if isinstance(raw, Mapping):
            return (cast(Mapping[str, object], raw),)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            children = tuple(
                cast(Mapping[str, object], item)
                for item in raw
                if isinstance(item, Mapping)
            )
            # A malformed child collection is not a partial proof.  Do not
            # silently discard a non-mapping object and call the snapshot
            # verified merely because another child happened to be valid.
            if len(children) != len(raw):
                return None
            return children
        return None
    return None


def _plex_snapshot_evidence(value: Mapping[str, object]) -> PlexSnapshotEvidence | None:
    """Extract path-free Media/Part/file evidence from a Plex response."""

    media = _mapping_children(value, "Media", "media")
    if not media:
        return None
    part_count = 0
    file_count = 0
    for media_value in media:
        parts = _mapping_children(media_value, "Part", "part", "parts")
        if not parts:
            continue
        part_count += len(parts)
        for part in parts:
            raw_file = part.get("file", part.get("filePath", part.get("file_path")))
            if not isinstance(raw_file, str):
                continue
            file_marker = raw_file.strip()
            if (
                file_marker
                and len(file_marker.encode("utf-8", "ignore")) <= 4_096
                and not any(
                    ord(character) < 0x20 or ord(character) == 0x7F
                    for character in file_marker
                )
            ):
                # The marker proves that Plex has a backing file.  Its value
                # is intentionally discarded so paths never enter the model.
                file_count += 1
    evidence = PlexSnapshotEvidence(len(media), part_count, file_count)
    return evidence if evidence.verified else None


def _metadata_from_mapping(
    value: Mapping[str, object],
    *,
    library: PlexLibrary | None,
    machine_identifier: str | None,
    web_link: Callable[[str], str | None],
) -> PlexMetadata | None:
    rating_raw = _first_non_none(value, "ratingKey", "rating_key")
    if not isinstance(rating_raw, str):
        if isinstance(rating_raw, int) and rating_raw > 0:
            rating_raw = str(rating_raw)
        else:
            return None
    try:
        rating_key = canonical_rating_key(rating_raw)
    except ValueError:
        return None
    kind = _media_type(_first_non_none(value, "type", "media_type"))
    if kind is None:
        return None
    snapshot = _plex_snapshot_evidence(value)
    if snapshot is None or not snapshot.playable:
        # A webhook/search envelope is not proof that the final Plex media is
        # playable.  Only a re-fetched Media/Part/file snapshot can become a
        # visible normalized item.
        return None
    title = _text(value.get("title"), fallback="Untitled") or "Untitled"
    year = _positive_int(value.get("year"), "year", optional=True)
    show_title = _text(value.get("grandparentTitle"), max_bytes=512)
    season = _nonnegative_int(
        value.get("parentIndex", value.get("seasonNumber")),
        "season_number",
        optional=True,
    )
    episode = _positive_int(
        value.get("index", value.get("episodeNumber")), "episode_number", optional=True
    )
    if kind is MediaType.EPISODE and (season is None or episode is None):
        return None
    if _identity_conflicted(value):
        # The caller can quarantine the source record; it must not be
        # converted into an unverified requester match.
        return None
    added_at: datetime | None = None
    raw_added = value.get("addedAt", value.get("added_at"))
    if (
        isinstance(raw_added, (int, float))
        and not isinstance(raw_added, bool)
        and math.isfinite(float(raw_added))
        and raw_added >= 0
    ):
        try:
            added_at = datetime.fromtimestamp(float(raw_added), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            added_at = None
    quality = _text(value.get("quality"), max_bytes=128)
    if quality is None:
        media_values = _mapping_children(value, "Media", "media")
        if media_values:
            quality = _text(media_values[0].get("videoResolution"), max_bytes=128)
    identity = _provider_identity(value, kind)
    plex_link = web_link(rating_key)
    item = PlexItem(
        rating_key,
        kind,
        title,
        year,
        library_key=library.key
        if library
        else _text(value.get("libraryKey"), max_bytes=128),
        library_name=library.title
        if library
        else _text(value.get("libraryName"), max_bytes=256),
        show_title=show_title,
        season_number=season,
        episode_number=episode,
        quality=quality,
        plex_url=plex_link,
        added_at=added_at,
        machine_identifier=machine_identifier,
        provider_identity=identity,
    )
    # ``PlexItem`` normalizes strings for model safety, which would otherwise
    # erase the process-local trusted-link marker required by safe views.
    if isinstance(plex_link, _ValidatedPlexLink):
        object.__setattr__(item, "plex_url", plex_link)
    return PlexMetadata(
        item,
        identity,
        snapshot,
        snapshot_verified=snapshot.verified,
        playable=snapshot.playable,
    )


def _extract_items(value: object) -> list[Mapping[str, object]]:
    if isinstance(value, Mapping):
        for container_key in ("MediaContainer", "mediaContainer", "container"):
            container = value.get(container_key)
            if isinstance(container, Mapping):
                return _extract_items(container)
        for key in ("Metadata", "metadata", "items", "records"):
            candidate = value.get(key)
            if isinstance(candidate, Sequence) and not isinstance(
                candidate, (str, bytes, bytearray)
            ):
                return [item for item in candidate if isinstance(item, Mapping)]
            # Plex's XML representation collapses a single child element to a
            # mapping (for example one ``<Metadata .../>``).  Treat that as a
            # one-item collection just like the JSON array form.
            if isinstance(candidate, Mapping):
                return [cast(Mapping[str, object], candidate)]
        for key in ("Directory", "directory", "Hub", "hub"):
            candidate = value.get(key)
            if isinstance(candidate, Sequence) and not isinstance(
                candidate, (str, bytes, bytearray)
            ):
                return [item for item in candidate if isinstance(item, Mapping)]
            if isinstance(candidate, Mapping):
                return [cast(Mapping[str, object], candidate)]
        if value.get("ratingKey") is not None:
            return [cast(Mapping[str, object], value)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _pagination_container(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    for container_key in ("MediaContainer", "mediaContainer", "container"):
        container = value.get(container_key)
        if isinstance(container, Mapping):
            return cast(Mapping[str, object], container)
    return cast(Mapping[str, object], value)


def _pagination_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise AdapterResponseError(f"Plex pagination {field_name} is invalid")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise AdapterResponseError(f"Plex pagination {field_name} is invalid")
    if parsed < 0:
        raise AdapterResponseError(f"Plex pagination {field_name} is invalid")
    return parsed


def _xml_to_mapping(body: bytes) -> Mapping[str, object] | None:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None

    def node(element: ET.Element) -> dict[str, object]:
        result: dict[str, object] = dict(element.attrib)
        children: dict[str, list[object]] = {}
        for child in element:
            children.setdefault(child.tag, []).append(node(child))
        for key, values in children.items():
            result[key] = values[0] if len(values) == 1 else values
        return result

    return node(root)


class PlexClient:
    """Read-only/refresh Plex API client with internal poster support."""

    service_name = "plex"

    def __init__(
        self,
        endpoint: ServiceEndpoint | str | None = None,
        token: str | SecretFileRef | Path | None = None,
        *,
        config: object | None = None,
        secret_reader: SecretReader | Callable[[object], str] | None = None,
        transport: HttpTransport | None = None,
        timeouts: TimeoutConfig | None = None,
        server_uuid: str | None = None,
        machine_identifier: str | None = None,
        allowed_library_keys: Sequence[str] = (),
        allowed_library_names: Sequence[str] = (),
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = _endpoint_url(endpoint, name="plex", config=config)
        self.token = _secret_value(
            token, config=config, field_name="plex_token", reader=secret_reader
        )
        self.server_uuid = _clean_identifier(
            server_uuid or getattr(config, "plex_server_uuid", None), "server_uuid"
        )
        self.machine_identifier = _clean_identifier(
            machine_identifier or getattr(config, "plex_machine_identifier", None),
            "machine_identifier",
        )
        names = allowed_library_names or getattr(config, "plex_library_names", ()) or ()
        keys = allowed_library_keys or getattr(config, "plex_library_keys", ()) or ()
        self.allowed_library_names = frozenset(
            value.strip() for value in names if isinstance(value, str) and value.strip()
        )
        self.allowed_library_keys = frozenset(_library_key(value) for value in keys)
        configured_timeouts = timeouts or getattr(config, "timeouts", None)
        self.transport: HttpTransport = transport or _ConfiguredHTTPTransport(
            timeouts=configured_timeouts,
            session=session,
            allowed_origin=self.base_url,
            allowed_addresses=getattr(config, "plex_allowed_addresses", ())
            if config is not None
            else (),
            allow_private_addresses=True,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        max_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
        parse_json: bool = True,
    ) -> tuple[HTTPResponse, object]:
        if not path.startswith("/") or "?" in path or "#" in path or ".." in path:
            raise ValueError("Plex path must be a fixed API path")
        response = self.transport.request(
            method,
            self.base_url + path,
            headers={
                "X-Plex-Token": self.token,
                "Accept": "application/json, application/xml;q=0.9",
                "X-Plex-Product": "CRBL Media Companion",
            },
            params=params,
            max_bytes=max_bytes,
        )
        body = _response_body(response)
        if len(body) > max_bytes:
            raise AdapterResponseError("Plex response exceeds the bounded body limit")
        status_code = _response_status(response)
        if status_code < 200:
            raise AdapterHTTPError(
                self.service_name, status_code, "Plex returned an invalid HTTP status"
            )
        if 300 <= status_code < 400:
            raise AdapterHTTPError(
                self.service_name, status_code, "redirects are disabled"
            )
        if status_code >= 400:
            raise AdapterHTTPError(
                self.service_name, status_code, "Plex request failed"
            )
        if not parse_json:
            return response, None
        try:
            payload = _response_json(response)
        except AdapterResponseError:
            payload = _xml_to_mapping(body)
            if payload is None and body:
                raise AdapterResponseError("Plex returned invalid metadata")
        return response, payload

    def _web_link(self, rating_key: str) -> str | None:
        if self.machine_identifier is None:
            return None
        try:
            # ``build_plex_link`` returns the trusted marker consumed by the
            # safe-view boundary.  The public Plex Web origin is deliberate;
            # the API token remains a header-only credential.
            return build_plex_link(
                PLEX_WEB_LINK_ORIGIN,
                self.machine_identifier,
                canonical_rating_key(rating_key),
            )
        except (TypeError, ValueError):
            # Invalid configured machine identifiers fail closed by omitting a
            # link rather than returning an unvalidated provider URL.
            return None

    def libraries(self) -> tuple[PlexLibrary, ...]:
        self._require_scope()
        _, payload = self._request("GET", "/library/sections")
        values = _extract_items(payload)
        result: list[PlexLibrary] = []
        for value in values:
            key = value.get("key")
            title = _text(value.get("title"), fallback="Library", max_bytes=256)
            if not isinstance(key, str) or title is None:
                continue
            try:
                normalized_key = _library_key(key)
            except ValueError:
                continue
            kind = str(value.get("type") or "unknown").lower()
            if kind not in {"movie", "show", "tv", "series"}:
                continue
            library = PlexLibrary(
                normalized_key,
                title,
                "movie" if kind == "movie" else "series",
                _text(value.get("uuid"), max_bytes=128),
            )
            if self._library_allowed(library):
                result.append(library)
        return tuple(result)

    get_libraries = libraries

    def _library_allowed(self, library: PlexLibrary) -> bool:
        if not self.allowed_library_keys and not self.allowed_library_names:
            return False
        stable_ids = {library.key}
        if library.uuid is not None:
            stable_ids.add(library.uuid)
        if self.allowed_library_keys and not stable_ids.intersection(
            self.allowed_library_keys
        ):
            return False
        if (
            self.allowed_library_names
            and library.title not in self.allowed_library_names
        ):
            return False
        return True

    def _require_scope(self) -> None:
        """Fail closed until the configured Plex authority is explicit."""

        if not self.server_uuid:
            raise AdapterConfigurationError("Plex server UUID is not configured")
        if not self.allowed_library_keys and not self.allowed_library_names:
            raise AdapterConfigurationError("Plex library allowlist is not configured")

    def _library_for_value(
        self, value: Mapping[str, object], media_type: MediaType
    ) -> PlexLibrary | None:
        raw_key = _first_non_none(
            value, "libraryKey", "library_key", "librarySectionID", "librarySectionId"
        )
        raw_uuid = _first_non_none(
            value, "libraryUuid", "libraryUUID", "librarySectionUUID"
        )
        key = _library_identifier(raw_key) or _library_identifier(raw_uuid)
        name = _text(
            _first_non_none(
                value,
                "libraryName",
                "library_name",
                "librarySectionTitle",
                "librarySectionName",
            ),
            max_bytes=256,
        )
        # A matching mutable title is never enough to authorize an item.  Real
        # Plex metadata must carry a stable section ID/UUID; do not synthesize
        # ``unknown`` and accidentally let it satisfy a name-only policy.
        if key is None:
            return None
        if name is None and self.allowed_library_names:
            return None
        if name is None:
            name = key
        try:
            library = PlexLibrary(
                key,
                name,
                "movie" if media_type is MediaType.MOVIE else "series",
                _text(raw_uuid, max_bytes=128),
            )
        except (ValueError, TypeError):
            return None
        return library if self._library_allowed(library) else None

    def _checked_metadata(self, value: Mapping[str, object]) -> PlexMetadata | None:
        raw_server = _first_non_none(value, "serverUUID", "server_uuid", "serverUuid")
        if raw_server is not None and (
            not isinstance(raw_server, str) or raw_server.strip() != self.server_uuid
        ):
            return None
        kind = _media_type(value.get("type", value.get("media_type")))
        if kind is None:
            return None
        library = self._library_for_value(value, kind)
        if library is None:
            return None
        return _metadata_from_mapping(
            value,
            library=library,
            machine_identifier=self.machine_identifier,
            web_link=self._web_link,
        )

    def _library_items_page(
        self,
        library: PlexLibrary | str,
        *,
        media_type: MediaType | str | None,
        limit: int,
        offset: int,
    ) -> tuple[Page[PlexItem], int, int | None, tuple[str, ...]]:
        self._require_scope()
        library_obj = (
            library
            if isinstance(library, PlexLibrary)
            else next((item for item in self.libraries() if item.key == library), None)
        )
        if library_obj is None:
            raise AdapterResponseError("Plex library is not configured")
        if not self._library_allowed(library_obj):
            raise AdapterConfigurationError(
                "Plex library is outside the configured allowlist"
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SEARCH_RESULTS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= MAX_LIBRARY_ITEMS
        ):
            raise ValueError(f"offset must be between 0 and {MAX_LIBRARY_ITEMS}")
        params: dict[str, object] = {
            "includeGuids": 1,
            "X-Plex-Container-Start": offset,
            "X-Plex-Container-Size": limit,
        }
        kind = media_type.value if isinstance(media_type, MediaType) else media_type
        if kind not in {None, "movie", "series", "episode"}:
            raise ValueError("media_type must be movie, series, episode, or omitted")
        if kind in {"movie", "episode"}:
            params["type"] = 1 if kind == "movie" else 4
        _, payload = self._request(
            "GET",
            f"/library/sections/{_library_key(library_obj.key)}/all",
            params=params,
        )
        raw_values = _extract_items(payload)
        if len(raw_values) > limit:
            raise AdapterResponseError("Plex pagination returned too many items")
        container = _pagination_container(payload)
        reported_offset = _pagination_int(
            container.get("offset", container.get("containerStart"))
            if container is not None
            else None,
            "offset",
        )
        if reported_offset is not None and reported_offset != offset:
            raise AdapterResponseError("Plex pagination offset changed")
        reported_size = _pagination_int(
            container.get("size", container.get("containerSize"))
            if container is not None
            else None,
            "size",
        )
        if reported_size is not None and reported_size > limit:
            raise AdapterResponseError("Plex pagination size changed")
        reported_total = None
        if container is not None:
            for field_name in ("totalSize", "total", "totalCount"):
                if field_name in container:
                    reported_total = _pagination_int(container.get(field_name), "total")
                    break
        if reported_total is not None and reported_total < offset + len(raw_values):
            raise AdapterResponseError("Plex pagination total is inconsistent")
        raw_rating_keys: list[str] = []
        for value in raw_values:
            raw_rating = value.get("ratingKey", value.get("rating_key"))
            if (
                isinstance(raw_rating, int)
                and not isinstance(raw_rating, bool)
                and raw_rating > 0
            ):
                raw_rating = str(raw_rating)
            try:
                raw_rating_keys.append(canonical_rating_key(raw_rating))
            except (TypeError, ValueError) as exc:
                raise AdapterResponseError("Plex item rating key is invalid") from exc
        if len(set(raw_rating_keys)) != len(raw_rating_keys):
            raise AdapterResponseError("Plex pagination repeated an item")
        items: list[PlexItem] = []
        for value in raw_values:
            kind_for_value = _media_type(value.get("type", value.get("media_type")))
            has_library_scope = any(
                value.get(field) is not None
                for field in (
                    "libraryKey",
                    "library_key",
                    "librarySectionID",
                    "librarySectionId",
                    "libraryUuid",
                    "libraryUUID",
                    "librarySectionUUID",
                    "libraryName",
                    "library_name",
                    "librarySectionTitle",
                    "librarySectionName",
                )
            )
            if has_library_scope and (
                kind_for_value is None
                or self._library_for_value(value, kind_for_value) is None
            ):
                continue
            raw_server = _first_non_none(
                value, "serverUUID", "server_uuid", "serverUuid"
            )
            if raw_server is not None and (
                not isinstance(raw_server, str)
                or raw_server.strip() != self.server_uuid
            ):
                continue
            metadata = _metadata_from_mapping(
                value,
                library=library_obj,
                machine_identifier=self.machine_identifier,
                web_link=self._web_link,
            )
            if metadata is not None and (
                kind is None
                or metadata.item.media_type.value == kind
                or (kind == "series" and metadata.item.media_type is MediaType.EPISODE)
            ):
                items.append(metadata.item)
        truncated = (
            offset + len(raw_values) < reported_total
            if reported_total is not None
            else len(raw_values) == limit
        )
        total = (
            reported_total
            if reported_total is not None
            else (len(items) if not truncated else None)
        )
        return (
            Page(items=tuple(items), total=total, truncated=truncated),
            len(raw_values),
            reported_total,
            tuple(raw_rating_keys),
        )

    def library_items(
        self,
        library: PlexLibrary | str,
        *,
        media_type: MediaType | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Page[PlexItem]:
        """Fetch one integrity-checked Plex container page."""

        page, _raw_count, _total, _raw_rating_keys = self._library_items_page(
            library,
            media_type=media_type,
            limit=limit,
            offset=offset,
        )
        return page

    def iter_library_items(
        self,
        library: PlexLibrary | str,
        *,
        media_type: MediaType | str | None = None,
        page_size: int = MAX_SEARCH_RESULTS,
    ) -> Iterator[PlexItem]:
        """Yield one complete, bounded library traversal or fail closed."""

        offset = 0
        seen: set[str] = set()
        while True:
            page, raw_count, total, raw_rating_keys = self._library_items_page(
                library,
                media_type=media_type,
                limit=page_size,
                offset=offset,
            )
            if total is not None and total > MAX_LIBRARY_ITEMS:
                raise AdapterResponseError("Plex library exceeds the bounded sweep")
            if raw_count == 0:
                if total is not None and offset < total:
                    raise AdapterResponseError("Plex pagination ended before total")
                return
            if seen.intersection(raw_rating_keys):
                raise AdapterResponseError("Plex pagination repeated an item")
            seen.update(raw_rating_keys)
            for item in page.items:
                yield item
            offset += raw_count
            if offset > MAX_LIBRARY_ITEMS:
                raise AdapterResponseError("Plex library exceeds the bounded sweep")
            if total is not None:
                if offset >= total:
                    return
            elif raw_count < page_size:
                return
            if not page.truncated:
                return

    get_library_items = library_items

    def search(
        self, query: str, *, media_type: MediaType | str = "any", limit: int = 25
    ) -> Page[MediaCandidate]:
        self._require_scope()
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be blank")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SEARCH_RESULTS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
        kind = media_type.value if isinstance(media_type, MediaType) else media_type
        if kind not in {"any", "movie", "series"}:
            raise ValueError("media_type must be movie, series, or any")
        _, payload = self._request(
            "GET",
            "/hubs/search",
            params={"query": query.strip(), "limit": min(limit, MAX_SEARCH_RESULTS)},
        )
        candidates: list[MediaCandidate] = []
        hubs = _extract_items(payload)
        for hub in hubs:
            hub_type = str(hub.get("type") or "").lower()
            allowed_hub_types = {kind}
            if kind == "series":
                allowed_hub_types.update({"show", "tv"})
            if kind != "any" and hub_type not in allowed_hub_types:
                continue
            for value in _extract_items(hub):
                if str(value.get("type") or "").lower() in {"show", "series", "tv"}:
                    if self._library_for_value(value, MediaType.SERIES) is None:
                        continue
                    candidate = _candidate_from_show(
                        value, machine_identifier=self.machine_identifier
                    )
                    if candidate is not None:
                        candidates.append(candidate)
                    if len(candidates) >= limit:
                        break
                    continue
                metadata = self._checked_metadata(value)
                if metadata is None or metadata.provider_identity is None:
                    continue
                identity = metadata.provider_identity
                provider = (
                    identity.tmdb_id
                    or identity.tvdb_id
                    or identity.imdb_id
                    or identity.provider_id
                )
                if provider is None:
                    continue
                candidates.append(
                    MediaCandidate(
                        metadata.item.media_type,
                        provider,
                        metadata.item.title,
                        metadata.item.year,
                        identity=identity,
                    )
                )
                if len(candidates) >= limit:
                    break
            if len(candidates) >= limit:
                break
        return Page(
            items=tuple(candidates),
            total=len(candidates),
            truncated=len(candidates) >= limit,
        )

    plex_search = search

    def get_metadata(self, rating_key: str) -> PlexMetadata:
        self._require_scope()
        rating_key = canonical_rating_key(rating_key)
        _, payload = self._request(
            "GET", f"/library/metadata/{rating_key}", params={"includeGuids": 1}
        )
        values = _extract_items(payload)
        if not values and isinstance(payload, Mapping):
            values = [payload]
        for value in values:
            metadata = self._checked_metadata(value)
            if metadata is not None and metadata.item.rating_key == rating_key:
                return metadata
        raise AdapterResponseError("Plex metadata did not contain a supported item")

    metadata = get_metadata
    get_item = get_metadata

    def find_by_identity(
        self, identity: MediaIdentity, *, limit: int = 100
    ) -> Page[MediaCandidate]:
        self._require_scope()
        # Plex search is intentionally used only through its fixed query path;
        # identity strings are not interpolated into a path or URL.
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SEARCH_RESULTS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
        query = str(
            identity.tmdb_id
            or identity.tvdb_id
            or identity.imdb_id
            or identity.provider_id
            or ""
        )
        if not query:
            return Page()
        kind = "movie" if identity.media_type is MediaType.MOVIE else "series"
        results = self.search(
            query, media_type=kind, limit=min(limit, MAX_SEARCH_RESULTS)
        )
        return Page(
            items=tuple(
                item
                for item in results.items
                if item.identity and _identity_intersects(item.identity, identity)
            ),
            total=results.total,
            truncated=results.truncated,
        )

    def find_metadata_by_identity(
        self, identity: MediaIdentity, *, limit: int = 100
    ) -> Page[PlexMetadata]:
        """Return verified Plex metadata records matching one provider identity.

        This is kept separate from the safe search-candidate surface so status
        reconciliation can retain Plex-authoritative quality and Web-link
        fields without exposing the provider's raw search envelope.
        """

        self._require_scope()
        query = str(
            identity.tmdb_id
            or identity.tvdb_id
            or identity.imdb_id
            or identity.provider_id
            or ""
        )
        if not query:
            return Page()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SEARCH_RESULTS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
        kind = "movie" if identity.media_type is MediaType.MOVIE else "series"
        _, payload = self._request(
            "GET",
            "/hubs/search",
            params={"query": query, "limit": min(limit, MAX_SEARCH_RESULTS)},
        )
        found: list[PlexMetadata] = []
        for hub in _extract_items(payload):
            hub_type = str(hub.get("type") or "").lower()
            allowed_hub_types = {kind}
            if kind == "series":
                allowed_hub_types.update({"show", "tv"})
            if hub_type and hub_type not in allowed_hub_types:
                continue
            for value in _extract_items(hub):
                metadata = self._checked_metadata(value)
                if metadata is None or metadata.provider_identity is None:
                    continue
                if _identity_intersects(metadata.provider_identity, identity):
                    found.append(metadata)
                    if len(found) >= limit:
                        return Page(
                            items=tuple(found), total=len(found), truncated=True
                        )
        return Page(items=tuple(found), total=len(found), truncated=False)

    def status_for_identity(self, identity: MediaIdentity) -> MediaStatus:
        self._require_scope()
        metadata = self.find_metadata_by_identity(identity, limit=1)
        if metadata.items:
            item = metadata.items[0].item
            return MediaStatus(
                identity,
                True,
                item.title,
                item.year,
                item.quality,
                item.plex_url,
                datetime.now(timezone.utc),
            )
        # Search/hub results are discovery hints, not proof of playable Plex
        # visibility.  In particular, a show hub can contain a title and GUID
        # without a verified ``Media[].Part[].file`` snapshot.  Do not fall
        # back to that candidate and accidentally report availability.
        return MediaStatus(
            identity,
            False,
            None,
            None,
            None,
            None,
            datetime.now(timezone.utc),
        )

    media_status = status_for_identity

    def refresh_library(self, library: PlexLibrary | str) -> bool:
        library_obj = (
            library
            if isinstance(library, PlexLibrary)
            else next((item for item in self.libraries() if item.key == library), None)
        )
        if library_obj is None or not self._library_allowed(library_obj):
            raise AdapterConfigurationError(
                "Plex library is outside the configured allowlist"
            )
        self._request(
            "GET", f"/library/sections/{_library_key(library_obj.key)}/refresh"
        )
        return True

    def poster_bytes(
        self, rating_key: str, *, max_bytes: int = MAX_POSTER_BYTES
    ) -> bytes:
        self._require_scope()
        rating_key = canonical_rating_key(rating_key)
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= MAX_POSTER_BYTES
        ):
            raise ValueError("max_bytes is invalid")
        response, _ = self._request(
            "GET",
            f"/library/metadata/{rating_key}/thumb",
            max_bytes=max_bytes,
            parse_json=False,
        )
        body = _response_body(response)
        if len(body) > max_bytes:
            raise AdapterResponseError("Plex poster exceeds the bounded body limit")
        if _response_status(response) != 200 or not body:
            raise AdapterResponseError("Plex poster is unavailable")
        if image_mime_type(body, response.headers.get("content-type")) is None:
            raise AdapterResponseError("Plex poster has an invalid image type")
        return body


def _clean_identifier(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    result = value.strip()
    if (
        not result
        or len(result) > 256
        or any(ord(character) < 0x20 for character in result)
    ):
        raise ValueError(f"{field_name} is invalid")
    return result


def _identity_intersects(left: MediaIdentity, right: MediaIdentity) -> bool:
    if left.media_type is not right.media_type:
        return False
    matched = False
    for field_name in ("tmdb_id", "tvdb_id", "imdb_id", "provider_id"):
        first = getattr(left, field_name)
        second = getattr(right, field_name)
        if first is None or second is None:
            continue
        if str(first).casefold() != str(second).casefold():
            # A match in one provider namespace cannot override a conflicting
            # explicit value in another shared namespace.
            return False
        matched = True
    return matched


__all__ = [
    "AdapterCircuitOpenError",
    "AdapterConfigurationError",
    "AdapterError",
    "AdapterHTTPError",
    "AdapterResponseError",
    "AdapterTimeoutError",
    "AdapterTransportError",
    "ConfiguredHTTPTransport",
    "HTTPResponse",
    "HttpTransport",
    "MAX_POSTER_BYTES",
    "image_mime_type",
    "PlexClient",
    "PlexLibrary",
    "PlexMetadata",
    "PlexSnapshotEvidence",
]
