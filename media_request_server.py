from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Mapping

import requests


ENV_RADARR_URL = "PLEX_MEDIA_REQUEST_RADARR_BASE_URL"
ENV_RADARR_API_KEY = "PLEX_MEDIA_REQUEST_RADARR_API_KEY"
ENV_RADARR_QUALITY_PROFILE_ID = "PLEX_MEDIA_REQUEST_RADARR_QUALITY_PROFILE_ID"
ENV_RADARR_QUALITY_PROFILE_NAME = "PLEX_MEDIA_REQUEST_RADARR_QUALITY_PROFILE_NAME"
ENV_RADARR_ROOT_FOLDER_PATH = "PLEX_MEDIA_REQUEST_RADARR_ROOT_FOLDER_PATH"
ENV_RADARR_TAG_IDS = "PLEX_MEDIA_REQUEST_RADARR_TAG_IDS"
ENV_SONARR_URL = "PLEX_MEDIA_REQUEST_SONARR_BASE_URL"
ENV_SONARR_API_KEY = "PLEX_MEDIA_REQUEST_SONARR_API_KEY"
ENV_SONARR_NORMAL_QUALITY_PROFILE_ID = (
    "PLEX_MEDIA_REQUEST_SONARR_NORMAL_QUALITY_PROFILE_ID"
)
ENV_SONARR_NORMAL_QUALITY_PROFILE_NAME = (
    "PLEX_MEDIA_REQUEST_SONARR_NORMAL_QUALITY_PROFILE_NAME"
)
ENV_SONARR_ANIME_QUALITY_PROFILE_ID = (
    "PLEX_MEDIA_REQUEST_SONARR_ANIME_QUALITY_PROFILE_ID"
)
ENV_SONARR_ANIME_QUALITY_PROFILE_NAME = (
    "PLEX_MEDIA_REQUEST_SONARR_ANIME_QUALITY_PROFILE_NAME"
)
ENV_SONARR_ROOT_FOLDER_PATH = "PLEX_MEDIA_REQUEST_SONARR_ROOT_FOLDER_PATH"
ENV_SONARR_TAG_IDS = "PLEX_MEDIA_REQUEST_SONARR_TAG_IDS"

REQUIRED_ENV_VARS = (
    ENV_RADARR_URL,
    ENV_RADARR_API_KEY,
    ENV_RADARR_QUALITY_PROFILE_ID,
    ENV_RADARR_QUALITY_PROFILE_NAME,
    ENV_RADARR_ROOT_FOLDER_PATH,
    ENV_SONARR_URL,
    ENV_SONARR_API_KEY,
    ENV_SONARR_NORMAL_QUALITY_PROFILE_ID,
    ENV_SONARR_NORMAL_QUALITY_PROFILE_NAME,
    ENV_SONARR_ANIME_QUALITY_PROFILE_ID,
    ENV_SONARR_ANIME_QUALITY_PROFILE_NAME,
    ENV_SONARR_ROOT_FOLDER_PATH,
)

DEFAULT_TIMEOUT_SECONDS = 15
MAX_SEARCH_RESULTS = 10


class ArrApiError(RuntimeError):
    """Raised when a Radarr or Sonarr API call fails."""


@dataclass(frozen=True)
class ArrConfig:
    radarr_url: str
    radarr_api_key: str
    radarr_quality_profile_id: int
    radarr_quality_profile_name: str
    radarr_root_folder_path: str
    radarr_tag_ids: list[int]
    sonarr_url: str
    sonarr_api_key: str
    sonarr_normal_quality_profile_id: int
    sonarr_normal_quality_profile_name: str
    sonarr_anime_quality_profile_id: int
    sonarr_anime_quality_profile_name: str
    sonarr_root_folder_path: str
    sonarr_tag_ids: list[int]


def normalize_base_url(value: str) -> str:
    stripped = value.strip().rstrip("/")
    if not stripped:
        raise ValueError("base URL cannot be blank")
    return stripped


