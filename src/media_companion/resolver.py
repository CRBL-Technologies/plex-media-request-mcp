"""Deterministic Plex identity resolution and tombstone lifecycle helpers.

Plex metadata is not an authorization assertion and titles are not stable
identifiers.  The functions in this module resolve only explicit provider IDs
(``tmdb``, ``tvdb``, and ``imdb``), preserve conflicts for quarantine, and
never fall back to title/year matching.  Tombstone tracking is deliberately
kept in memory here; the application layer can persist the small sanitized
records returned by the tracker in its ledger transaction.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from .errors import ModelValidationError
from .models import MediaIdentity, MediaType, PlexItem
from .plex_ingress import (
    NormalizedPlexEvent,
    canonical_rating_key,
    structured_plex_event_key,
)

MAX_PROVIDER_ID_DIGITS = 18
MAX_IMDB_ID_DIGITS = 12
_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*\Z")
# IMDb's canonical numeric suffix is commonly zero-padded (for example
# ``tt0111161``), so leading zeroes are valid.  Reject the all-zero sentinel
# while retaining a bounded positive identifier.
_IMDB_ID = re.compile(r"tt[0-9]*[1-9][0-9]*\Z", re.IGNORECASE)
_GUID_RE = re.compile(
    r"(?P<provider>tmdb|themoviedb|tvdb|thetvdb|imdb)://(?P<identifier>[^?#/\s]+)",
    re.IGNORECASE,
)
_URL_GUID_RE = re.compile(
    r"(?:^|[:/])(?P<provider>tmdb|themoviedb|tvdb|thetvdb|imdb)[:/]+(?P<identifier>[^?#/\s]+)",
    re.IGNORECASE,
)
_PROVIDER_ALIASES = {
    "tmdb": "tmdb",
    "themoviedb": "tmdb",
    "tvdb": "tvdb",
    "thetvdb": "tvdb",
    "imdb": "imdb",
}
_MEDIA_TYPE_VALUES = {item.value for item in MediaType}


class ResolverError(ValueError):
    """Base class for unusable or conflicting provider metadata."""


class ProviderIdentityError(ResolverError):
    """A provider ID is malformed or cannot be normalized."""


class ProviderConflictError(ResolverError):
    """Two explicit provider IDs conflict and require quarantine."""


class TombstoneError(ResolverError):
    """A tombstone observation is ambiguous or not lifecycle-proven."""


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    CONFLICT = "conflict"
    IDENTITY_WARNING = "identity_warning"


def _provider_name(value: object) -> str:
    if not isinstance(value, str):
        raise ProviderIdentityError("provider_name_invalid")
    normalized = _PROVIDER_ALIASES.get(value.strip().lower())
    if normalized is None:
        raise ProviderIdentityError("provider_name_unsupported")
    return normalized


def _decimal_provider_id(value: object, provider: str) -> int:
    if isinstance(value, bool):
        raise ProviderIdentityError(f"{provider}_id_invalid")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ProviderIdentityError(f"{provider}_id_invalid")
    if len(text) > MAX_PROVIDER_ID_DIGITS or not _POSITIVE_DECIMAL.fullmatch(text):
        raise ProviderIdentityError(f"{provider}_id_invalid")
    try:
        return int(text)
    except ValueError as exc:  # pragma: no cover - regex makes this unreachable
        raise ProviderIdentityError(f"{provider}_id_invalid") from exc


def _imdb_provider_id(value: object) -> str:
    if not isinstance(value, str):
        raise ProviderIdentityError("imdb_id_invalid")
    text = value.strip().lower()
    if len(text) > 2 + MAX_IMDB_ID_DIGITS or not _IMDB_ID.fullmatch(text):
        raise ProviderIdentityError("imdb_id_invalid")
    return text


def normalize_provider_id(provider: str, value: object) -> int | str:
    """Normalize one explicit provider ID and reject alternate forms."""

    name = _provider_name(provider)
    return (
        _decimal_provider_id(value, name)
        if name != "imdb"
        else _imdb_provider_id(value)
    )


@dataclass(frozen=True, slots=True)
class ProviderIds:
    """Normalized provider IDs extracted from one Plex/Sonarr snapshot.

    ``tmdb_ids``/``tvdb_ids`` retain all distinct values so a conflict is not
    accidentally hidden by selecting the first object-map entry.  A resolved
    identity exposes the single value through the convenience properties.
    """

    tmdb_ids: tuple[int, ...] = ()
    tvdb_ids: tuple[int, ...] = ()
    imdb_ids: tuple[str, ...] = ()
    source_guids: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("tmdb_ids", "tvdb_ids"):
            raw_values = tuple(getattr(self, field_name))
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in raw_values
            ):
                raise ProviderIdentityError(f"{field_name}_invalid")
            values = tuple(sorted(set(raw_values)))
            if any(
                value <= 0 or value > 10**MAX_PROVIDER_ID_DIGITS - 1 for value in values
            ):
                raise ProviderIdentityError(f"{field_name}_invalid")
            object.__setattr__(self, field_name, values)
        imdb_values = tuple(
            sorted({_imdb_provider_id(value) for value in self.imdb_ids})
        )
        object.__setattr__(self, "imdb_ids", imdb_values)
        valid_guids: set[str] = set()
        conflicts = set(self.conflicts)
        for value in self.source_guids:
            try:
                valid_guids.add(_bounded_guid(value))
            except ProviderIdentityError:
                # Direct construction is another adapter seam.  Preserve a
                # bounded quarantine reason there too instead of raising on an
                # overlong/malformed source GUID before callers can inspect it.
                conflicts.add("guid_invalid")
        object.__setattr__(self, "source_guids", tuple(sorted(valid_guids)))
        object.__setattr__(self, "conflicts", tuple(sorted(conflicts)))

    @property
    def tmdb_id(self) -> int | None:
        return self.tmdb_ids[0] if len(self.tmdb_ids) == 1 else None

    @property
    def tvdb_id(self) -> int | None:
        return self.tvdb_ids[0] if len(self.tvdb_ids) == 1 else None

    @property
    def imdb_id(self) -> str | None:
        return self.imdb_ids[0] if len(self.imdb_ids) == 1 else None

    @property
    def conflicted(self) -> bool:
        return bool(self.conflicts)

    @property
    def stable(self) -> bool:
        return (
            bool(self.tmdb_ids or self.tvdb_ids or self.imdb_ids)
            and not self.conflicted
        )

    @property
    def canonical_provider(self) -> tuple[str, int | str] | None:
        # TVDB is the canonical show identity in Sonarr; TMDB is canonical for
        # movies.  The caller may use ``canonical_key`` when media kind is not
        # known yet, where the fixed order remains deterministic.
        for provider, values in (
            ("tmdb", self.tmdb_ids),
            ("tvdb", self.tvdb_ids),
            ("imdb", self.imdb_ids),
        ):
            if len(values) == 1:
                return provider, values[0]
        return None

    def canonical_provider_for(
        self, media_type: str | MediaType | None = None
    ) -> tuple[str, int | str] | None:
        """Select the configured canonical provider for a media scope."""

        value = media_type.value if isinstance(media_type, MediaType) else media_type
        order = (
            ("tmdb", self.tmdb_ids),
            ("tvdb", self.tvdb_ids),
            ("imdb", self.imdb_ids),
        )
        if value in {MediaType.SERIES.value, MediaType.EPISODE.value}:
            order = (
                ("tvdb", self.tvdb_ids),
                ("tmdb", self.tmdb_ids),
                ("imdb", self.imdb_ids),
            )
        for provider, values in order:
            if len(values) == 1:
                return provider, values[0]
        return None

    @property
    def key(self) -> str:
        pieces: list[str] = []
        if self.tmdb_ids:
            pieces.append("tmdb=" + ",".join(str(value) for value in self.tmdb_ids))
        if self.tvdb_ids:
            pieces.append("tvdb=" + ",".join(str(value) for value in self.tvdb_ids))
        if self.imdb_ids:
            pieces.append("imdb=" + ",".join(self.imdb_ids))
        return "|".join(pieces) if pieces else "unresolved"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tmdb_id": self.tmdb_id,
            "tvdb_id": self.tvdb_id,
            "imdb_id": self.imdb_id,
            "tmdb_ids": list(self.tmdb_ids),
            "tvdb_ids": list(self.tvdb_ids),
            "imdb_ids": list(self.imdb_ids),
            "conflicts": list(self.conflicts),
        }


ProviderIdentity = ProviderIds


def _bounded_guid(value: object) -> str:
    if not isinstance(value, str):
        raise ProviderIdentityError("guid_invalid")
    text = value.strip()
    if (
        not text
        or len(text) > 1024
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in text)
    ):
        raise ProviderIdentityError("guid_invalid")
    return text


def _iter_guid_values(value: object) -> Iterable[object]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        # Plex's XML/JSON adapters use both ``{id: ...}`` and ``{guid: ...}``
        # shapes.  Only the value is consumed; arbitrary nested data is not
        # retained.  Preserve a present non-string value long enough for the
        # caller to quarantine it as malformed instead of silently dropping a
        # malformed GUID object.
        found = False
        for key in ("id", "guid", "value"):
            if key in value:
                found = True
                yield value[key]
        if not found:
            yield value
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            yield from _iter_guid_values(item)
    elif value is not None:
        yield value


def _parse_guid(value: object) -> tuple[str, int | str] | None:
    try:
        text = _bounded_guid(value)
    except ProviderIdentityError:
        return None
    match = _GUID_RE.search(text) or _URL_GUID_RE.search(text)
    if match is None:
        return None
    try:
        provider = _provider_name(match.group("provider"))
        return provider, normalize_provider_id(provider, match.group("identifier"))
    except ProviderIdentityError:
        return None


def _collect_explicit_ids(metadata: Mapping[str, Any]) -> list[tuple[str, int | str]]:
    found: list[tuple[str, int | str]] = []
    aliases = {
        "tmdb": ("tmdbId", "tmdb_id", "theMovieDbId", "themoviedb_id"),
        "tvdb": ("tvdbId", "tvdb_id", "theTvdbId", "thetvdb_id"),
        "imdb": ("imdbId", "imdb_id"),
    }
    external = metadata.get("externalIds")
    if isinstance(external, Mapping):
        for provider, names in aliases.items():
            for name in (provider, *names):
                if name in external and external[name] is not None:
                    try:
                        found.append(
                            (provider, normalize_provider_id(provider, external[name]))
                        )
                    except ProviderIdentityError:
                        # Malformed explicit IDs are represented as conflicts;
                        # silently dropping them could make a later title match
                        # look safe.
                        found.append((provider, "<invalid>"))
                        break
    for provider, names in aliases.items():
        for name in names:
            if name in metadata and metadata[name] is not None:
                try:
                    found.append(
                        (provider, normalize_provider_id(provider, metadata[name]))
                    )
                except ProviderIdentityError:
                    found.append((provider, "<invalid>"))
                    break
    return found


def extract_provider_ids(
    metadata: Mapping[str, Any],
    *,
    include_parent_guids: bool = True,
) -> ProviderIds:
    """Extract and normalize provider IDs from a bounded metadata mapping."""

    if not isinstance(metadata, Mapping):
        raise ProviderIdentityError("metadata_invalid")
    pairs = _collect_explicit_ids(metadata)
    guid_values: list[object] = []
    for field_name in (
        "guid",
        "guids",
        "Guid",
        "providerGuids",
    ):
        guid_values.extend(_iter_guid_values(metadata.get(field_name)))
    if include_parent_guids:
        for field_name in (
            "parentGuid",
            "grandparentGuid",
            "parentGuids",
            "grandparentGuids",
        ):
            guid_values.extend(_iter_guid_values(metadata.get(field_name)))
    malformed_providers: set[str] = set()
    valid_source_guids: list[str] = []
    for guid in guid_values:
        try:
            bounded = _bounded_guid(guid)
        except ProviderIdentityError:
            # Preserve a safe conflict marker instead of allowing one
            # overlong/malformed provider field to escape as an exception or
            # silently disappear from the quarantine surface.
            malformed_providers.add("guid_invalid")
            continue
        valid_source_guids.append(bounded)
        parsed = _parse_guid(bounded)
        if parsed is not None:
            pairs.append(parsed)
        else:
            # A recognizable provider scheme with a non-canonical identifier
            # is evidence of an identity conflict, not permission to fall
            # back to title/year matching.  Unknown provider GUIDs remain
            # irrelevant to this resolver.
            hint = _GUID_RE.search(bounded) or _URL_GUID_RE.search(bounded)
            if hint is not None:
                try:
                    malformed_providers.add(_provider_name(hint.group("provider")))
                except ProviderIdentityError:
                    pass

    by_provider: dict[str, set[int | str]] = {
        "tmdb": set(),
        "tvdb": set(),
        "imdb": set(),
    }
    conflicts: set[str] = set()
    conflicts.update(malformed_providers)
    for provider, value in pairs:
        if value == "<invalid>":
            conflicts.add(provider)
            continue
        by_provider[provider].add(value)
    for provider, values in by_provider.items():
        if len(values) > 1:
            conflicts.add(provider)
    return ProviderIds(
        tmdb_ids=tuple(
            value for value in by_provider["tmdb"] if isinstance(value, int)
        ),
        tvdb_ids=tuple(
            value for value in by_provider["tvdb"] if isinstance(value, int)
        ),
        imdb_ids=tuple(
            value for value in by_provider["imdb"] if isinstance(value, str)
        ),
        source_guids=tuple(valid_source_guids),
        conflicts=tuple(conflicts),
    )


provider_ids_from_metadata = extract_provider_ids
parse_provider_ids = extract_provider_ids


def parse_provider_guid(guid: str) -> tuple[str, int | str] | None:
    """Parse one Plex provider GUID without title/year inference."""

    return _parse_guid(guid)


@dataclass(frozen=True, slots=True)
class ProviderCrosswalk:
    """Verified request-time provider identity crosswalk."""

    media_type: str
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None
    verified: bool = True
    source: str = "request"
    version: int = 1

    def __post_init__(self) -> None:
        media_type = (
            self.media_type.value
            if isinstance(self.media_type, MediaType)
            else self.media_type
        )
        if not isinstance(media_type, str) or media_type not in _MEDIA_TYPE_VALUES:
            raise ProviderIdentityError("media_type_invalid")
        object.__setattr__(self, "media_type", media_type)
        for field_name, provider in (("tmdb_id", "tmdb"), ("tvdb_id", "tvdb")):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self, field_name, _decimal_provider_id(value, provider)
                )
        if self.imdb_id is not None:
            object.__setattr__(self, "imdb_id", _imdb_provider_id(self.imdb_id))
        if not isinstance(self.verified, bool):
            raise ProviderIdentityError("verified_invalid")
        if (
            not isinstance(self.source, str)
            or not self.source.strip()
            or len(self.source) > 128
        ):
            raise ProviderIdentityError("crosswalk_source_invalid")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ProviderIdentityError("crosswalk_version_invalid")

    @classmethod
    def from_provider_ids(
        cls,
        media_type: str | MediaType,
        ids: ProviderIds,
        *,
        verified: bool = True,
        source: str = "plex",
        version: int = 1,
    ) -> ProviderCrosswalk:
        if ids.conflicted:
            raise ProviderConflictError("provider_ids_conflict")
        return cls(
            media_type=media_type.value
            if isinstance(media_type, MediaType)
            else media_type,
            tmdb_id=ids.tmdb_id,
            tvdb_id=ids.tvdb_id,
            imdb_id=ids.imdb_id,
            verified=verified,
            source=source,
            version=version,
        )

    @property
    def ids(self) -> ProviderIds:
        return ProviderIds(
            tmdb_ids=() if self.tmdb_id is None else (self.tmdb_id,),
            tvdb_ids=() if self.tvdb_id is None else (self.tvdb_id,),
            imdb_ids=() if self.imdb_id is None else (self.imdb_id,),
        )

    @property
    def key(self) -> str:
        return f"{self.media_type}:{self.ids.key}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "media_type": self.media_type,
            "tmdb_id": self.tmdb_id,
            "tvdb_id": self.tvdb_id,
            "imdb_id": self.imdb_id,
            "verified": self.verified,
            "source": self.source,
            "version": self.version,
        }


Crosswalk = ProviderCrosswalk


@dataclass(frozen=True, slots=True)
class ProviderResolution:
    """A safe resolution result, including an explicit warning/quarantine reason."""

    status: ResolutionStatus
    ids: ProviderIds
    identity: ProviderCrosswalk | None = None
    matched_providers: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED and self.identity is not None

    @property
    def quarantined(self) -> bool:
        return self.status is not ResolutionStatus.RESOLVED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "ids": self.ids.as_dict(),
            "identity": None if self.identity is None else self.identity.as_dict(),
            "matched_providers": list(self.matched_providers),
            "reason": self.reason,
        }


ResolvedIdentity = ProviderResolution


def _coerce_crosswalk(
    value: ProviderCrosswalk | ProviderIds | Mapping[str, Any],
    *,
    media_type: str | None = None,
) -> ProviderCrosswalk:
    if isinstance(value, ProviderCrosswalk):
        return value
    if isinstance(value, ProviderIds):
        if media_type is None:
            raise ProviderIdentityError("crosswalk_media_type_required")
        return ProviderCrosswalk.from_provider_ids(media_type, value, source="request")
    if isinstance(value, Mapping):
        return ProviderCrosswalk(
            media_type=value.get("media_type", value.get("type", "movie")),
            tmdb_id=value.get("tmdb_id", value.get("tmdbId")),
            tvdb_id=value.get("tvdb_id", value.get("tvdbId")),
            imdb_id=value.get("imdb_id", value.get("imdbId")),
            verified=value.get("verified", True),
            source=value.get("source", "request"),
            version=value.get("version", 1),
        )
    raise ProviderIdentityError("crosswalk_invalid")


def resolve_provider_identity(
    metadata: Mapping[str, Any] | NormalizedPlexEvent | ProviderIds,
    *,
    media_type: str | MediaType | None = None,
    expected: ProviderCrosswalk | ProviderIds | Mapping[str, Any] | None = None,
    verified: bool = True,
) -> ProviderResolution:
    """Resolve explicit provider IDs, never by title or year.

    When ``expected`` is supplied, at least one non-conflicting verified ID
    must intersect and every provider present on both sides must agree.  A
    conflict or unresolved crosswalk returns a warning result rather than
    collapsing two libraries by display title.
    """

    if isinstance(metadata, NormalizedPlexEvent):
        metadata_mapping: Mapping[str, Any] = metadata.sanitized_dict()
        inferred_type = metadata.media_type
    elif isinstance(metadata, ProviderIds):
        ids = metadata
        inferred_type = None
        metadata_mapping = {}
    elif isinstance(metadata, Mapping):
        metadata_mapping = metadata
        inferred_type = metadata.get("media_type", metadata.get("type"))
        ids = extract_provider_ids(metadata)
    else:
        raise ProviderIdentityError("metadata_invalid")
    if isinstance(metadata, ProviderIds):
        ids = metadata
    elif "ids" not in locals():
        ids = extract_provider_ids(metadata_mapping)
    type_value = media_type or inferred_type or "movie"
    if isinstance(type_value, MediaType):
        type_value = type_value.value
    if not isinstance(type_value, str) or type_value not in _MEDIA_TYPE_VALUES:
        raise ProviderIdentityError("media_type_invalid")
    if ids.conflicted:
        return ProviderResolution(
            ResolutionStatus.CONFLICT,
            ids,
            reason="conflicting_provider_ids",
        )
    if not ids.stable:
        return ProviderResolution(
            ResolutionStatus.UNRESOLVED, ids, reason="provider_ids_missing"
        )
    try:
        observed = ProviderCrosswalk.from_provider_ids(
            type_value, ids, verified=verified, source="plex"
        )
    except ProviderConflictError:
        return ProviderResolution(
            ResolutionStatus.CONFLICT, ids, reason="conflicting_provider_ids"
        )
    if expected is None:
        return ProviderResolution(
            ResolutionStatus.RESOLVED
            if verified
            else ResolutionStatus.IDENTITY_WARNING,
            ids,
            identity=observed if verified else None,
            reason=None if verified else "provider_ids_unverified",
        )
    try:
        target = _coerce_crosswalk(expected, media_type=type_value)
    except ProviderIdentityError:
        return ProviderResolution(
            ResolutionStatus.IDENTITY_WARNING, ids, reason="crosswalk_unresolved"
        )
    if not target.verified or target.media_type != type_value:
        return ProviderResolution(
            ResolutionStatus.IDENTITY_WARNING, ids, reason="crosswalk_unverified"
        )
    target_ids = target.ids
    if target_ids.conflicted or ids.conflicted:
        return ProviderResolution(
            ResolutionStatus.CONFLICT, ids, reason="conflicting_provider_ids"
        )
    observed_values = {
        "tmdb": ids.tmdb_id,
        "tvdb": ids.tvdb_id,
        "imdb": ids.imdb_id,
    }
    target_values = {
        "tmdb": target.tmdb_id,
        "tvdb": target.tvdb_id,
        "imdb": target.imdb_id,
    }
    matched: list[str] = []
    for provider in ("tmdb", "tvdb", "imdb"):
        observed_value = observed_values[provider]
        target_value = target_values[provider]
        if observed_value is not None and target_value is not None:
            if observed_value != target_value:
                return ProviderResolution(
                    ResolutionStatus.CONFLICT,
                    ids,
                    reason=f"crosswalk_{provider}_mismatch",
                )
            matched.append(provider)
    if not matched:
        return ProviderResolution(
            ResolutionStatus.UNRESOLVED, ids, reason="provider_ids_do_not_match"
        )
    return ProviderResolution(
        ResolutionStatus.RESOLVED,
        ids,
        identity=target,
        matched_providers=tuple(matched),
    )


resolve_provider_ids = resolve_provider_identity
resolve_identity = resolve_provider_identity


def provider_identity_key(
    media_type: str | MediaType,
    ids: ProviderIds | ProviderCrosswalk,
) -> str:
    """Build the deterministic logical identity key used by planners."""

    media_value = (
        media_type.value if isinstance(media_type, MediaType) else str(media_type)
    )
    if isinstance(ids, ProviderCrosswalk):
        return ids.key
    if ids.conflicted or not ids.stable:
        return f"{media_value}:unresolved:{ids.key}"
    return f"{media_value}:{ids.key}"


canonical_identity_key = provider_identity_key


def provider_identity_from_event(
    event: NormalizedPlexEvent,
    *,
    expected: ProviderCrosswalk | Mapping[str, Any] | None = None,
) -> ProviderResolution:
    """Resolve the GUID fields from an ingress event without title matching."""

    metadata = event.sanitized_dict()
    return resolve_provider_identity(
        metadata, media_type=event.media_type, expected=expected
    )


def plex_item_from_metadata(
    metadata: Mapping[str, Any] | NormalizedPlexEvent,
    *,
    server_uuid: str | None = None,
    library_uuid: str | None = None,
    library_name: str | None = None,
    machine_identifier: str | None = None,
    plex_url: str | None = None,
    added_at: datetime | None = None,
    expected_identity: ProviderCrosswalk | Mapping[str, Any] | None = None,
) -> tuple[PlexItem, ProviderResolution]:
    """Build a safe :class:`PlexItem` from a verified metadata snapshot.

    This helper intentionally returns an identity warning alongside the item.
    An admin Plex-identity event can still be planned when the requester
    crosswalk is unresolved; the caller must not turn a warning into a title
    match.
    """

    source: Mapping[str, Any]
    media_type: str
    if isinstance(metadata, NormalizedPlexEvent):
        event = metadata
        source = event.sanitized_dict()
        media_type = event.media_type
    elif isinstance(metadata, Mapping):
        source = metadata
        raw_media_type = source.get("media_type", source.get("type"))
        if isinstance(raw_media_type, MediaType):
            media_type = raw_media_type.value
        elif isinstance(raw_media_type, str):
            media_type = raw_media_type
        else:
            raise ResolverError("media_type_missing")
        if media_type not in {MediaType.MOVIE.value, MediaType.EPISODE.value}:
            raise ResolverError("plex_item_type_invalid")
    else:
        raise ResolverError("metadata_invalid")
    event_server = source.get("server_uuid", source.get("serverUuid"))
    event_library = source.get(
        "library_uuid", source.get("libraryUuid", source.get("library_key"))
    )
    normalized_server = server_uuid or event_server
    normalized_library = library_uuid or event_library
    if not isinstance(normalized_server, str) or not normalized_server.strip():
        raise ResolverError("server_uuid_missing")
    if not isinstance(normalized_library, str) or not normalized_library.strip():
        raise ResolverError("library_uuid_missing")
    rating = canonical_rating_key(source.get("rating_key", source.get("ratingKey")))
    title = source.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ResolverError("title_missing")
    year = source.get("year")
    if year is not None and (
        isinstance(year, bool) or not isinstance(year, int) or not 1800 <= year <= 3000
    ):
        raise ResolverError("year_invalid")
    season = source.get(
        "season_number", source.get("seasonNumber", source.get("parentIndex"))
    )
    episode = source.get(
        "episode_number", source.get("episodeNumber", source.get("index"))
    )
    if media_type == MediaType.EPISODE.value:
        if isinstance(season, bool) or not isinstance(season, int) or season < 0:
            raise ResolverError("season_number_invalid")
        if isinstance(episode, bool) or not isinstance(episode, int) or episode < 0:
            raise ResolverError("episode_number_invalid")
    else:
        season = None
        episode = None
    if plex_url is not None and (
        not isinstance(plex_url, str)
        or not plex_url.strip()
        or any(ord(ch) < 0x20 for ch in plex_url)
    ):
        raise ResolverError("plex_url_invalid")
    if added_at is not None and added_at.tzinfo is None:
        raise ResolverError("added_at_invalid")
    resolution = resolve_provider_identity(
        source,
        media_type=media_type,
        expected=expected_identity,
    )
    identity: MediaIdentity | None = None
    if resolution.identity is not None:
        resolved_ids = resolution.identity.ids
        canonical = resolved_ids.canonical_provider_for(media_type)
        identity = MediaIdentity(
            MediaType(media_type),
            tmdb_id=resolved_ids.tmdb_id,
            tvdb_id=resolved_ids.tvdb_id,
            imdb_id=resolved_ids.imdb_id,
            provider_id=None if canonical is None else str(canonical[1]),
        )
    try:
        item = PlexItem(
            rating_key=rating,
            media_type=MediaType(media_type),
            title=title,
            year=year,
            library_key=normalized_library,
            library_name=library_name
            or source.get("library_name", source.get("librarySectionTitle")),
            show_title=source.get("show_title", source.get("grandparentTitle")),
            season_number=season,
            episode_number=episode,
            quality=source.get("quality"),
            plex_url=plex_url,
            added_at=added_at,
            machine_identifier=machine_identifier or source.get("machine_identifier"),
            provider_identity=identity,
        )
    except (ModelValidationError, ValueError, TypeError) as exc:
        raise ResolverError("plex_snapshot_invalid") from exc
    return item, resolution


build_plex_item = plex_item_from_metadata
resolve_plex_metadata = plex_item_from_metadata


def _aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise TombstoneError(f"{field_name}_timezone_missing")
    return value.astimezone(timezone.utc)


def _mapping_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value, "timestamp")
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise TombstoneError("timestamp_invalid") from exc
        return _aware(parsed, "timestamp")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TombstoneError("timestamp_invalid")
    try:
        if not math.isfinite(float(value)):
            raise ValueError
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise TombstoneError("timestamp_invalid") from exc


@dataclass(frozen=True, slots=True)
class TombstoneGeneration:
    """One Plex storage lifecycle for a rating key."""

    server_uuid: str
    library_uuid: str
    rating_key: str
    generation: int = 0
    added_at: datetime | None = None
    first_seen_at: datetime | None = None
    deleted_at: datetime | None = None
    lifecycle_status: Literal["active", "tombstone", "quarantined"] = "active"
    provider_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.server_uuid, str) or not self.server_uuid.strip():
            raise TombstoneError("server_uuid_missing")
        if not isinstance(self.library_uuid, str) or not self.library_uuid.strip():
            raise TombstoneError("library_uuid_missing")
        object.__setattr__(self, "rating_key", canonical_rating_key(self.rating_key))
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise TombstoneError("generation_invalid")
        object.__setattr__(self, "added_at", _aware(self.added_at, "added_at"))
        object.__setattr__(
            self, "first_seen_at", _aware(self.first_seen_at, "first_seen_at")
        )
        object.__setattr__(self, "deleted_at", _aware(self.deleted_at, "deleted_at"))
        if self.lifecycle_status not in {"active", "tombstone", "quarantined"}:
            raise TombstoneError("lifecycle_status_invalid")
        if self.provider_key is not None and (
            not isinstance(self.provider_key, str) or len(self.provider_key) > 512
        ):
            raise TombstoneError("provider_key_invalid")

    @property
    def storage_key(self) -> str:
        return structured_plex_event_key(
            self.server_uuid,
            self.library_uuid,
            self.rating_key,
            self.generation,
        )

    @property
    def is_tombstone(self) -> bool:
        return self.lifecycle_status == "tombstone"

    def as_dict(self) -> dict[str, Any]:
        return {
            "server_uuid": self.server_uuid,
            "library_uuid": self.library_uuid,
            "rating_key": self.rating_key,
            "tombstone_generation": self.generation,
            "added_at": _iso(self.added_at),
            "first_seen_at": _iso(self.first_seen_at),
            "deleted_at": _iso(self.deleted_at),
            "lifecycle_status": self.lifecycle_status,
            "provider_key": self.provider_key,
        }


Tombstone = TombstoneGeneration


@dataclass(frozen=True, slots=True)
class TombstoneDecision:
    generation: TombstoneGeneration
    action: Literal["new", "existing", "tombstone", "quarantine"]
    is_new_lifecycle: bool = False
    reason: str | None = None

    @property
    def quarantined(self) -> bool:
        return self.action == "quarantine"

    @property
    def event_key(self) -> str:
        return self.generation.storage_key


TombstoneObservation = TombstoneDecision


def _coerce_observation(
    item: PlexItem | NormalizedPlexEvent | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(item, PlexItem):
        return {
            "server_uuid": None,
            "library_uuid": item.library_key,
            "rating_key": item.rating_key,
            "added_at": item.added_at,
            "provider_key": item.provider_identity.key
            if item.provider_identity
            else None,
        }
    if isinstance(item, NormalizedPlexEvent):
        return {
            "server_uuid": item.server_uuid,
            "library_uuid": item.library_uuid,
            "rating_key": item.rating_key,
            "added_at": item.added_at,
            "provider_key": None,
        }
    if isinstance(item, Mapping):
        return {
            "server_uuid": item.get("server_uuid", item.get("serverUuid")),
            "library_uuid": item.get(
                "library_uuid", item.get("libraryUuid", item.get("library_key"))
            ),
            "rating_key": item.get("rating_key", item.get("ratingKey")),
            "added_at": item.get("added_at", item.get("addedAt")),
            "provider_key": item.get("provider_key"),
        }
    raise TombstoneError("observation_invalid")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


class TombstoneTracker:
    """Track rating-key generations without guessing through ambiguous time."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], TombstoneGeneration] = {}

    def current(
        self,
        server_uuid: str,
        library_uuid: str,
        rating_key: str,
    ) -> TombstoneGeneration | None:
        try:
            canonical = canonical_rating_key(rating_key)
        except ValueError as exc:
            raise TombstoneError("rating_key_invalid") from exc
        return self._records.get((server_uuid, library_uuid, canonical))

    def observe(
        self,
        item: PlexItem | NormalizedPlexEvent | Mapping[str, Any],
        *,
        server_uuid: str | None = None,
        library_uuid: str | None = None,
        observed_at: datetime | None = None,
        verified: bool = True,
    ) -> TombstoneDecision:
        observation = _coerce_observation(item)
        server = server_uuid or observation["server_uuid"]
        library = library_uuid or observation["library_uuid"]
        if not isinstance(server, str) or not server.strip():
            raise TombstoneError("server_uuid_missing")
        if not isinstance(library, str) or not library.strip():
            raise TombstoneError("library_uuid_missing")
        try:
            rating = canonical_rating_key(observation["rating_key"])
        except ValueError as exc:
            raise TombstoneError("rating_key_invalid") from exc
        if not isinstance(verified, bool) or not verified:
            existing = self._records.get((server, library, rating))
            if existing is None:
                generation = TombstoneGeneration(
                    server, library, rating, lifecycle_status="quarantined"
                )
            else:
                generation = TombstoneGeneration(
                    existing.server_uuid,
                    existing.library_uuid,
                    existing.rating_key,
                    existing.generation,
                    existing.added_at,
                    existing.first_seen_at,
                    existing.deleted_at,
                    "quarantined",
                    existing.provider_key,
                )
            self._records[(server, library, rating)] = generation
            return TombstoneDecision(
                generation, "quarantine", reason="snapshot_unverified"
            )
        added = _mapping_timestamp(observation["added_at"])
        seen = _aware(observed_at, "observed_at") or datetime.now(timezone.utc)
        key = (server, library, rating)
        existing = self._records.get(key)
        if existing is None:
            generation = TombstoneGeneration(
                server,
                library,
                rating,
                generation=0,
                added_at=added,
                first_seen_at=seen,
                provider_key=observation["provider_key"],
            )
            self._records[key] = generation
            return TombstoneDecision(generation, "new", is_new_lifecycle=True)
        if existing.lifecycle_status == "tombstone":
            if existing.added_at is None or added is None:
                quarantine = TombstoneGeneration(
                    existing.server_uuid,
                    existing.library_uuid,
                    existing.rating_key,
                    existing.generation,
                    existing.added_at,
                    existing.first_seen_at,
                    existing.deleted_at,
                    "quarantined",
                    existing.provider_key,
                )
                self._records[key] = quarantine
                return TombstoneDecision(
                    quarantine, "quarantine", reason="rating_key_reuse_time_ambiguous"
                )
            if added <= existing.added_at:
                quarantine = TombstoneGeneration(
                    existing.server_uuid,
                    existing.library_uuid,
                    existing.rating_key,
                    existing.generation,
                    existing.added_at,
                    existing.first_seen_at,
                    existing.deleted_at,
                    "quarantined",
                    existing.provider_key,
                )
                self._records[key] = quarantine
                return TombstoneDecision(
                    quarantine, "quarantine", reason="rating_key_reuse_not_later"
                )
            generation = TombstoneGeneration(
                server,
                library,
                rating,
                generation=existing.generation + 1,
                added_at=added,
                first_seen_at=seen,
                provider_key=observation["provider_key"],
            )
            self._records[key] = generation
            return TombstoneDecision(
                generation, "new", is_new_lifecycle=True, reason="rating_key_reused"
            )
        if existing.lifecycle_status == "quarantined":
            return TombstoneDecision(
                existing, "quarantine", reason="lifecycle_quarantined"
            )
        # Active metadata changes, rescan, quality upgrades, and duplicate
        # representations remain one Plex lifecycle.  A later addedAt alone
        # cannot prove a deletion/re-add without an intervening tombstone.
        return TombstoneDecision(existing, "existing", is_new_lifecycle=False)

    def mark_deleted(
        self,
        server_uuid: str,
        library_uuid: str,
        rating_key: str,
        *,
        deleted_at: datetime | None = None,
        generation: int | None = None,
        confirmed: bool = True,
    ) -> TombstoneDecision:
        """Create a tombstone only after bounded retry/reconciliation evidence."""

        if not confirmed:
            current = self.current(server_uuid, library_uuid, rating_key)
            if current is None:
                raise TombstoneError("item_not_found")
            return TombstoneDecision(
                current, "existing", reason="deletion_not_confirmed"
            )
        try:
            canonical = canonical_rating_key(rating_key)
        except ValueError as exc:
            raise TombstoneError("rating_key_invalid") from exc
        key = (server_uuid, library_uuid, canonical)
        existing = self._records.get(key)
        if existing is None:
            raise TombstoneError("item_not_found")
        if generation is not None and generation != existing.generation:
            return TombstoneDecision(existing, "existing", reason="stale_generation")
        if existing.lifecycle_status == "tombstone":
            return TombstoneDecision(existing, "tombstone", reason="already_tombstoned")
        deleted = _aware(deleted_at, "deleted_at") or datetime.now(timezone.utc)
        tombstone = TombstoneGeneration(
            existing.server_uuid,
            existing.library_uuid,
            existing.rating_key,
            existing.generation,
            existing.added_at,
            existing.first_seen_at,
            deleted,
            "tombstone",
            existing.provider_key,
        )
        self._records[key] = tombstone
        return TombstoneDecision(tombstone, "tombstone")

    def records(self) -> tuple[TombstoneGeneration, ...]:
        return tuple(self._records[key] for key in sorted(self._records))


TombstoneRegistry = TombstoneTracker
GenerationTracker = TombstoneTracker


__all__ = [
    "MAX_IMDB_ID_DIGITS",
    "MAX_PROVIDER_ID_DIGITS",
    "Crosswalk",
    "GenerationTracker",
    "ProviderConflictError",
    "ProviderCrosswalk",
    "ProviderIdentity",
    "ProviderIdentityError",
    "ProviderIds",
    "ProviderResolution",
    "ResolutionStatus",
    "ResolvedIdentity",
    "ResolverError",
    "Tombstone",
    "TombstoneDecision",
    "TombstoneError",
    "TombstoneGeneration",
    "TombstoneObservation",
    "TombstoneRegistry",
    "TombstoneTracker",
    "build_plex_item",
    "canonical_identity_key",
    "extract_provider_ids",
    "normalize_provider_id",
    "parse_provider_guid",
    "parse_provider_ids",
    "plex_item_from_metadata",
    "provider_identity_from_event",
    "provider_identity_key",
    "provider_ids_from_metadata",
    "resolve_identity",
    "resolve_plex_metadata",
    "resolve_provider_identity",
    "resolve_provider_ids",
]
