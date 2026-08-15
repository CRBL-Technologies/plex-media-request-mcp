"""Typed Sonarr adapter for durable request reconciliation.

Only fixed v3 endpoints are exposed.  In particular, season searching is a
season-level command: ``search_seasons`` emits exactly one ``SeasonSearch``
command for each requested season and never fans a request out to episodes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Callable

import requests

from ..config import SecretFileRef, ServiceEndpoint, TimeoutConfig
from ..models import (
    MediaCandidate,
    MediaIdentity,
    MediaType,
    Page,
    QueueItem,
    QueueState,
    ServiceName,
)
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
    _eta_seconds,
    _json_response,
    _nonnegative_int,
    _positive_int,
    _progress,
    _records,
    _response_body,
    _secret_value,
    _text,
    _typed_mapping,
)


MAX_REQUESTED_SEASONS = 50
MAX_SEARCH_RESULTS = 100
DEFAULT_QUEUE_PAGE_SIZE = 250
MAX_QUEUE_PAGE_SIZE = 250
MAX_EPISODES = 10_000


@dataclass(frozen=True, slots=True)
class SonarrDefaults:
    """Server-owned normal/anime Sonarr policy."""

    normal_quality_profile_id: int | None = None
    normal_quality_profile_name: str | None = None
    anime_quality_profile_id: int | None = None
    anime_quality_profile_name: str | None = None
    root_folder_path: str | None = None
    tag_ids: tuple[int, ...] = ()
    monitored: bool = True
    season_folder: bool = True
    search_for_missing_episodes: bool = True

    def __post_init__(self) -> None:
        for name in ("normal_quality_profile_id", "anime_quality_profile_id"):
            value = getattr(self, name)
            if value is not None:
                _positive_int(value, name)
        if self.root_folder_path is not None and (
            not isinstance(self.root_folder_path, str)
            or not self.root_folder_path.startswith("/")
            or any(ord(character) < 0x20 for character in self.root_folder_path)
            or "?" in self.root_folder_path
            or "#" in self.root_folder_path
            or ".." in self.root_folder_path.split("/")
        ):
            raise ValueError("root_folder_path must be an absolute configured path")
        tags: list[int] = []
        for tag in self.tag_ids:
            parsed = _positive_int(tag, "tag_id")
            assert parsed is not None
            tags.append(parsed)
        object.__setattr__(self, "tag_ids", tuple(dict.fromkeys(tags)))
        for name in ("monitored", "season_folder", "search_for_missing_episodes"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")

    def profile_id(self, anime: bool) -> int | None:
        return (
            self.anime_quality_profile_id if anime else self.normal_quality_profile_id
        )

    def profile_name(self, anime: bool) -> str | None:
        return (
            self.anime_quality_profile_name
            if anime
            else self.normal_quality_profile_name
        )


@dataclass(frozen=True, slots=True)
class SonarrEpisode:
    """Allowlisted episode truth used for per-season completion."""

    id: int | None
    season_number: int
    episode_number: int
    title: str
    has_file: bool = False
    air_date: str | None = None
    monitored: bool = False
    series_id: int | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class SonarrSeasonAvailability:
    season_number: int
    total_episodes: int
    available_episodes: int
    missing_episodes: int
    future_episodes: int = 0

    @property
    def complete(self) -> bool:
        return (
            self.total_episodes > 0
            and self.missing_episodes == 0
            and self.future_episodes == 0
        )


@dataclass(frozen=True, slots=True)
class SonarrSeries:
    id: int | None
    tvdb_id: int | None
    tmdb_id: int | None
    imdb_id: str | None
    title: str
    year: int | None = None
    overview: str | None = None
    monitored: bool = False
    seasons: tuple[int, ...] = ()
    monitored_seasons: tuple[int, ...] = ()
    status: str | None = None
    quality_profile_id: int | None = None
    root_folder_path: str | None = None
    season_folder: bool | None = None
    tags: tuple[int, ...] = ()
    series_type: str | None = None
    language_profile_id: int | None = None
    season_statuses: tuple[tuple[int, str], ...] = ()

    @property
    def provider_id(self) -> int:
        if self.tvdb_id is None:
            raise AdapterResponseError("Sonarr series has no TVDB identity")
        return self.tvdb_id

    @property
    def identity(self) -> MediaIdentity:
        return MediaIdentity(
            MediaType.SERIES,
            tmdb_id=self.tmdb_id,
            tvdb_id=self.tvdb_id,
            imdb_id=self.imdb_id,
            # Sonarr's internal id is not a stable provider identity.
            provider_id=None,
        )


@dataclass(frozen=True, slots=True)
class SonarrQueueRecord:
    item: QueueItem
    provider_id: int | None = None


def _normalize_seasons(seasons: Sequence[int]) -> tuple[int, ...]:
    if isinstance(seasons, (str, bytes, bytearray)) or not isinstance(
        seasons, Sequence
    ):
        raise ValueError("seasons must contain integers")
    values = tuple(seasons)
    if not values:
        raise ValueError("seasons must be an explicit non-empty list")
    result: set[int] = set()
    for season in values:
        if isinstance(season, bool) or not isinstance(season, int) or season < 0:
            raise ValueError(
                "seasons must contain non-negative integers; use 0 for specials"
            )
        result.add(season)
    if len(result) > MAX_REQUESTED_SEASONS:
        raise ValueError(
            f"seasons cannot contain more than {MAX_REQUESTED_SEASONS} values"
        )
    return tuple(sorted(result))


def _series_from_mapping(value: Mapping[str, object]) -> SonarrSeries:
    series_id = _positive_int(value.get("id"), "id", optional=True)
    tvdb_id = _positive_int(value.get("tvdbId"), "tvdbId", optional=True)
    tmdb_id = _positive_int(value.get("tmdbId"), "tmdbId", optional=True)
    imdb_id = _text(value.get("imdbId"), max_bytes=32)
    title = _text(value.get("title"), fallback="Series") or "Series"
    year = _positive_int(value.get("year"), "year", optional=True)
    overview = _text(value.get("overview"), max_bytes=2048)
    seasons: list[int] = []
    raw_seasons = value.get("seasons")
    if isinstance(raw_seasons, Sequence) and not isinstance(
        raw_seasons, (str, bytes, bytearray)
    ):
        for season in raw_seasons:
            if isinstance(season, Mapping):
                parsed = _nonnegative_int(
                    season.get("seasonNumber"), "seasonNumber", optional=True
                )
                if parsed is not None:
                    seasons.append(parsed)
    monitored_seasons: list[int] = []
    season_statuses: list[tuple[int, str]] = []
    if isinstance(raw_seasons, Sequence) and not isinstance(
        raw_seasons, (str, bytes, bytearray)
    ):
        for season in raw_seasons:
            if isinstance(season, Mapping) and season.get("monitored") is True:
                parsed = _nonnegative_int(
                    season.get("seasonNumber"), "seasonNumber", optional=True
                )
                if parsed is not None:
                    monitored_seasons.append(parsed)
            if isinstance(season, Mapping):
                parsed = _nonnegative_int(
                    season.get("seasonNumber"), "seasonNumber", optional=True
                )
                status = _text(
                    season.get("status", season.get("seasonStatus")), max_bytes=64
                )
                if parsed is not None and status is not None:
                    season_statuses.append((parsed, status))
    tags: list[int] = []
    raw_tags = value.get("tags")
    if isinstance(raw_tags, Sequence) and not isinstance(
        raw_tags, (str, bytes, bytearray)
    ):
        for tag in raw_tags:
            parsed = _positive_int(tag, "tag", optional=True)
            if parsed is not None:
                tags.append(parsed)
    root_folder_path = _text(value.get("rootFolderPath"), max_bytes=1024)
    if root_folder_path is not None and not root_folder_path.startswith("/"):
        root_folder_path = None
    raw_season_folder = value.get("seasonFolder")
    season_folder = raw_season_folder if isinstance(raw_season_folder, bool) else None
    return SonarrSeries(
        series_id,
        tvdb_id,
        tmdb_id,
        imdb_id,
        title,
        year,
        overview,
        value.get("monitored") is True,
        tuple(sorted(set(seasons))),
        tuple(sorted(set(monitored_seasons))),
        _text(value.get("status"), max_bytes=64),
        _positive_int(value.get("qualityProfileId"), "qualityProfileId", optional=True),
        root_folder_path,
        season_folder,
        tuple(sorted(set(tags))),
        _text(value.get("seriesType"), max_bytes=64),
        _positive_int(
            value.get("languageProfileId"), "languageProfileId", optional=True
        ),
        tuple(sorted(set(season_statuses))),
    )


def _episode_from_mapping(value: Mapping[str, object]) -> SonarrEpisode | None:
    season = _nonnegative_int(value.get("seasonNumber"), "seasonNumber", optional=True)
    episode = _positive_int(value.get("episodeNumber"), "episodeNumber", optional=True)
    if season is None or episode is None:
        return None
    episode_file = value.get("episodeFile")
    has_file = (
        value.get("hasFile") is True
        or _positive_int(value.get("episodeFileId"), "episodeFileId", optional=True)
        is not None
        or (
            isinstance(episode_file, Mapping)
            and any(
                episode_file.get(key) not in (None, "", 0, False)
                for key in ("path", "relativePath", "size", "id")
            )
        )
    )
    return SonarrEpisode(
        _positive_int(value.get("id"), "id", optional=True),
        season,
        episode,
        _text(value.get("title"), fallback=f"Episode {season}x{episode}")
        or f"Episode {season}x{episode}",
        has_file,
        _text(value.get("airDate"), max_bytes=64),
        value.get("monitored") is True,
        _positive_int(value.get("seriesId"), "seriesId", optional=True),
        _text(value.get("status"), max_bytes=64),
    )


class SonarrClient:
    """Configured-origin Sonarr v3 client."""

    service_name = "sonarr"

    def __init__(
        self,
        endpoint: ServiceEndpoint | str | None = None,
        api_key: str | SecretFileRef | Path | None = None,
        *,
        config: object | None = None,
        secret_reader: SecretReader | Callable[[object], str] | None = None,
        transport: HttpTransport | None = None,
        defaults: SonarrDefaults | None = None,
        policy: SonarrDefaults | None = None,
        timeouts: TimeoutConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = _endpoint_url(endpoint, name="sonarr", config=config)
        self.api_key = _secret_value(
            api_key, config=config, field_name="sonarr_api_key", reader=secret_reader
        )
        self.defaults = policy or defaults or _defaults_from_config(config)
        configured_timeouts = timeouts or getattr(config, "timeouts", None)
        self.transport: HttpTransport = transport or _ConfiguredHTTPTransport(
            timeouts=configured_timeouts,
            session=session,
            allowed_origin=self.base_url,
            allowed_addresses=getattr(config, "sonarr_allowed_addresses", ())
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
        payload: Mapping[str, object] | None = None,
        max_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    ) -> object:
        if not path.startswith("/") or "?" in path or "#" in path or ".." in path:
            raise ValueError("Sonarr path must be a fixed API path")
        response = self.transport.request(
            method,
            self.base_url + path,
            headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
            params=params,
            json_body=payload,
            max_bytes=max_bytes,
        )
        if len(_response_body(response)) > max_bytes:
            raise AdapterResponseError("Sonarr response exceeds the bounded body limit")
        return _json_response(response, service=self.service_name)

    def system_status(self) -> Mapping[str, object]:
        source = _typed_mapping(
            self._request("GET", "/api/v3/system/status"), "Sonarr system status"
        )
        return {
            "version": _text(source.get("version"), max_bytes=64),
            "branch": _text(source.get("branch"), max_bytes=64),
            "instance_name": _text(source.get("instanceName"), max_bytes=128),
            "is_up": True,
        }

    def list_series(self) -> tuple[SonarrSeries, ...]:
        return tuple(
            _series_from_mapping(value)
            for value in _records(self._request("GET", "/api/v3/series"))
        )

    def get_series(self, series_id: int) -> SonarrSeries:
        series_id = _positive_int(series_id, "series_id") or 0
        return _series_from_mapping(
            _typed_mapping(
                self._request("GET", f"/api/v3/series/{series_id}"), "Sonarr series"
            )
        )

    get_series_by_id = get_series

    def find_existing_series(self, tvdb_id: int) -> SonarrSeries | None:
        validated_tvdb_id = _positive_int(tvdb_id, "tvdb_id")
        assert validated_tvdb_id is not None
        return next(
            (
                series
                for series in self.list_series()
                if series.tvdb_id == validated_tvdb_id
            ),
            None,
        )

    get_existing_series = find_existing_series
    lookup_existing_series = find_existing_series

    def lookup_series(
        self, tvdb_id: int | None = None, *, query: str | None = None
    ) -> tuple[SonarrSeries, ...]:
        if tvdb_id is not None:
            tvdb_id = _positive_int(tvdb_id, "tvdb_id")
        if tvdb_id is None and (not isinstance(query, str) or not query.strip()):
            raise ValueError("tvdb_id or query is required")
        query_value = query.strip() if isinstance(query, str) else ""
        params = (
            {"term": f"tvdb:{tvdb_id}"}
            if tvdb_id is not None
            else {"term": query_value}
        )
        return tuple(
            _series_from_mapping(value)
            for value in _records(
                self._request("GET", "/api/v3/series/lookup", params=params)
            )
        )

    def search_series(self, query: str, *, limit: int = 25) -> Page[MediaCandidate]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be blank")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SEARCH_RESULTS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
        result = self.lookup_series(query=query)
        candidates: list[MediaCandidate] = []
        for series in result[:limit]:
            provider_id = series.tvdb_id
            if provider_id is None:
                continue
            candidates.append(
                MediaCandidate(
                    MediaType.SERIES,
                    provider_id,
                    series.title,
                    series.year,
                    series.overview,
                    series.identity,
                )
            )
        return Page(
            items=tuple(candidates), total=len(result), truncated=len(result) > limit
        )

    def _season_payload(
        self,
        series: SonarrSeries | Mapping[str, object],
        seasons: tuple[int, ...],
        *,
        preserve_existing: bool,
    ) -> list[dict[str, object]]:
        if isinstance(series, SonarrSeries):
            source: Mapping[str, object] = {
                "seasons": [
                    {
                        "seasonNumber": number,
                        "monitored": number in series.monitored_seasons,
                    }
                    for number in series.seasons
                ]
            }
        else:
            source = series
        raw_seasons = source.get("seasons") if isinstance(source, Mapping) else None
        existing: dict[int, Mapping[str, object]] = {}
        if isinstance(raw_seasons, Sequence) and not isinstance(
            raw_seasons, (str, bytes, bytearray)
        ):
            for value in raw_seasons:
                if isinstance(value, Mapping):
                    number = _nonnegative_int(
                        value.get("seasonNumber"), "seasonNumber", optional=True
                    )
                    if number is not None:
                        existing[number] = value
        missing = [season for season in seasons if season not in existing]
        if missing and existing:
            raise AdapterResponseError(
                "requested seasons are not available in Sonarr metadata"
            )
        result: list[dict[str, object]] = []
        for number, value in sorted(existing.items()):
            row = dict(value)
            row["seasonNumber"] = number
            row["monitored"] = number in seasons or (
                preserve_existing and value.get("monitored") is True
            )
            result.append(row)
        if not result:
            # Minimal lookup fixtures occasionally omit the seasons array.  The
            # server can still accept explicit season rows, but we never infer
            # any season beyond the caller's validated list.
            result = [{"seasonNumber": number, "monitored": True} for number in seasons]
        return result

    def add_series(
        self,
        series: SonarrSeries | Mapping[str, object],
        *,
        tvdb_id: int | None = None,
        seasons: Sequence[int],
        anime: bool = False,
    ) -> SonarrSeries:
        if not isinstance(anime, bool):
            raise ValueError("anime must be a boolean")
        requested = _normalize_seasons(seasons)
        if isinstance(series, SonarrSeries):
            source: Mapping[str, object] = {
                "title": series.title,
                "year": series.year,
                "tvdbId": series.tvdb_id,
                "tmdbId": series.tmdb_id,
                "imdbId": series.imdb_id,
                "overview": series.overview,
                "seasons": [
                    {
                        "seasonNumber": number,
                        "monitored": number in series.monitored_seasons,
                    }
                    for number in series.seasons
                ],
                "id": series.id,
            }
        else:
            source = series
        if not isinstance(source, Mapping):
            raise ValueError("series metadata must be a mapping")
        explicit_id = _positive_int(tvdb_id, "tvdb_id") if tvdb_id is not None else None
        identifier = explicit_id or (
            series.tvdb_id
            if isinstance(series, SonarrSeries)
            else _positive_int(source.get("tvdbId"), "tvdbId", optional=True)
        )
        if identifier is None:
            raise ValueError("tvdb_id is required")
        payload: dict[str, object] = {
            key: value
            for key, value in source.items()
            if value is not None
            and key
            in {
                "title",
                "year",
                "tvdbId",
                "tmdbId",
                "imdbId",
                "titleSlug",
                "overview",
                "seasons",
                "images",
                "genres",
                "cleanTitle",
                "sortTitle",
                "alternateTitles",
                "seriesType",
                "language",
            }
        }
        payload["tvdbId"] = identifier
        payload["seasons"] = self._season_payload(
            series, requested, preserve_existing=False
        )
        payload["monitored"] = self.defaults.monitored
        payload["seasonFolder"] = self.defaults.season_folder
        payload["tags"] = list(self.defaults.tag_ids)
        payload["addOptions"] = {
            "searchForMissingEpisodes": self.defaults.search_for_missing_episodes
        }
        profile_id = self.defaults.profile_id(anime)
        if profile_id is not None:
            payload["qualityProfileId"] = profile_id
        if self.defaults.root_folder_path is not None:
            payload["rootFolderPath"] = self.defaults.root_folder_path
        normalized = _series_from_mapping(
            _typed_mapping(
                self._request("POST", "/api/v3/series", payload=payload),
                "Sonarr add series",
            )
        )
        if normalized.tvdb_id != identifier:
            raise AdapterResponseError(
                "Sonarr add response did not match the requested TVDB identity"
            )
        return normalized

    def update_series(
        self,
        series: SonarrSeries | Mapping[str, object],
        *,
        seasons: Sequence[int],
        preserve_existing: bool = True,
    ) -> SonarrSeries:
        requested = _normalize_seasons(seasons)
        if isinstance(series, SonarrSeries):
            source: Mapping[str, object] = {
                "title": series.title,
                "year": series.year,
                "tvdbId": series.tvdb_id,
                "tmdbId": series.tmdb_id,
                "imdbId": series.imdb_id,
                "overview": series.overview,
                "seasons": [
                    {
                        "seasonNumber": number,
                        "monitored": number in series.monitored_seasons,
                    }
                    for number in series.seasons
                ],
                "id": series.id,
                "monitored": series.monitored,
                "qualityProfileId": series.quality_profile_id,
                "rootFolderPath": series.root_folder_path,
                "seasonFolder": series.season_folder,
                "tags": list(series.tags),
                "seriesType": series.series_type,
                "languageProfileId": series.language_profile_id,
            }
        else:
            source = series
        if not isinstance(source, Mapping):
            raise ValueError("series metadata must be a mapping")
        series_id = _positive_int(source.get("id"), "series_id", optional=True)
        if series_id is None and isinstance(series, SonarrSeries):
            series_id = series.id
        if series_id is None:
            raise ValueError("series id is required")
        update_fields = {
            "id",
            "title",
            "year",
            "tvdbId",
            "tmdbId",
            "imdbId",
            "overview",
            "seasons",
            "images",
            "genres",
            "cleanTitle",
            "sortTitle",
            "alternateTitles",
            "seriesType",
            "language",
            "qualityProfileId",
            "rootFolderPath",
            "seasonFolder",
            "tags",
            "seriesType",
            "languageProfileId",
        }
        payload = {
            key: value
            for key, value in source.items()
            if key in update_fields and value is not None
        }
        payload["seasons"] = self._season_payload(
            series, requested, preserve_existing=preserve_existing
        )
        # An update is not an add.  Preserve the existing series monitoring
        # flag; only the requested season rows are changed.
        if "monitored" in source:
            payload["monitored"] = source.get("monitored") is True
        result = self._request("PUT", f"/api/v3/series/{series_id}", payload=payload)
        normalized = _series_from_mapping(
            _typed_mapping(result, "Sonarr update series")
        )
        expected_tvdb = _positive_int(source.get("tvdbId"), "tvdbId", optional=True)
        if expected_tvdb is not None and normalized.tvdb_id != expected_tvdb:
            raise AdapterResponseError(
                "Sonarr update response did not match the existing TVDB identity"
            )
        return normalized

    def season_search(self, series_id: int, season_number: int) -> Mapping[str, object]:
        series_id = _positive_int(series_id, "series_id") or 0
        if (
            isinstance(season_number, bool)
            or not isinstance(season_number, int)
            or season_number < 0
        ):
            raise ValueError("season_number must be a non-negative integer")
        result = self._request(
            "POST",
            "/api/v3/command",
            payload={
                "name": "SeasonSearch",
                "seriesId": series_id,
                "seasonNumber": season_number,
            },
        )
        return _typed_mapping(result, "Sonarr SeasonSearch")

    search_season = season_search

    def search_seasons(
        self, series_id: int, seasons: Sequence[int]
    ) -> tuple[Mapping[str, object], ...]:
        requested = _normalize_seasons(seasons)
        # This loop is intentionally over seasons, not episodes.  Callers can
        # safely retry the whole method when command idempotency is fenced by
        # the durable request command keys.
        return tuple(self.season_search(series_id, season) for season in requested)

    def list_episodes(
        self, series_id: int, *, season_number: int | None = None
    ) -> tuple[SonarrEpisode, ...]:
        series_id = _positive_int(series_id, "series_id") or 0
        if season_number is not None and (
            isinstance(season_number, bool)
            or not isinstance(season_number, int)
            or season_number < 0
        ):
            raise ValueError("season_number must be a non-negative integer")
        params: dict[str, object] = {"seriesId": series_id}
        if season_number is not None:
            params["seasonNumber"] = season_number
        episodes: list[SonarrEpisode] = []
        for value in _records(self._request("GET", "/api/v3/episode", params=params))[
            :MAX_EPISODES
        ]:
            episode = _episode_from_mapping(value)
            if episode is not None:
                episodes.append(episode)
        return tuple(episodes)

    get_episodes = list_episodes

    def list_episode_records(
        self, series_id: int, *, season_number: int
    ) -> tuple[dict[str, object], ...]:
        """Return a bounded, sanitized episode mapping for enumeration.

        This is intentionally not a raw provider passthrough: filesystem paths,
        URLs, and episode-file metadata are omitted before the enumeration
        boundary.
        """

        season_number = _nonnegative_int(season_number, "season_number") or 0
        episodes = self.list_episodes(series_id, season_number=season_number)
        return tuple(
            {
                "id": episode.id,
                "seasonNumber": episode.season_number,
                "episodeNumber": episode.episode_number,
                "title": episode.title,
                "airDate": episode.air_date,
                "hasFile": episode.has_file,
                "monitored": episode.monitored,
                "seriesId": episode.series_id,
                "status": episode.status,
            }
            for episode in episodes
        )

    def season_availability(
        self, series_id: int, season_number: int
    ) -> SonarrSeasonAvailability:
        if (
            isinstance(season_number, bool)
            or not isinstance(season_number, int)
            or season_number < 0
        ):
            raise ValueError("season_number must be a non-negative integer")
        episodes = self.list_episodes(series_id, season_number=season_number)
        available = sum(1 for episode in episodes if episode.has_file)
        # Sonarr may expose future episodes without an air date.  They remain
        # known units but are not silently counted as missing playable media.
        future = sum(
            1
            for episode in episodes
            if not episode.has_file and _episode_is_future(episode)
        )
        total = len(episodes)
        return SonarrSeasonAvailability(
            season_number, total, available, max(0, total - available - future), future
        )

    def queue(
        self, *, page: int = 1, page_size: int = DEFAULT_QUEUE_PAGE_SIZE
    ) -> tuple[SonarrQueueRecord, ...]:
        if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
            raise ValueError("page must be a positive integer")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_QUEUE_PAGE_SIZE
        ):
            raise ValueError(f"page_size must be between 1 and {MAX_QUEUE_PAGE_SIZE}")
        result = self._request(
            "GET", "/api/v3/queue", params={"page": page, "pageSize": page_size}
        )
        records: list[SonarrQueueRecord] = []
        for value in _records(result)[:MAX_EPISODES]:
            nested = value.get("series")
            title = _text(value.get("title"), fallback=None)
            if title is None and isinstance(nested, Mapping):
                title = _text(nested.get("title"), fallback=None)
            title = title or "Sonarr item"
            provider_id = _positive_int(
                value.get("seriesId"), "seriesId", optional=True
            )
            if provider_id is None and isinstance(nested, Mapping):
                provider_id = _positive_int(nested.get("id"), "id", optional=True)
            error = _text(value.get("errorMessage"), max_bytes=512)
            queue_item = QueueItem(
                ServiceName.SONARR,
                title,
                _queue_state(value),
                _progress(value),
                _eta_seconds(value.get("timeleft", value.get("timeLeft"))),
                error,
                MediaType.SERIES,
            )
            records.append(SonarrQueueRecord(queue_item, provider_id))
        return tuple(records)

    queue_items = queue
    get_queue = queue

    def health(self) -> bool:
        try:
            self.system_status()
        except AdapterError:
            return False
        return True


def _queue_state(value: Mapping[str, object]) -> QueueState:
    status = str(
        value.get("status") or value.get("trackedDownloadState") or "unknown"
    ).lower()
    if status in {"queued", "pending"}:
        return QueueState.QUEUED
    if status in {"downloading", "downloaded"}:
        return QueueState.DOWNLOADING
    if status in {"importpending", "importing", "importblocked"}:
        return QueueState.IMPORTING
    if status in {"paused", "pause"}:
        return QueueState.PAUSED
    if status in {"failed", "error"}:
        return QueueState.FAILED
    if status in {"completed", "complete", "done"}:
        return QueueState.COMPLETED
    return QueueState.UNKNOWN


def _episode_is_future(episode: SonarrEpisode) -> bool:
    """Treat missing/invalid air dates conservatively as not-yet-playable."""

    if not episode.air_date:
        return True
    value = episode.air_date.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) > datetime.now(timezone.utc)


def _defaults_from_config(config: object | None) -> SonarrDefaults:
    if config is None:
        return SonarrDefaults()

    def positive(name: str) -> int | None:
        return _positive_int(getattr(config, name, None), name, optional=True)

    def text(name: str) -> str | None:
        value = getattr(config, name, None)
        return value.strip() if isinstance(value, str) and value.strip() else None

    tags_value = getattr(config, "sonarr_tag_ids", ())
    tags: list[int] = []
    values: Sequence[object]
    if isinstance(tags_value, str):
        values = tuple(tags_value.split(","))
    elif isinstance(tags_value, Sequence):
        values = tags_value
    else:
        values = ()
    for value in values:
        try:
            parsed = _positive_int(
                value
                if isinstance(value, int) and not isinstance(value, bool)
                else int(value.strip())
                if isinstance(value, str) and value.strip().isdigit()
                else 0,
                "tag_id",
                optional=True,
            )
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            tags.append(parsed)
    return SonarrDefaults(
        positive("sonarr_normal_quality_profile_id"),
        text("sonarr_normal_quality_profile_name"),
        positive("sonarr_anime_quality_profile_id"),
        text("sonarr_anime_quality_profile_name"),
        text("sonarr_root_folder_path"),
        tuple(tags),
    )


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
    "MAX_REQUESTED_SEASONS",
    "SonarrClient",
    "SonarrDefaults",
    "SonarrEpisode",
    "SonarrQueueRecord",
    "SonarrSeasonAvailability",
    "SonarrSeries",
]