def load_config(env: Mapping[str, str] | None = None) -> ArrConfig:
    values = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV_VARS if not values.get(name, "").strip()]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    return ArrConfig(
        radarr_url=normalize_base_url(values[ENV_RADARR_URL]),
        radarr_api_key=values[ENV_RADARR_API_KEY].strip(),
        radarr_quality_profile_id=_load_positive_int(
            values[ENV_RADARR_QUALITY_PROFILE_ID], ENV_RADARR_QUALITY_PROFILE_ID
        ),
        radarr_quality_profile_name=values[ENV_RADARR_QUALITY_PROFILE_NAME].strip(),
        radarr_root_folder_path=values[ENV_RADARR_ROOT_FOLDER_PATH].strip(),
        radarr_tag_ids=_load_int_list(
            values.get(ENV_RADARR_TAG_IDS, ""), ENV_RADARR_TAG_IDS
        ),
        sonarr_url=normalize_base_url(values[ENV_SONARR_URL]),
        sonarr_api_key=values[ENV_SONARR_API_KEY].strip(),
        sonarr_normal_quality_profile_id=_load_positive_int(
            values[ENV_SONARR_NORMAL_QUALITY_PROFILE_ID],
            ENV_SONARR_NORMAL_QUALITY_PROFILE_ID,
        ),
        sonarr_normal_quality_profile_name=values[
            ENV_SONARR_NORMAL_QUALITY_PROFILE_NAME
        ].strip(),
        sonarr_anime_quality_profile_id=_load_positive_int(
            values[ENV_SONARR_ANIME_QUALITY_PROFILE_ID],
            ENV_SONARR_ANIME_QUALITY_PROFILE_ID,
        ),
        sonarr_anime_quality_profile_name=values[
            ENV_SONARR_ANIME_QUALITY_PROFILE_NAME
        ].strip(),
        sonarr_root_folder_path=values[ENV_SONARR_ROOT_FOLDER_PATH].strip(),
        sonarr_tag_ids=_load_int_list(
            values.get(ENV_SONARR_TAG_IDS, ""), ENV_SONARR_TAG_IDS
        ),
    )


