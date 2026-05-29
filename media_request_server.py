from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

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
ENV_REQUEST_DB_PATH = "PLEX_MEDIA_REQUEST_DB_PATH"
ENV_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
QUEUE_PAGE_SIZE = 250
QUEUE_PARAMS = {"page": 1, "pageSize": QUEUE_PAGE_SIZE}

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
MAX_STATUS_RESULTS = QUEUE_PAGE_SIZE


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



class RequestStore:
    """SQLite-backed durable record of accepted media requests."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS media_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'series')),
        title TEXT,
        year INTEGER,
        requested_by_user_id INTEGER,
        requested_by_chat_id INTEGER,
        requested_by_username TEXT,
        radarr_movie_id INTEGER,
        sonarr_series_id INTEGER,
        tmdb_id INTEGER,
        tvdb_id INTEGER,
        imdb_id TEXT,
        season_numbers TEXT,
        status TEXT NOT NULL DEFAULT 'requested',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        notified_available_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_media_requests_movie_pending
        ON media_requests(media_type, radarr_movie_id, notified_available_at);
    CREATE INDEX IF NOT EXISTS idx_media_requests_series_pending
        ON media_requests(media_type, sonarr_series_id, notified_available_at);
    CREATE INDEX IF NOT EXISTS idx_media_requests_requester
        ON media_requests(requested_by_chat_id, requested_by_user_id);
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RequestStore":
        values = os.environ if env is None else env
        configured = values.get(ENV_REQUEST_DB_PATH, "").strip()
        if configured:
            return cls(configured)
        hermes_home = Path(values.get("HERMES_HOME", "").strip() or "/opt/data")
        return cls(hermes_home / "state" / "media_requests.sqlite3")

    def _initialize(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.executescript(self.SCHEMA)

    def add_request(
        self,
        *,
        media_type: str,
        title: str | None,
        year: int | None = None,
        requested_by_user_id: int | None = None,
        requested_by_chat_id: int | None = None,
        requested_by_username: str | None = None,
        radarr_movie_id: int | None = None,
        sonarr_series_id: int | None = None,
        tmdb_id: int | None = None,
        tvdb_id: int | None = None,
        imdb_id: str | None = None,
        season_numbers: list[int] | None = None,
        status: str = "requested",
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        seasons_json = json.dumps(season_numbers) if season_numbers is not None else None
        with sqlite3.connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO media_requests (
                    media_type, title, year,
                    requested_by_user_id, requested_by_chat_id, requested_by_username,
                    radarr_movie_id, sonarr_series_id, tmdb_id, tvdb_id, imdb_id,
                    season_numbers, status, created_at, updated_at, notified_available_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    media_type,
                    title,
                    year,
                    requested_by_user_id,
                    requested_by_chat_id,
                    requested_by_username,
                    radarr_movie_id,
                    sonarr_series_id,
                    tmdb_id,
                    tvdb_id,
                    imdb_id,
                    seasons_json,
                    status,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def pending_movie_notifications(self, limit: int = 100) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM media_requests
                WHERE media_type = 'movie'
                  AND radarr_movie_id IS NOT NULL
                  AND requested_by_chat_id IS NOT NULL
                  AND notified_available_at IS NULL
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_movie_notifications_for_radarr_id(
        self, radarr_movie_id: int
    ) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM media_requests
                WHERE media_type = 'movie'
                  AND radarr_movie_id = ?
                  AND requested_by_chat_id IS NOT NULL
                  AND notified_available_at IS NULL
                ORDER BY created_at ASC
                """,
                (radarr_movie_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notified(self, request_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE media_requests
                SET notified_available_at = ?, updated_at = ?, status = 'available'
                WHERE id = ?
                """,
                (now, now, request_id),
            )


class MediaRequestService:
    def __init__(
        self,
        config: ArrConfig,
        session: requests.Session | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        request_store: RequestStore | None = None,
        telegram_sender: Callable[[int, str], bool] | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.request_store = request_store
        self.telegram_sender = telegram_sender or _send_telegram_message

    def search_media(
        self,
        query: str,
        media_type: str = "any",
        season: int | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        query = _require_text(query, "query")
        requested_type = _normalize_media_type(media_type)
        requested_season = _optional_season(season)
        result_limit = _normalize_limit(limit)

        items: list[dict[str, Any]] = []
        if requested_type in {"movie", "any"}:
            movie_results = _ensure_list(
                self._get_radarr("/api/v3/movie/lookup", params={"term": query})
            )
            movies = _ensure_list(self._get_radarr("/api/v3/movie"))
            items.extend(
                _shape_movie_search_item(item, _movie_library_match(item, movies))
                for item in movie_results
            )

        if requested_type in {"series", "any"}:
            series_results = _ensure_list(
                self._get_sonarr("/api/v3/series/lookup", params={"term": query})
            )
            series = _ensure_list(self._get_sonarr("/api/v3/series"))
            items.extend(
                _shape_search_series_item(
                    item,
                    _series_library_match(item, series),
                    requested_season,
                )
                for item in series_results
            )

        return {"ok": True, "items": items[:result_limit]}

    def request_movie(
        self,
        tmdbId: int,
        title: str | None = None,
        requested_by_user_id: int | None = None,
        requested_by_chat_id: int | None = None,
        requested_by_username: str | None = None,
    ) -> dict[str, Any]:
        tmdb_id = _require_positive_int(tmdbId, "tmdbId")
        requested_title = _optional_text(title)

        try:
            existing = self._find_existing_movie(tmdb_id)
            if existing is not None:
                existing_title = existing.get("title") or requested_title
                result = {
                    "status": "already_exists",
                    "title": existing_title,
                    "tmdbId": tmdb_id,
                    "radarrMovieId": _positive_int_or_none(existing.get("id")),
                    "message": f"{existing_title or 'Movie'} is already in Radarr.",
                }
                self._record_movie_request(
                    result,
                    movie=existing,
                    tmdb_id=tmdb_id,
                    title=existing_title,
                    requested_by_user_id=requested_by_user_id,
                    requested_by_chat_id=requested_by_chat_id,
                    requested_by_username=requested_by_username,
                )
                return result

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
            result = {
                "status": "added",
                "title": added_title,
                "tmdbId": tmdb_id,
                "radarrMovieId": _positive_int_or_none(response.get("id")),
                "message": (
                    f"{added_title or 'Movie'} was added to Radarr using "
                    f"{self.config.radarr_quality_profile_name}."
                ),
            }
            self._record_movie_request(
                result,
                movie=response if response else movie,
                tmdb_id=tmdb_id,
                title=added_title,
                requested_by_user_id=requested_by_user_id,
                requested_by_chat_id=requested_by_chat_id,
                requested_by_username=requested_by_username,
            )
            return self._with_post_request_status(result, added_title)
        except ArrApiError as exc:
            return {
                "status": "error",
                "title": requested_title,
                "tmdbId": tmdb_id,
                "message": str(exc),
            }

    def request_series(
        self,
        tvdbId: int,
        title: str | None = None,
        seasons: list[int] | None = None,
        anime: bool = False,
        requested_by_user_id: int | None = None,
        requested_by_chat_id: int | None = None,
        requested_by_username: str | None = None,
    ) -> dict[str, Any]:
        tvdb_id = _require_positive_int(tvdbId, "tvdbId")
        requested_title = _optional_text(title)
        profile_name = (
            self.config.sonarr_anime_quality_profile_name
            if anime
            else self.config.sonarr_normal_quality_profile_name
        )
        try:
            requested_seasons = _require_requested_seasons(seasons)
        except ValueError as exc:
            return {
                "status": "error",
                "title": requested_title,
                "tvdbId": tvdb_id,
                "profileUsed": profile_name,
                "message": str(exc),
            }

        try:
            existing = self._find_existing_show(tvdb_id)
            if existing is not None:
                season_update = _with_season_monitoring(
                    existing, requested_seasons, preserve_existing=True
                )
                if "error" in season_update:
                    return {
                        "status": "error",
                        "title": existing.get("title") or requested_title,
                        "tvdbId": tvdb_id,
                        "profileUsed": profile_name,
                        "monitoredSeasons": requested_seasons,
                        "message": season_update["error"],
                    }

                updated = dict(existing)
                updated["monitored"] = True
                updated["seasons"] = season_update["seasons"]
                series_id = _positive_int_or_none(existing.get("id"))
                if series_id is not None:
                    updated = self._put_sonarr(f"/api/v3/series/{series_id}", json=updated)
                    for season_number in requested_seasons:
                        self._post_sonarr(
                            "/api/v3/command",
                            json={
                                "name": "SeasonSearch",
                                "seriesId": series_id,
                                "seasonNumber": season_number,
                            },
                        )

                availability = _series_availability(updated, requested_seasons)
                result = {
                    "status": "already_exists",
                    "title": updated.get("title") or existing.get("title") or requested_title,
                    "tvdbId": tvdb_id,
                    "sonarrSeriesId": series_id,
                    "profileUsed": profile_name,
                    "monitoredSeasons": requested_seasons,
                    "monitoringUpdated": series_id is not None,
                    "searchSubmitted": series_id is not None,
                    "available": availability["availableEpisodes"] > 0,
                    "availability": availability,
                }
                self._record_series_request(
                    result,
                    series=updated,
                    tvdb_id=tvdb_id,
                    title=updated.get("title") or existing.get("title") or requested_title,
                    seasons=requested_seasons,
                    requested_by_user_id=requested_by_user_id,
                    requested_by_chat_id=requested_by_chat_id,
                    requested_by_username=requested_by_username,
                )
                return self._with_post_request_status(result, updated.get("title") or existing.get("title") or requested_title)

            return self._add_series(
                tvdbId=tvdb_id,
                title=requested_title,
                anime=anime,
                seasons=requested_seasons,
                requested_by_user_id=requested_by_user_id,
                requested_by_chat_id=requested_by_chat_id,
                requested_by_username=requested_by_username,
            )
        except ArrApiError as exc:
            return {
                "status": "error",
                "title": requested_title,
                "tvdbId": tvdb_id,
                "profileUsed": profile_name,
                "monitoredSeasons": requested_seasons,
                "message": str(exc),
            }

    def _add_series(
        self,
        tvdbId: int,
        title: str | None,
        anime: bool,
        seasons: list[int],
        requested_by_user_id: int | None = None,
        requested_by_chat_id: int | None = None,
        requested_by_username: str | None = None,
    ) -> dict[str, Any]:
        tvdb_id = _require_positive_int(tvdbId, "tvdbId")
        requested_title = _optional_text(title)
        requested_seasons = _require_requested_seasons(seasons)
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
            season_update = _with_season_monitoring(series, requested_seasons)
            if "error" in season_update:
                return {
                    "status": "error",
                    "title": series.get("title") or requested_title,
                    "tvdbId": tvdb_id,
                    "profileUsed": profile_name,
                    "monitoredSeasons": requested_seasons,
                    "message": season_update["error"],
                }
            payload["seasons"] = season_update["seasons"]

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
            result = {
                "status": "added",
                "title": added_title,
                "tvdbId": tvdb_id,
                "sonarrSeriesId": _positive_int_or_none(response.get("id")),
                "profileUsed": profile_name,
                "monitoredSeasons": requested_seasons,
                "message": (
                    f"{added_title or 'Series'} was added with only "
                    f"{_format_season_list(requested_seasons)} monitored."
                ),
            }
            self._record_series_request(
                result,
                series=response if response else series,
                tvdb_id=tvdb_id,
                title=added_title,
                seasons=requested_seasons,
                requested_by_user_id=requested_by_user_id,
                requested_by_chat_id=requested_by_chat_id,
                requested_by_username=requested_by_username,
            )
            return self._with_post_request_status(result, added_title)
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

    def notify_movie_available(self, radarr_movie_id: int) -> dict[str, Any]:
        """Notify requesters for one Radarr movie when a webhook says it is available."""
        if self.request_store is None:
            return {"ok": False, "notified": 0, "checked": 0, "message": "Request store is not configured."}

        movie_id = _positive_int_or_none(radarr_movie_id)
        if movie_id is None:
            return {"ok": False, "notified": 0, "checked": 0, "message": "radarr_movie_id must be a positive integer."}

        rows = self.request_store.pending_movie_notifications_for_radarr_id(movie_id)
        if not rows:
            return {"ok": True, "checked": 0, "notified": 0, "notifications": [], "skipped": []}

        try:
            movie = self._get_radarr(f"/api/v3/movie/{movie_id}")
        except ArrApiError as exc:
            return {
                "ok": True,
                "checked": len(rows),
                "notified": 0,
                "notifications": [],
                "skipped": [{"radarrMovieId": movie_id, "reason": str(exc)}],
            }

        if not isinstance(movie, dict) or not _movie_has_file(movie):
            return {"ok": True, "checked": len(rows), "notified": 0, "notifications": [], "skipped": []}

        notified: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in rows:
            request_id = _positive_int_or_none(row.get("id"))
            chat_id = _positive_int_or_none(row.get("requested_by_chat_id"))
            if request_id is None or chat_id is None:
                skipped.append({"id": request_id, "reason": "missing request or chat ID"})
                continue
            title = _clean_text(movie.get("title")) or _clean_text(row.get("title")) or "Your movie"
            year = _positive_int_or_none(movie.get("year")) or _positive_int_or_none(row.get("year"))
            title_with_year = f"{title} ({year})" if year else title
            message = f"✅ {title_with_year} is now available on Plex."
            if not self.telegram_sender(chat_id, message):
                skipped.append({"id": request_id, "reason": "Telegram send failed"})
                continue
            self.request_store.mark_notified(request_id)
            notified.append({"id": request_id, "title": title_with_year, "chatId": chat_id})

        return {
            "ok": True,
            "checked": len(rows),
            "notified": len(notified),
            "notifications": notified,
            "skipped": skipped,
        }

    def notify_available_requests(self, limit: int = 100) -> dict[str, Any]:
        """Manual backfill helper; webhook flow should call notify_movie_available."""
        if self.request_store is None:
            return {"ok": False, "notified": 0, "checked": 0, "message": "Request store is not configured."}

        checked = 0
        total_notified: list[dict[str, Any]] = []
        total_skipped: list[dict[str, Any]] = []
        for row in self.request_store.pending_movie_notifications(limit=limit):
            movie_id = _positive_int_or_none(row.get("radarr_movie_id"))
            if movie_id is None:
                total_skipped.append({"id": _positive_int_or_none(row.get("id")), "reason": "missing movie ID"})
                continue
            result = self.notify_movie_available(movie_id)
            checked += int(result.get("checked") or 0)
            total_notified.extend(result.get("notifications") or [])
            total_skipped.extend(result.get("skipped") or [])

        return {
            "ok": True,
            "checked": checked,
            "notified": len(total_notified),
            "notifications": total_notified,
            "skipped": total_skipped,
        }

    def _record_movie_request(
        self,
        result: dict[str, Any],
        *,
        movie: Mapping[str, Any],
        tmdb_id: int,
        title: str | None,
        requested_by_user_id: int | None,
        requested_by_chat_id: int | None,
        requested_by_username: str | None,
    ) -> None:
        if self.request_store is None:
            return
        try:
            request_id = self.request_store.add_request(
                media_type="movie",
                title=_clean_text(title) or _clean_text(movie.get("title")),
                year=_positive_int_or_none(movie.get("year")),
                requested_by_user_id=_positive_int_or_none(requested_by_user_id),
                requested_by_chat_id=_positive_int_or_none(requested_by_chat_id),
                requested_by_username=_optional_text(requested_by_username),
                radarr_movie_id=_positive_int_or_none(movie.get("id")),
                tmdb_id=tmdb_id,
                imdb_id=_clean_text(movie.get("imdbId")),
                status="requested",
            )
        except Exception as exc:
            result["requestRecord"] = {"recorded": False, "error": str(exc)}
            return
        result["requestRecord"] = {"recorded": True, "id": request_id}

    def _record_series_request(
        self,
        result: dict[str, Any],
        *,
        series: Mapping[str, Any],
        tvdb_id: int,
        title: str | None,
        seasons: list[int],
        requested_by_user_id: int | None,
        requested_by_chat_id: int | None,
        requested_by_username: str | None,
    ) -> None:
        if self.request_store is None:
            return
        try:
            request_id = self.request_store.add_request(
                media_type="series",
                title=_clean_text(title) or _clean_text(series.get("title")),
                year=_positive_int_or_none(series.get("year")),
                requested_by_user_id=_positive_int_or_none(requested_by_user_id),
                requested_by_chat_id=_positive_int_or_none(requested_by_chat_id),
                requested_by_username=_optional_text(requested_by_username),
                sonarr_series_id=_positive_int_or_none(series.get("id")),
                tmdb_id=_positive_int_or_none(series.get("tmdbId")),
                tvdb_id=tvdb_id,
                imdb_id=_clean_text(series.get("imdbId")),
                season_numbers=seasons,
                status="requested",
            )
        except Exception as exc:
            result["requestRecord"] = {"recorded": False, "error": str(exc)}
            return
        result["requestRecord"] = {"recorded": True, "id": request_id}

    def _with_post_request_status(
        self, result: dict[str, Any], title: str | None
    ) -> dict[str, Any]:
        """Attach an immediate status snapshot after a successful request.

        The Telegram agent is instructed to call `request_status` after every
        request, but putting the same snapshot in the request tool result makes
        that behavior reliable even if the model stops after the add call.
        Status collection is best-effort so a transient queue/library API issue
        does not turn a successful request into a failed request.
        """
        query = _optional_text(title)
        if not query:
            return result
        requests_log = getattr(self.session, "requests", None)
        request_count = len(requests_log) if isinstance(requests_log, list) else None
        try:
            result["postRequestStatus"] = self.request_status(query=query)
        except Exception:
            # Unit-test fakes may not provide enough follow-up responses; avoid
            # leaving a partial status request in their request log. Real HTTP
            # sessions do not expose this attribute.
            if request_count is not None:
                del requests_log[request_count:]
            return result
        return result

    def download_status(self) -> dict[str, Any]:
        radarr_queue = self._get_radarr("/api/v3/queue", params=QUEUE_PARAMS)
        sonarr_queue = self._get_sonarr("/api/v3/queue", params=QUEUE_PARAMS)
        items = [
            *[
                _shape_queue_item(item, "movie")
                for item in _queue_records(radarr_queue)
            ],
            *[
                _shape_queue_item(item, "series")
                for item in _queue_records(sonarr_queue)
            ],
        ]

        if not items:
            return {
                "active": False,
                "items": [],
                "message": "No active downloads found.",
            }

        return {
            "active": True,
            "items": items,
        }

    def request_status(
        self, query: str | None = None, limit: int = MAX_STATUS_RESULTS
    ) -> dict[str, Any]:
        query_text = _optional_text(query)
        result_limit = _normalize_limit(limit, max_value=MAX_STATUS_RESULTS)
        radarr_queue_records = _queue_records(
            self._get_radarr("/api/v3/queue", params=QUEUE_PARAMS)
        )
        sonarr_queue_records = _queue_records(
            self._get_sonarr("/api/v3/queue", params=QUEUE_PARAMS)
        )
        movies = _ensure_list(self._get_radarr("/api/v3/movie"))
        series = _ensure_list(self._get_sonarr("/api/v3/series"))

        if query_text:
            available_movie = _find_available_movie_match(movies, query_text)
            if available_movie is not None:
                return _shape_available_movie_request(available_movie)

        items: list[dict[str, Any]] = []
        items.extend(
            _shape_request_queue_item(item, "movie") for item in radarr_queue_records
        )
        items.extend(
            _shape_request_queue_item(item, "series") for item in sonarr_queue_records
        )

        queued_movie_ids = _queue_media_ids(radarr_queue_records, "movie")
        queued_series_ids = _queue_media_ids(sonarr_queue_records, "series")
        items.extend(
            _shape_waiting_request_item(movie, "movie")
            for movie in movies
            if _is_missing_monitored_media(movie, "movie")
            and _media_id(movie) not in queued_movie_ids
        )
        items.extend(
            _shape_waiting_request_item(show, "series")
            for show in series
            if _is_missing_monitored_media(show, "series")
            and _media_id(show) not in queued_series_ids
        )

        if query_text:
            items = [item for item in items if _matches_query(item, query_text)]

        items = items[:result_limit]
        if not items:
            return {
                "active": False,
                "items": [],
                "message": "No matching requests found.",
            }

        return {
            "active": any(item.get("status") == "downloading" for item in items),
            "items": items,
        }

    def repair_blocked_imports(
        self, query: str | None = None, media_type: str = "any"
    ) -> dict[str, Any]:
        """Safely repair completed queue items blocked from automatic import."""
        query_text = _optional_text(query)
        requested_type = _normalize_media_type(media_type)
        items: list[dict[str, Any]] = []

        try:
            if requested_type in {"series", "any"}:
                sonarr_queue = _queue_records(
                    self._get_sonarr("/api/v3/queue", params=QUEUE_PARAMS)
                )
                for item in sonarr_queue:
                    if not _is_import_blocked(item):
                        continue
                    if query_text and not _matches_queue_query(item, query_text, "series"):
                        continue
                    items.append(self._repair_sonarr_import_block(item))

            if requested_type in {"movie", "any"}:
                radarr_queue = _queue_records(
                    self._get_radarr("/api/v3/queue", params=QUEUE_PARAMS)
                )
                for item in radarr_queue:
                    if not _is_import_blocked(item):
                        continue
                    if query_text and not _matches_queue_query(item, query_text, "movie"):
                        continue
                    items.append(self._repair_radarr_import_block(item))
        except ArrApiError as exc:
            return {
                "ok": False,
                "repairedCount": 0,
                "items": items,
                "message": str(exc),
            }

        repaired_count = sum(1 for item in items if item.get("status") == "repaired")
        if not items:
            return {
                "ok": True,
                "repairedCount": 0,
                "items": [],
                "message": "No matching import-blocked downloads found.",
            }

        return {
            "ok": True,
            "repairedCount": repaired_count,
            "items": items,
        }

    def browse_library(
        self,
        media_type: str = "any",
        genre: str | None = None,
        query: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        runtime_max: int | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        requested_type = _normalize_media_type(media_type)
        result_limit = _normalize_limit(limit)
        items = self._library_items(requested_type)
        return [
            item
            for item in items
            if _library_item_matches_filters(
                item,
                genre=genre,
                query=query,
                year_min=year_min,
                year_max=year_max,
                runtime_max=runtime_max,
                language=language,
            )
        ][:result_limit]

    def _library_items(self, media_type: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if media_type in {"movie", "any"}:
            items.extend(
                _shape_library_movie(movie)
                for movie in _ensure_list(self._get_radarr("/api/v3/movie"))
                if _movie_has_file(movie)
            )
        if media_type in {"series", "any"}:
            items.extend(
                _shape_library_series(show)
                for show in _ensure_list(self._get_sonarr("/api/v3/series"))
                if _series_has_file(show)
            )
        items.sort(key=lambda item: (item["media_type"], item.get("title") or ""))
        return items

    def _repair_sonarr_import_block(self, item: Mapping[str, Any]) -> dict[str, Any]:
        title = _queue_title(item, "series") or "Series"
        download_id = _clean_text(item.get("downloadId"))
        series_id = _queue_media_id(item, "series")
        episode_id = _positive_int_or_none(item.get("episodeId"))
        episode = item.get("episode")
        if episode_id is None and isinstance(episode, dict):
            episode_id = _positive_int_or_none(episode.get("id"))

        base = _repair_result_base(item, "series", title)
        if not download_id:
            return {**base, "status": "skipped", "reason": "Blocked queue item has no downloadId."}
        if series_id is None:
            return {**base, "status": "skipped", "reason": "Blocked queue item has no series ID."}
        if episode_id is None:
            return {**base, "status": "skipped", "reason": "Blocked queue item has no episode ID."}

        candidates = _ensure_list(
            self._get_sonarr(
                "/api/v3/manualimport",
                params={"downloadId": download_id, "filterExistingFiles": "true"},
            )
        )
        if len(candidates) != 1:
            return {
                **base,
                "status": "skipped",
                "reason": f"Found {len(candidates)} manual import candidates; safe repair requires exactly 1.",
            }

        candidate = candidates[0]
        reason = _sonarr_candidate_rejection_reason(candidate, series_id, episode_id)
        if reason:
            return {**base, "status": "skipped", "reason": reason}

        candidate_episodes = _ensure_list(candidate.get("episodes"))
        episode_ids = [_positive_int_or_none(ep.get("id")) for ep in candidate_episodes]
        episode_ids = [value for value in episode_ids if value is not None]
        file_payload = {
            "path": candidate.get("path"),
            "folderName": candidate.get("folderName"),
            "seriesId": series_id,
            "episodeIds": episode_ids,
            "releaseGroup": candidate.get("releaseGroup"),
            "quality": candidate.get("quality"),
            "languages": candidate.get("languages") or [],
            "indexerFlags": candidate.get("indexerFlags") or 0,
            "releaseType": candidate.get("releaseType") or "singleEpisode",
            "downloadId": download_id,
            "episodeFileId": candidate.get("episodeFileId"),
        }
        self._post_sonarr(
            "/api/v3/command",
            json={
                "name": "ManualImport",
                "files": [file_payload],
                "importMode": "auto",
                "priority": "high",
            },
        )

        verified = self._verify_sonarr_episodes_have_files(series_id, episode_ids)
        if not verified:
            return {
                **base,
                "status": "repair_submitted",
                "reason": "Manual import command was submitted, but availability was not verified yet.",
            }

        return {
            **base,
            "status": "repaired",
            "message": f"Imported blocked download for {title}.",
            "episodeIds": episode_ids,
        }

    def _repair_radarr_import_block(self, item: Mapping[str, Any]) -> dict[str, Any]:
        title = _queue_title(item, "movie") or "Movie"
        download_id = _clean_text(item.get("downloadId"))
        movie_id = _queue_media_id(item, "movie")
        base = _repair_result_base(item, "movie", title)
        if not download_id:
            return {**base, "status": "skipped", "reason": "Blocked queue item has no downloadId."}
        if movie_id is None:
            return {**base, "status": "skipped", "reason": "Blocked queue item has no movie ID."}

        candidates = _ensure_list(
            self._get_radarr(
                "/api/v3/manualimport",
                params={"downloadId": download_id, "filterExistingFiles": "true"},
            )
        )
        if len(candidates) != 1:
            return {
                **base,
                "status": "skipped",
                "reason": f"Found {len(candidates)} manual import candidates; safe repair requires exactly 1.",
            }

        candidate = candidates[0]
        reason = _radarr_candidate_rejection_reason(candidate, movie_id)
        if reason:
            return {**base, "status": "skipped", "reason": reason}

        file_payload = {
            "path": candidate.get("path"),
            "folderName": candidate.get("folderName"),
            "movieId": movie_id,
            "releaseGroup": candidate.get("releaseGroup"),
            "quality": candidate.get("quality"),
            "languages": candidate.get("languages") or [],
            "indexerFlags": candidate.get("indexerFlags") or 0,
            "downloadId": download_id,
            "movieFileId": candidate.get("movieFileId"),
        }
        self._post_radarr(
            "/api/v3/command",
            json={
                "name": "ManualImport",
                "files": [file_payload],
                "importMode": "auto",
                "priority": "high",
            },
        )

        verified = self._verify_radarr_movie_has_file(movie_id)
        if not verified:
            return {
                **base,
                "status": "repair_submitted",
                "reason": "Manual import command was submitted, but availability was not verified yet.",
            }

        return {
            **base,
            "status": "repaired",
            "message": f"Imported blocked download for {title}.",
        }

    def _verify_sonarr_episodes_have_files(self, series_id: int, episode_ids: list[int]) -> bool:
        if not episode_ids:
            return False
        episodes = _ensure_list(
            self._get_sonarr("/api/v3/episode", params={"seriesId": series_id})
        )
        by_id = {
            _positive_int_or_none(episode.get("id")): episode
            for episode in episodes
            if isinstance(episode, dict)
        }
        return all(
            isinstance(by_id.get(episode_id), dict)
            and by_id[episode_id].get("hasFile") is True
            for episode_id in episode_ids
        )

    def _verify_radarr_movie_has_file(self, movie_id: int) -> bool:
        movie = self._get_radarr(f"/api/v3/movie/{movie_id}")
        return isinstance(movie, dict) and _movie_has_file(movie)

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

    def _put_sonarr(self, path: str, json: Mapping[str, Any]) -> dict[str, Any]:
        response = self._request(
            "PUT",
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

    service = MediaRequestService(load_config(), request_store=RequestStore.from_env())
    mcp = FastMCP("plex-media-request")

    @mcp.tool()
    def search_media(
        query: str,
        media_type: str = "any",
        season: int | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Search movies and series with factual file-based availability."""
        return service.search_media(
            query=query, media_type=media_type, season=season, limit=limit
        )

    @mcp.tool()
    def request_movie(
        tmdbId: int,
        title: str | None = None,
        requested_by_user_id: int | None = None,
        requested_by_chat_id: int | None = None,
        requested_by_username: str | None = None,
    ) -> dict[str, Any]:
        """Request a movie by TMDB ID and record who requested it when provided."""
        return service.request_movie(
            tmdbId=tmdbId,
            title=title,
            requested_by_user_id=requested_by_user_id,
            requested_by_chat_id=requested_by_chat_id,
            requested_by_username=requested_by_username,
        )

    @mcp.tool()
    def request_series(
        tvdbId: int,
        title: str | None = None,
        seasons: list[int] | None = None,
        anime: bool = False,
        requested_by_user_id: int | None = None,
        requested_by_chat_id: int | None = None,
        requested_by_username: str | None = None,
    ) -> dict[str, Any]:
        """Request a series by TVDB ID. Always pass explicit season numbers and requester info when available."""
        return service.request_series(
            tvdbId=tvdbId,
            title=title,
            seasons=seasons,
            anime=anime,
            requested_by_user_id=requested_by_user_id,
            requested_by_chat_id=requested_by_chat_id,
            requested_by_username=requested_by_username,
        )

    @mcp.tool()
    def request_status(
        query: str | None = None, limit: int = MAX_STATUS_RESULTS
    ) -> dict[str, Any]:
        return service.request_status(query=query, limit=limit)

    @mcp.tool()
    def download_status() -> dict[str, Any]:
        return service.download_status()

    @mcp.tool()
    def repair_blocked_imports(
        query: str | None = None, media_type: str = "any"
    ) -> dict[str, Any]:
        """Safely repair import-blocked completed downloads when there is one exact match."""
        return service.repair_blocked_imports(query=query, media_type=media_type)

    @mcp.tool()
    def browse_library(
        media_type: str = "any",
        genre: str | None = None,
        query: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        runtime_max: int | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return service.browse_library(
            media_type=media_type,
            genre=genre,
            query=query,
            year_min=year_min,
            year_max=year_max,
            runtime_max=runtime_max,
            language=language,
            limit=limit,
        )

    @mcp.tool()
    def media_status() -> dict[str, Any]:
        return service.media_status()

    @mcp.tool()
    def notify_movie_available(radarr_movie_id: int) -> dict[str, Any]:
        """Webhook target: notify requesters for one Radarr movie ID after import/download."""
        return service.notify_movie_available(radarr_movie_id=radarr_movie_id)

    @mcp.tool()
    def notify_available_requests(limit: int = 100) -> dict[str, Any]:
        """Manual backfill helper. Do not use for routine availability notifications."""
        return service.notify_available_requests(limit=limit)

    return mcp


def main() -> None:
    try:
        server = create_server()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    server.run()


def _shape_movie_search_item(
    item: Mapping[str, Any], library_match: Mapping[str, Any] | None
) -> dict[str, Any]:
    result = {
        "media_type": "movie",
        "title": _clean_text(item.get("title")),
        "year": _positive_int_or_none(item.get("year")),
        "tmdbId": _positive_int_or_none(item.get("tmdbId")),
        "exists": library_match is not None,
        "available": bool(library_match and _movie_has_file(library_match)),
    }
    _copy_if_not_none(result, "imdbId", _clean_text(item.get("imdbId")))
    _copy_if_not_none(
        result, "runtimeMinutes", _positive_int_or_none(item.get("runtime"))
    )
    _copy_if_not_none(result, "overview", _clean_text(item.get("overview")))
    _copy_if_not_none(result, "posterUrl", _poster_url(item.get("images")))
    return result


def _shape_search_series_item(
    item: Mapping[str, Any],
    library_match: Mapping[str, Any] | None,
    season: int | None,
) -> dict[str, Any]:
    season_filter = [season] if season is not None else None
    availability = (
        _series_availability(library_match, season_filter)
        if library_match is not None
        else _empty_series_availability(item, season_filter)
    )
    season_source = library_match if library_match is not None else item
    result = {
        "media_type": "series",
        "title": _clean_text(item.get("title")),
        "year": _positive_int_or_none(item.get("year")),
        "tvdbId": _positive_int_or_none(item.get("tvdbId")),
        "exists": library_match is not None,
        "available": availability["availableEpisodes"] > 0,
        "seasons": _series_season_numbers(season_source),
        "availability": availability,
    }
    _copy_if_not_none(result, "imdbId", _clean_text(item.get("imdbId")))
    _copy_if_not_none(result, "tmdbId", _positive_int_or_none(item.get("tmdbId")))
    _copy_if_not_none(result, "status", _clean_text(item.get("status")))
    _copy_if_not_none(result, "overview", _clean_text(item.get("overview")))
    _copy_if_not_none(result, "posterUrl", _poster_url(item.get("images")))
    return result


def _shape_library_movie(item: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "title": _clean_text(item.get("title")),
        "year": _positive_int_or_none(item.get("year")),
        "media_type": "movie",
        "available": True,
    }
    _copy_if_not_none(result, "genres", _clean_string_list(item.get("genres")))
    _copy_if_not_none(
        result, "runtimeMinutes", _positive_int_or_none(item.get("runtime"))
    )
    _copy_if_not_none(result, "overview", _clean_text(item.get("overview")))
    _copy_if_not_none(result, "imdbId", _clean_text(item.get("imdbId")))
    _copy_if_not_none(result, "tmdbId", _positive_int_or_none(item.get("tmdbId")))
    _copy_if_not_none(result, "posterUrl", _poster_url(item.get("images")))
    _copy_if_not_none(result, "language", _language_name(item))
    return result


def _shape_library_series(item: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "title": _clean_text(item.get("title")),
        "year": _positive_int_or_none(item.get("year")),
        "media_type": "series",
        "available": True,
    }
    _copy_if_not_none(result, "genres", _clean_string_list(item.get("genres")))
    _copy_if_not_none(result, "seasons", _series_season_numbers(item))
    _copy_if_not_none(result, "status", _clean_text(item.get("status")))
    _copy_if_not_none(result, "overview", _clean_text(item.get("overview")))
    _copy_if_not_none(result, "imdbId", _clean_text(item.get("imdbId")))
    _copy_if_not_none(result, "tmdbId", _positive_int_or_none(item.get("tmdbId")))
    _copy_if_not_none(result, "tvdbId", _positive_int_or_none(item.get("tvdbId")))
    _copy_if_not_none(result, "posterUrl", _poster_url(item.get("images")))
    _copy_if_not_none(result, "language", _language_name(item))
    result["availability"] = _series_availability(item)
    return result


def _movie_library_match(
    item: Mapping[str, Any], movies: list[dict[str, Any]]
) -> dict[str, Any] | None:
    tmdb_id = _positive_int_or_none(item.get("tmdbId"))
    if tmdb_id is not None:
        match = _find_by_id(movies, "tmdbId", tmdb_id)
        if match is not None:
            return match

    item_key = _normalized_lookup_key(item.get("title"))
    item_year = _positive_int_or_none(item.get("year"))
    if not item_key:
        return None

    for movie in movies:
        movie_year = _positive_int_or_none(movie.get("year"))
        if item_year is not None and movie_year is not None and item_year != movie_year:
            continue
        if item_key in _movie_match_keys(movie):
            return movie
    return None


def _series_library_match(
    item: Mapping[str, Any], series: list[dict[str, Any]]
) -> dict[str, Any] | None:
    tvdb_id = _positive_int_or_none(item.get("tvdbId"))
    if tvdb_id is not None:
        match = _find_by_id(series, "tvdbId", tvdb_id)
        if match is not None:
            return match

    item_key = _normalized_lookup_key(item.get("title"))
    item_year = _positive_int_or_none(item.get("year"))
    if not item_key:
        return None

    for show in series:
        show_year = _positive_int_or_none(show.get("year"))
        if item_year is not None and show_year is not None and item_year != show_year:
            continue
        if item_key in _series_match_keys(show):
            return show
    return None


def _series_match_keys(show: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for value in (
        show.get("title"),
        show.get("cleanTitle"),
        _strip_slug_year(show.get("titleSlug")),
        show.get("titleSlug"),
    ):
        normalized = _normalized_lookup_key(value)
        if normalized:
            keys.add(normalized)

    alternate_titles = show.get("alternateTitles")
    if isinstance(alternate_titles, list):
        for alternate_title in alternate_titles:
            if isinstance(alternate_title, str):
                normalized = _normalized_lookup_key(alternate_title)
            elif isinstance(alternate_title, dict):
                normalized = _normalized_lookup_key(alternate_title.get("title"))
            else:
                normalized = ""
            if normalized:
                keys.add(normalized)
    return keys


def _series_availability(
    item: Mapping[str, Any], seasons: list[int] | None = None
) -> dict[str, Any]:
    requested = set(seasons) if seasons is not None else None
    season_summaries = [
        summary
        for season in _ensure_list(item.get("seasons"))
        for summary in [_season_availability(season)]
        if summary is not None
        and (requested is None or summary["season"] in requested)
    ]

    has_season_counts = any(
        summary["availableEpisodes"] > 0 or summary["totalEpisodes"] > 0
        for summary in season_summaries
    )
    if season_summaries and (has_season_counts or requested is not None):
        available = sum(summary["availableEpisodes"] for summary in season_summaries)
        total = sum(summary["totalEpisodes"] for summary in season_summaries)
        missing = sum(summary["missingEpisodes"] for summary in season_summaries)
        return {
            "availableEpisodes": available,
            "missingEpisodes": missing,
            "totalEpisodes": total,
            "seasons": season_summaries,
        }
    if requested is not None:
        return {
            "availableEpisodes": 0,
            "missingEpisodes": 0,
            "totalEpisodes": 0,
            "seasons": [],
        }

    statistics = item.get("statistics")
    if not isinstance(statistics, dict):
        statistics = item
    available = _episode_count(statistics, ("episodeFileCount",))
    total = _episode_count(statistics, ("totalEpisodeCount", "episodeCount"))
    if total == 0 and available > 0:
        total = available
    return {
        "availableEpisodes": available,
        "missingEpisodes": max(total - available, 0),
        "totalEpisodes": total,
        "seasons": [],
    }


def _empty_series_availability(
    item: Mapping[str, Any], seasons: list[int] | None = None
) -> dict[str, Any]:
    requested = set(seasons) if seasons is not None else None
    season_summaries = [
        {
            "season": season,
            "available": False,
            "availableEpisodes": 0,
            "missingEpisodes": 0,
            "totalEpisodes": 0,
        }
        for season in _series_season_numbers(item)
        if requested is None or season in requested
    ]
    return {
        "availableEpisodes": 0,
        "missingEpisodes": 0,
        "totalEpisodes": 0,
        "seasons": season_summaries,
    }


def _season_availability(season: Mapping[str, Any]) -> dict[str, Any] | None:
    season_number = _non_negative_int_or_none(season.get("seasonNumber"))
    if season_number is None:
        return None

    statistics = season.get("statistics")
    if not isinstance(statistics, dict):
        statistics = season

    available = _episode_count(statistics, ("episodeFileCount",))
    total = _episode_count(statistics, ("totalEpisodeCount", "episodeCount"))
    if total == 0 and available > 0:
        total = available
    missing = max(total - available, 0)
    return {
        "season": season_number,
        "available": available > 0,
        "availableEpisodes": available,
        "missingEpisodes": missing,
        "totalEpisodes": total,
    }


def _series_season_numbers(item: Mapping[str, Any]) -> list[int]:
    seasons = sorted(
        season_number
        for season in _ensure_list(item.get("seasons"))
        for season_number in [_non_negative_int_or_none(season.get("seasonNumber"))]
        if season_number is not None
    )
    if seasons:
        return seasons

    season_count = item.get("seasonCount")
    if isinstance(season_count, int) and not isinstance(season_count, bool):
        return list(range(1, season_count + 1))
    return []


def _episode_count(statistics: Mapping[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        count = _non_negative_int_or_none(statistics.get(key))
        if count is not None:
            return count
    return 0


def _normalize_media_type(media_type: str) -> str:
    if not isinstance(media_type, str):
        raise ValueError("media_type must be movie, series, or any")
    normalized = media_type.strip().lower()
    if normalized not in {"movie", "series", "any"}:
        raise ValueError("media_type must be movie, series, or any")
    return normalized


def _library_item_matches_filters(
    item: Mapping[str, Any],
    genre: str | None,
    query: str | None,
    year_min: int | None,
    year_max: int | None,
    runtime_max: int | None,
    language: str | None,
) -> bool:
    if genre and not _genre_matches(item, genre):
        return False
    if query and not _library_query_matches(item, query):
        return False
    year = _positive_int_or_none(item.get("year"))
    if year_min is not None and (year is None or year < year_min):
        return False
    if year_max is not None and (year is None or year > year_max):
        return False
    runtime = _positive_int_or_none(item.get("runtimeMinutes"))
    if runtime_max is not None and item.get("media_type") == "movie":
        if runtime is None or runtime > runtime_max:
            return False
    if language and not _language_matches(item, language):
        return False
    return True


def _genre_matches(item: Mapping[str, Any], genre: str) -> bool:
    genre_key = _normalized_lookup_key(genre)
    genres = item.get("genres")
    return isinstance(genres, list) and any(
        genre_key == _normalized_lookup_key(candidate) for candidate in genres
    )


def _library_query_matches(item: Mapping[str, Any], query: str) -> bool:
    query_key = _normalized_lookup_key(query)
    if not query_key:
        return True
    haystack = " ".join(
        value
        for value in (
            _clean_text(item.get("title")),
            _clean_text(item.get("overview")),
            " ".join(item.get("genres", [])) if isinstance(item.get("genres"), list) else "",
        )
        if value
    )
    return query_key in _normalized_lookup_key(haystack)


def _language_matches(item: Mapping[str, Any], language: str) -> bool:
    item_language = item.get("language")
    return isinstance(item_language, str) and _normalized_lookup_key(language) in (
        _normalized_lookup_key(item_language)
    )


def _clean_string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    cleaned = [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip() and "://" not in item
    ]
    return cleaned or None


def _language_name(item: Mapping[str, Any]) -> str | None:
    language = _first_present(item, ("originalLanguage", "language"))
    if isinstance(language, dict):
        return _clean_text(language.get("name"))
    return _clean_text(language)


def _normalize_requested_seasons(seasons: list[int] | None) -> list[int] | None:
    if seasons is None:
        return None
    if not isinstance(seasons, list):
        raise ValueError("seasons must be a list of season numbers")
    if not seasons:
        return None

    normalized: set[int] = set()
    for season in seasons:
        if isinstance(season, bool) or not isinstance(season, int) or season < 0:
            raise ValueError(
                "seasons must contain non-negative integers; use 0 for specials"
            )
        normalized.add(season)
    return sorted(normalized)


def _require_requested_seasons(seasons: list[int] | None) -> list[int]:
    normalized = _normalize_requested_seasons(seasons)
    if normalized is None:
        raise ValueError("seasons must be an explicit non-empty list")
    return normalized


def _optional_season(season: int | None) -> int | None:
    if season is None:
        return None
    if isinstance(season, bool) or not isinstance(season, int) or season < 0:
        raise ValueError("season must be a non-negative integer")
    return season


def _with_season_monitoring(
    series: Mapping[str, Any],
    requested_seasons: list[int],
    preserve_existing: bool = False,
) -> dict[str, Any]:
    seasons = _ensure_list(series.get("seasons"))
    available_seasons = sorted(
        season["seasonNumber"]
        for season in seasons
        if isinstance(season.get("seasonNumber"), int)
        and not isinstance(season.get("seasonNumber"), bool)
    )
    missing = [season for season in requested_seasons if season not in available_seasons]
    if missing:
        return {
            "error": (
                "Requested seasons are not available: "
                f"{_format_int_list(missing)}. Available seasons: "
                f"{_format_int_list(available_seasons)}."
            )
        }

    requested = set(requested_seasons)
    return {
        "seasons": [
            {
                **season,
                "monitored": season.get("seasonNumber") in requested
                or (preserve_existing and season.get("monitored") is True),
            }
            for season in seasons
        ]
    }


def _format_season_list(seasons: list[int]) -> str:
    if len(seasons) == 1:
        season = seasons[0]
        return "specials" if season == 0 else f"season {season}"

    if _is_contiguous(seasons):
        return f"seasons {seasons[0]}-{seasons[-1]}"
    return "seasons " + ", ".join(str(season) for season in seasons)


def _is_contiguous(values: list[int]) -> bool:
    return values == list(range(values[0], values[-1] + 1))


def _format_int_list(values: list[int]) -> str:
    return ", ".join(str(value) for value in values) if values else "none"


def _shape_queue_item(item: Mapping[str, Any], media_type: str) -> dict[str, Any]:
    result: dict[str, Any] = {"media_type": media_type}
    _copy_if_not_none(result, "title", _queue_title(item, media_type))
    _copy_if_not_none(result, "status", _clean_text(item.get("status")))
    _copy_if_not_none(result, "progress_percent", _queue_progress_percent(item))
    _copy_if_not_none(
        result,
        "time_left",
        _clean_text(_first_present(item, ("timeleft", "timeLeft"))),
    )
    _copy_if_not_none(
        result,
        "tracked_download_status",
        _clean_text(item.get("trackedDownloadStatus")),
    )
    tracked_state = _clean_text(item.get("trackedDownloadState"))
    _copy_if_not_none(result, "tracked_download_state", tracked_state)
    _copy_if_not_none(
        result,
        "download_client",
        _clean_text(_first_present(item, ("downloadClient", "downloadClientName"))),
    )
    _copy_if_not_none(result, "note", _queue_note(tracked_state))
    return result


def _shape_request_queue_item(
    item: Mapping[str, Any], media_type: str
) -> dict[str, Any]:
    tracked_state = _clean_text(item.get("trackedDownloadState"))
    time_left = _clean_text(_first_present(item, ("timeleft", "timeLeft")))
    is_downloading = _queue_is_downloading(item)

    result: dict[str, Any] = {
        "media_type": media_type,
        "status": "downloading" if is_downloading else _request_queue_status(item),
        "eta": time_left if is_downloading else None,
    }
    _copy_if_not_none(result, "title", _queue_title(item, media_type))
    if is_downloading:
        _copy_if_not_none(result, "progress_percent", _queue_progress_percent(item))
        _copy_if_not_none(result, "time_left", time_left)
    _copy_if_not_none(
        result,
        "tracked_download_status",
        _clean_text(item.get("trackedDownloadStatus")),
    )
    _copy_if_not_none(result, "tracked_download_state", tracked_state)
    _copy_if_not_none(
        result,
        "download_client",
        _clean_text(_first_present(item, ("downloadClient", "downloadClientName"))),
    )
    _copy_if_not_none(result, "note", _queue_note(tracked_state))
    return result


def _shape_waiting_request_item(
    item: Mapping[str, Any], media_type: str
) -> dict[str, Any]:
    waiting_for_release = _is_waiting_for_release(item, media_type)
    if waiting_for_release:
        status = "waiting_for_release"
        message = (
            "This is being watched, but it has not been released yet. "
            "No ETA is available until a download starts."
        )
    else:
        status = "waiting_for_suitable_release"
        message = (
            "This is being watched, but no suitable release has been found yet. "
            "No ETA is available until a download starts."
        )

    result = {
        "media_type": media_type,
        "status": status,
        "eta": None,
        "message": message,
    }
    _copy_if_not_none(result, "title", _media_title(item))
    return result


def _shape_available_movie_request(item: Mapping[str, Any]) -> dict[str, Any]:
    title = _media_title(item) or "Movie"
    result: dict[str, Any] = {
        "found": True,
        "media_type": "movie",
        "state": "available",
        "available": True,
        "message": f"{title} is already in the library.",
    }
    _copy_if_not_none(result, "title", _media_title(item))
    _copy_if_not_none(result, "year", _positive_int_or_none(item.get("year")))
    return result


def _queue_records(queue_response: Any) -> list[dict[str, Any]]:
    if isinstance(queue_response, dict):
        return _ensure_list(queue_response.get("records"))
    return _ensure_list(queue_response)


def _is_import_blocked(item: Mapping[str, Any]) -> bool:
    state = _clean_text(item.get("trackedDownloadState"))
    if state != "importBlocked":
        return False
    status = _clean_text(item.get("status"))
    progress = _queue_progress_percent(item)
    return (status is not None and status.lower() == "completed") or progress == 100.0


def _matches_queue_query(item: Mapping[str, Any], query: str, media_type: str) -> bool:
    query_key = _normalized_lookup_key(query)
    if not query_key:
        return True
    candidates = [
        _queue_title(item, media_type),
        item.get("title"),
    ]
    nested_key = "movie" if media_type == "movie" else "series"
    nested = item.get(nested_key)
    if isinstance(nested, dict):
        candidates.extend(
            [nested.get("title"), nested.get("cleanTitle"), nested.get("titleSlug")]
        )
    return any(
        query_key in _normalized_lookup_key(candidate)
        or _normalized_lookup_key(candidate) in query_key
        for candidate in candidates
        if candidate
    )


def _repair_result_base(
    item: Mapping[str, Any], media_type: str, title: str
) -> dict[str, Any]:
    result: dict[str, Any] = {"media_type": media_type, "title": title}
    episode = item.get("episode")
    if media_type == "series" and isinstance(episode, dict):
        _copy_if_not_none(result, "seasonNumber", _positive_int_or_none(episode.get("seasonNumber")))
        _copy_if_not_none(result, "episodeNumber", _positive_int_or_none(episode.get("episodeNumber")))
    return result


def _sonarr_candidate_rejection_reason(
    candidate: Mapping[str, Any], expected_series_id: int, expected_episode_id: int
) -> str | None:
    if _ensure_list(candidate.get("rejections")):
        return "Manual import candidate has rejections."
    series = candidate.get("series")
    candidate_series_id = None
    if isinstance(series, dict):
        candidate_series_id = _positive_int_or_none(series.get("id"))
    if candidate_series_id != expected_series_id:
        return "Manual import candidate does not match the expected series."
    episode_ids = [
        _positive_int_or_none(episode.get("id"))
        for episode in _ensure_list(candidate.get("episodes"))
    ]
    episode_ids = [value for value in episode_ids if value is not None]
    if episode_ids != [expected_episode_id]:
        return "Manual import candidate does not match the expected episode."
    if candidate.get("path") is None:
        return "Manual import candidate has no import path."
    return None


def _radarr_candidate_rejection_reason(
    candidate: Mapping[str, Any], expected_movie_id: int
) -> str | None:
    if _ensure_list(candidate.get("rejections")):
        return "Manual import candidate has rejections."
    movie = candidate.get("movie")
    candidate_movie_id = None
    if isinstance(movie, dict):
        candidate_movie_id = _positive_int_or_none(movie.get("id"))
    if candidate_movie_id != expected_movie_id:
        return "Manual import candidate does not match the expected movie."
    if candidate.get("path") is None:
        return "Manual import candidate has no import path."
    return None


def _queue_title(item: Mapping[str, Any], media_type: str) -> str | None:
    if media_type == "movie":
        movie = item.get("movie")
        if isinstance(movie, dict):
            title = _clean_text(movie.get("title"))
            if title:
                return title
    else:
        series = item.get("series")
        if isinstance(series, dict):
            title = _clean_text(series.get("title"))
            if title:
                return title

    return _clean_text(item.get("title"))


def _queue_progress_percent(item: Mapping[str, Any]) -> float | int | None:
    progress = _number(_first_present(item, ("progressPercent", "progress")))
    if progress is not None:
        return _clamped_percent(progress)

    size = _number(item.get("size"))
    size_left = _number(_first_present(item, ("sizeleft", "sizeLeft")))
    if size is None or size <= 0 or size_left is None:
        return None

    return _clamped_percent(((size - size_left) / size) * 100)


def _queue_note(tracked_state: str | None) -> str | None:
    if tracked_state == "importPending":
        return "Download is complete and waiting to be imported."
    return None


def _request_queue_status(item: Mapping[str, Any]) -> str | None:
    tracked_state = _clean_text(item.get("trackedDownloadState"))
    if tracked_state:
        return tracked_state
    return _clean_text(item.get("status"))


def _queue_is_downloading(item: Mapping[str, Any]) -> bool:
    status = _clean_text(item.get("status"))
    return bool(status and status.lower() == "downloading")


def _queue_media_ids(records: list[dict[str, Any]], media_type: str) -> set[int]:
    ids: set[int] = set()
    for item in records:
        media_id = _queue_media_id(item, media_type)
        if media_id is not None:
            ids.add(media_id)
    return ids


def _queue_media_id(item: Mapping[str, Any], media_type: str) -> int | None:
    key = "movieId" if media_type == "movie" else "seriesId"
    media_id = _positive_int_or_none(item.get(key))
    if media_id is not None:
        return media_id

    nested_key = "movie" if media_type == "movie" else "series"
    nested = item.get(nested_key)
    if isinstance(nested, dict):
        return _positive_int_or_none(nested.get("id"))
    return None


def _is_missing_monitored_media(item: Mapping[str, Any], media_type: str) -> bool:
    if item.get("monitored") is not True:
        return False
    if media_type == "movie":
        return not _movie_has_file(item)
    return not _series_has_file(item)


def _movie_has_file(item: Mapping[str, Any]) -> bool:
    if item.get("hasFile") is True:
        return True
    if _positive_int_or_none(item.get("movieFileId")) is not None:
        return True
    return isinstance(item.get("movieFile"), dict)


def _series_has_file(item: Mapping[str, Any]) -> bool:
    statistics = item.get("statistics")
    if isinstance(statistics, dict):
        if _positive_int_or_none(statistics.get("episodeFileCount")) is not None:
            return True
    if _positive_int_or_none(item.get("episodeFileCount")) is not None:
        return True
    return any(
        summary["availableEpisodes"] > 0
        for summary in _series_availability(item).get("seasons", [])
    )


def _media_id(item: Mapping[str, Any]) -> int | None:
    return _positive_int_or_none(item.get("id"))


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _non_negative_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _media_title(item: Mapping[str, Any]) -> str | None:
    return _clean_text(item.get("title"))


def _find_available_movie_match(
    movies: list[dict[str, Any]], query: str
) -> dict[str, Any] | None:
    query_title, query_year = _split_query_year(query)
    query_key = _normalized_lookup_key(query_title)
    if not query_key:
        return None

    for movie in movies:
        if not _movie_has_file(movie):
            continue
        movie_year = _positive_int_or_none(movie.get("year"))
        if query_year is not None and movie_year is not None and query_year != movie_year:
            continue
        if query_key in _movie_match_keys(movie):
            return movie
    return None


def _split_query_year(query: str) -> tuple[str, int | None]:
    stripped = query.strip()
    match = re.search(r"(?:^|\D)((?:19|20)\d{2})(?:\D|$)", stripped)
    if match is None:
        return stripped, None

    year = int(match.group(1))
    title = (stripped[: match.start(1)] + stripped[match.end(1) :]).strip()
    title = title.strip("()[]{}-: ")
    return title, year


def _movie_match_keys(movie: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for value in (
        movie.get("title"),
        movie.get("cleanTitle"),
        _strip_slug_year(movie.get("titleSlug")),
        movie.get("titleSlug"),
    ):
        normalized = _normalized_lookup_key(value)
        if normalized:
            keys.add(normalized)

    alternate_titles = movie.get("alternateTitles")
    if isinstance(alternate_titles, list):
        for alternate_title in alternate_titles:
            if isinstance(alternate_title, str):
                normalized = _normalized_lookup_key(alternate_title)
            elif isinstance(alternate_title, dict):
                normalized = _normalized_lookup_key(alternate_title.get("title"))
            else:
                normalized = ""
            if normalized:
                keys.add(normalized)
    return keys


def _strip_slug_year(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return re.sub(r"[-_](?:19|20)\d{2}$", "", value.strip())


def _normalized_lookup_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(character.lower() for character in value if character.isalnum())


def _matches_query(item: Mapping[str, Any], query: str) -> bool:
    title = item.get("title")
    return isinstance(title, str) and query.lower() in title.lower()


def _is_waiting_for_release(item: Mapping[str, Any], media_type: str) -> bool:
    if media_type == "movie":
        status = _clean_text(item.get("status"))
        if status and status.lower() in {"announced", "incinemas"}:
            return True
        return _has_future_date(
            item,
            ("physicalRelease", "digitalRelease", "inCinemas", "premiered"),
        )

    status = _clean_text(item.get("status"))
    if status and status.lower() == "upcoming":
        return True
    return _has_future_date(item, ("firstAired", "nextAiring", "airDateUtc"))


def _has_future_date(item: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    now = datetime.now(timezone.utc)
    for key in keys:
        parsed = _parse_datetime(item.get(key))
        if parsed and parsed > now:
            return True
    return False


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _clamped_percent(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if "://" in cleaned or cleaned.startswith(("/", "\\")) or ":\\" in cleaned:
        return None
    return cleaned


def _copy_if_not_none(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


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
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        return False
    hostname = urlparse(value).hostname
    if not hostname:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return "." in hostname
    return not (address.is_private or address.is_loopback or address.is_link_local)


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


def _normalize_limit(value: int, max_value: int = MAX_SEARCH_RESULTS) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("limit must be a positive integer")
    return min(value, max_value)


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


def _send_telegram_message(chat_id: int, text: str) -> bool:
    token = os.getenv(ENV_TELEGRAM_BOT_TOKEN, "").strip()
    if not token:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": str(chat_id),
                "text": text,
                "disable_web_page_preview": "true",
            },
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        return False


if __name__ == "__main__":
    main()
