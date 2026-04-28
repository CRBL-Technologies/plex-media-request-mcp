from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Mapping

import requests


ENV_RADARR_URL = "PLEX_MEDIA_REQUEST_RADARR_BASE_URL"
ENV_RADARR_API_KEY = "PLEX_MEDIA_REQUEST_RADARR_API_KEY"
ENV_SONARR_URL = "PLEX_MEDIA_REQUEST_SONARR_BASE_URL"
ENV_SONARR_API_KEY = "PLEX_MEDIA_REQUEST_SONARR_API_KEY"

DEFAULT_RADARR_URL = "http://radarr:7878"
DEFAULT_SONARR_URL = "http://sonarr:8989"

REQUIRED_ENV_VARS = (
    ENV_RADARR_API_KEY,
    ENV_SONARR_API_KEY,
)

RADARR_QUALITY_PROFILE_ID = 25
RADARR_QUALITY_PROFILE_NAME = "HD Bluray + WEB - Original"
RADARR_ROOT_FOLDER_PATH = "/data/media/movies"

SONARR_NORMAL_QUALITY_PROFILE_ID = 25
SONARR_NORMAL_QUALITY_PROFILE_NAME = "WEB-1080p - Original"
SONARR_ANIME_QUALITY_PROFILE_ID = 26
SONARR_ANIME_QUALITY_PROFILE_NAME = "Remux-1080p - Anime - Original"
SONARR_ROOT_FOLDER_PATH = "/data/media/tv"

DEFAULT_TIMEOUT_SECONDS = 15
MAX_SEARCH_RESULTS = 10


class ArrApiError(RuntimeError):
    """Raised when a Radarr or Sonarr API call fails."""


@dataclass(frozen=True)
class ArrConfig:
    radarr_url: str
    radarr_api_key: str
    sonarr_url: str
    sonarr_api_key: str


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
        radarr_url=normalize_base_url(
            values.get(ENV_RADARR_URL, "").strip() or DEFAULT_RADARR_URL
        ),
        radarr_api_key=values[ENV_RADARR_API_KEY].strip(),
        sonarr_url=normalize_base_url(
            values.get(ENV_SONARR_URL, "").strip() or DEFAULT_SONARR_URL
        ),
        sonarr_api_key=values[ENV_SONARR_API_KEY].strip(),
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

    def search_movie(self, query: str) -> list[dict[str, Any]]:
        query = _require_text(query, "query")
        results = self._get_radarr("/api/v3/movie/lookup", params={"term": query})
        return [_shape_movie_result(item) for item in _ensure_list(results)][
            :MAX_SEARCH_RESULTS
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
                    "qualityProfileId": RADARR_QUALITY_PROFILE_ID,
                    "rootFolderPath": RADARR_ROOT_FOLDER_PATH,
                    "monitored": True,
                    "minimumAvailability": "announced",
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
                    f"{RADARR_QUALITY_PROFILE_NAME}."
                ),
            }
        except ArrApiError as exc:
            return {
                "status": "error",
                "title": requested_title,
                "tmdbId": tmdb_id,
                "message": str(exc),
            }

    def search_show(self, query: str) -> list[dict[str, Any]]:
        query = _require_text(query, "query")
        results = self._get_sonarr("/api/v3/series/lookup", params={"term": query})
        return [_shape_show_result(item) for item in _ensure_list(results)][
            :MAX_SEARCH_RESULTS
        ]

    def add_show(
        self, tvdbId: int, title: str | None = None, anime: bool = False
    ) -> dict[str, Any]:
        tvdb_id = _require_positive_int(tvdbId, "tvdbId")
        requested_title = _optional_text(title)
        profile_id = (
            SONARR_ANIME_QUALITY_PROFILE_ID
            if anime
            else SONARR_NORMAL_QUALITY_PROFILE_ID
        )
        profile_name = (
            SONARR_ANIME_QUALITY_PROFILE_NAME
            if anime
            else SONARR_NORMAL_QUALITY_PROFILE_NAME
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
                    "rootFolderPath": SONARR_ROOT_FOLDER_PATH,
                    "monitored": True,
                    "seasonFolder": True,
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
    def search_movie(query: str) -> list[dict[str, Any]]:
        return service.search_movie(query)

    @mcp.tool()
    def add_movie(tmdbId: int, title: str | None = None) -> dict[str, Any]:
        return service.add_movie(tmdbId=tmdbId, title=title)

    @mcp.tool()
    def search_show(query: str) -> list[dict[str, Any]]:
        return service.search_show(query)

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
        "tmdbId": item.get("tmdbId"),
    }
    _copy_optional(item, result, "overview")
    _copy_optional(item, result, "alreadyExists")
    _copy_optional(item, result, "isExisting")
    return result


def _shape_show_result(item: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "title": item.get("title"),
        "year": item.get("year"),
        "tvdbId": item.get("tvdbId"),
    }
    _copy_optional(item, result, "overview")
    _copy_optional(item, result, "alreadyExists")
    _copy_optional(item, result, "isExisting")
    _copy_optional(item, result, "genres")
    return result


def _copy_optional(
    source: Mapping[str, Any], target: dict[str, Any], key: str
) -> None:
    if key in source and source[key] is not None:
        target[key] = source[key]


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


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _require_positive_int(value: int, name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


if __name__ == "__main__":
    main()