class MediaRequestService:
    def __init__(
        self,
        config: ArrConfig,
        session: requests.Session | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds

    def search_movie(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query = _require_text(query, "query")
        result_limit = _normalize_limit(limit)
        results = self._get_radarr("/api/v3/movie/lookup", params={"term": query})
        return [_shape_movie_result(item) for item in _ensure_list(results)][
            :result_limit
        ]

    def add_movie(self, tmdbId: int, title: str | None = None) -> dict[str, Any]:
        tmdb_id = _require_positive_int(tmdbId, "tmdbId")
        requested_title = _optional_text(title)

        try:
            existing = self._find_existing_movie(tmdb_id)
            if existing is not None:
                existing_title = existing.get("title") or requested_title
                return {
                    "status": "already_exists",
                    "title": existing_title,
                    "tmdbId": tmdb_id,
                    "message": f"{existing_title or 'Movie'} is already in Radarr.",
                }

            movie = self._lookup_movie_by_tmdb(tmdb_id)
            if movie is None:
                return {
                    "status": "error",
                    "title": requested_title,
                    "tmdbId": tmdb_id,
                    "message": "Radarr did not return metadata for that TMDB ID.",
                }

            payload = dict(movie)
            payload.update(
                {
                    "tmdbId": tmdb_id,
                    "qualityProfileId": self.config.radarr_quality_profile_id,
                    "rootFolderPath": self.config.radarr_root_folder_path,
                    "monitored": True,
                    "minimumAvailability": "announced",
                    "tags": self.config.radarr_tag_ids,
                    "addOptions": {"searchForMovie": True},
                }
            )

            response = self._post_radarr("/api/v3/movie", json=payload)
            added_title = response.get("title") or movie.get("title") or requested_title
            return {
                "status": "added",
                "title": added_title,
                "tmdbId": tmdb_id,
                "message": (
                    f"{added_title or 'Movie'} was added to Radarr using "
                    f"{self.config.radarr_quality_profile_name}."
                ),
            }
        except ArrApiError as exc:
            return {
                "status": "error",
                "title": requested_title,
                "tmdbId": tmdb_id,
                "message": str(exc),
            }

    def search_show(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query = _require_text(query, "query")
        result_limit = _normalize_limit(limit)
        results = self._get_sonarr("/api/v3/series/lookup", params={"term": query})
        return [_shape_show_result(item) for item in _ensure_list(results)][
            :result_limit
        ]

    def add_show(
        self, tvdbId: int, title: str | None = None, anime: bool = False
    ) -> dict[str, Any]:
        tvdb_id = _require_positive_int(tvdbId, "tvdbId")
        requested_title = _optional_text(title)
        profile_id = (
            self.config.sonarr_anime_quality_profile_id
            if anime
            else self.config.sonarr_normal_quality_profile_id
        )
        profile_name = (
            self.config.sonarr_anime_quality_profile_name
            if anime
            else self.config.sonarr_normal_quality_profile_name
        )

        try:
            existing = self._find_existing_show(tvdb_id)
            if existing is not None:
                existing_title = existing.get("title") or requested_title
                return {
                    "status": "already_exists",
                    "title": existing_title,
                    "tvdbId": tvdb_id,
                    "profileUsed": profile_name,
                    "message": f"{existing_title or 'Series'} is already in Sonarr.",
                }

            series = self._lookup_show_by_tvdb(tvdb_id)
            if series is None:
                return {
                    "status": "error",
                    "title": requested_title,
                    "tvdbId": tvdb_id,
                    "profileUsed": profile_name,
                    "message": "Sonarr did not return metadata for that TVDB ID.",
                }

            payload = dict(series)
            payload.update(
                {
                    "tvdbId": tvdb_id,
                    "qualityProfileId": profile_id,
                    "rootFolderPath": self.config.sonarr_root_folder_path,
                    "monitored": True,
                    "seasonFolder": True,
                    "tags": self.config.sonarr_tag_ids,
                    "addOptions": {"searchForMissingEpisodes": True},
                }
            )

            response = self._post_sonarr("/api/v3/series", json=payload)
            added_title = response.get("title") or series.get("title") or requested_title
            return {
                "status": "added",
                "title": added_title,
                "tvdbId": tvdb_id,
                "profileUsed": profile_name,
                "message": f"{added_title or 'Series'} was added to Sonarr.",
            }
        except ArrApiError as exc:
            return {
                "status": "error",
                "title": requested_title,
                "tvdbId": tvdb_id,
                "profileUsed": profile_name,
                "message": str(exc),
            }

    def media_status(self) -> dict[str, Any]:
        return {
            "radarr": self._service_status("radarr"),
            "sonarr": self._service_status("sonarr"),
        }

    def _find_existing_movie(self, tmdb_id: int) -> dict[str, Any] | None:
        movies = self._get_radarr("/api/v3/movie")
        return _find_by_id(_ensure_list(movies), "tmdbId", tmdb_id)

    def _lookup_movie_by_tmdb(self, tmdb_id: int) -> dict[str, Any] | None:
        results = self._get_radarr(
            "/api/v3/movie/lookup", params={"term": f"tmdb:{tmdb_id}"}
        )
        return _find_by_id(_ensure_list(results), "tmdbId", tmdb_id)

    def _find_existing_show(self, tvdb_id: int) -> dict[str, Any] | None:
        series = self._get_sonarr("/api/v3/series")
        return _find_by_id(_ensure_list(series), "tvdbId", tvdb_id)

    def _lookup_show_by_tvdb(self, tvdb_id: int) -> dict[str, Any] | None:
        results = self._get_sonarr(
            "/api/v3/series/lookup", params={"term": f"tvdb:{tvdb_id}"}
        )
        return _find_by_id(_ensure_list(results), "tvdbId", tvdb_id)

    def _service_status(self, service: str) -> dict[str, Any]:
        try:
            if service == "radarr":
                status = self._get_radarr("/api/v3/system/status")
            else:
                status = self._get_sonarr("/api/v3/system/status")

            return {
                "ok": True,
                "version": status.get("version"),
                "message": "connected",
            }
        except ArrApiError as exc:
            return {
                "ok": False,
                "version": None,
                "message": str(exc),
            }

    def _get_radarr(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> Any:
        return self._request(
            "GET",
            self.config.radarr_url,
            self.config.radarr_api_key,
            path,
            params=params,
        )

    def _post_radarr(self, path: str, json: Mapping[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST",
            self.config.radarr_url,
            self.config.radarr_api_key,
            path,
            json=json,
        )
        return response if isinstance(response, dict) else {}

    def _get_sonarr(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> Any:
        return self._request(
            "GET",
            self.config.sonarr_url,
            self.config.sonarr_api_key,
            path,
            params=params,
        )

    def _post_sonarr(self, path: str, json: Mapping[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST",
            self.config.sonarr_url,
            self.config.sonarr_api_key,
            path,
            json=json,
        )
        return response if isinstance(response, dict) else {}

    def _request(
        self,
        method: str,
        base_url: str,
        api_key: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{base_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                headers={"X-Api-Key": api_key},
                params=params,
                json=json,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ArrApiError(f"{method} {path} failed: {exc}") from exc

        if response.status_code == 204 or not response.content:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise ArrApiError(f"{method} {path} returned invalid JSON") from exc


def create_server() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "The Python MCP SDK is required. Install dependencies from requirements.txt."
        ) from exc

    service = MediaRequestService(load_config())
    mcp = FastMCP("plex-media-request")

    @mcp.tool()
    def search_movie(query: str, limit: int = 5) -> list[dict[str, Any]]:
        return service.search_movie(query, limit=limit)

    @mcp.tool()
    def add_movie(tmdbId: int, title: str | None = None) -> dict[str, Any]:
        return service.add_movie(tmdbId=tmdbId, title=title)

    @mcp.tool()
    def search_show(query: str, limit: int = 5) -> list[dict[str, Any]]:
        return service.search_show(query, limit=limit)

    @mcp.tool()
    def add_show(
        tvdbId: int, title: str | None = None, anime: bool = False
    ) -> dict[str, Any]:
        return service.add_show(tvdbId=tvdbId, title=title, anime=anime)

    @mcp.tool()
    def media_status() -> dict[str, Any]:
        return service.media_status()

    return mcp


def main() -> None:
    try:
        server = create_server()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    server.run()


def _shape_movie_result(item: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "title": item.get("title"),
        "year": item.get("year"),
        "tmdb_id": item.get("tmdbId"),
    }
    _copy_optional_renamed(item, result, "imdbId", "imdb_id")
    _copy_optional_renamed(item, result, "runtime", "runtime_minutes")
    _copy_optional_renamed(item, result, "overview", "overview")
    _copy_if_not_none(result, "poster_url", _poster_url(item.get("images")))
    _copy_existence(item, result)
    return result


def _shape_show_result(item: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "title": item.get("title"),
        "year": item.get("year"),
        "tvdb_id": item.get("tvdbId"),
    }
    _copy_optional_renamed(item, result, "imdbId", "imdb_id")
    _copy_optional_renamed(item, result, "tmdbId", "tmdb_id")
    _copy_if_not_none(result, "season_count", _season_count(item))
    _copy_optional_renamed(item, result, "status", "status")
    _copy_optional_renamed(item, result, "overview", "overview")
    _copy_if_not_none(result, "poster_url", _poster_url(item.get("images")))
    _copy_if_not_none(result, "is_anime", _is_anime(item))
    _copy_existence(item, result)
    return result


def _copy_optional_renamed(
    source: Mapping[str, Any], target: dict[str, Any], source_key: str, target_key: str
) -> None:
    if source_key in source and source[source_key] is not None:
        target[target_key] = source[source_key]


def _copy_if_not_none(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


def _copy_existence(source: Mapping[str, Any], target: dict[str, Any]) -> None:
    existence = _first_present(source, ("alreadyExists", "isExisting"))
    if existence is None:
        return
    exists = bool(existence)
    target["in_library"] = exists
    target["already_exists"] = exists


def _first_present(source: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def _poster_url(images: Any) -> str | None:
    if not isinstance(images, list):
        return None

    for image in images:
        if not isinstance(image, dict):
            continue
        cover_type = image.get("coverType")
        if isinstance(cover_type, str) and cover_type.lower() != "poster":
            continue
        url = image.get("remoteUrl")
        if _is_external_url(url):
            return url
    return None


def _is_external_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("https://", "http://"))


def _season_count(item: Mapping[str, Any]) -> int | None:
    season_count = item.get("seasonCount")
    if isinstance(season_count, int) and not isinstance(season_count, bool):
        return season_count

    seasons = item.get("seasons")
    if isinstance(seasons, list):
        return len([season for season in seasons if isinstance(season, dict)])
    return None


def _is_anime(item: Mapping[str, Any]) -> bool | None:
    series_type = item.get("seriesType")
    if isinstance(series_type, str):
        return series_type.lower() == "anime"

    genres = item.get("genres")
    if isinstance(genres, list) and any(
        isinstance(genre, str) and genre.lower() == "anime" for genre in genres
    ):
        return True
    return None


def _ensure_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _find_by_id(
    items: list[dict[str, Any]], key: str, expected_id: int
) -> dict[str, Any] | None:
    return next((item for item in items if item.get(key) == expected_id), None)


def _require_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _normalize_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("limit must be a positive integer")
    return min(value, MAX_SEARCH_RESULTS)


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _load_positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


def _load_int_list(value: str, name: str) -> list[int]:
    stripped = value.strip()
    if not stripped:
        return []

    parsed: list[int] = []
    for raw_item in stripped.split(","):
        item = raw_item.strip()
        if not item:
            continue
        try:
            parsed_item = int(item)
        except ValueError as exc:
            raise RuntimeError(
                f"{name} must be a comma-separated list of positive integers"
            ) from exc
        if parsed_item <= 0:
            raise RuntimeError(
                f"{name} must be a comma-separated list of positive integers"
            )
        parsed.append(parsed_item)
    return parsed


def _require_positive_int(value: int, name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


if __name__ == "__main__":
    main()
