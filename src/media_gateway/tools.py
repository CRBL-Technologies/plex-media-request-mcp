"""Seven stable shared tools plus a closed administrator proxy."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from .config import Config
from .constants import ADMIN_UPSTREAM_TOOLS, SHARED_TOOLS
from .store import Store
from .types import Actor, Role
from .upstream import Upstream, UpstreamError


class ToolError(ValueError):
    pass


def _object(value: object, *, name: str = "arguments") -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ToolError(f"{name} must be an object")
    return value


def _exact(value: object, allowed: set[str]) -> dict[str, Any]:
    result = _object(value)
    unknown = set(result) - allowed
    if unknown:
        raise ToolError(f"unknown argument: {sorted(unknown)[0]}")
    return result


def _positive(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolError(f"{name} must be a positive integer")
    return value


def _short_text(value: object, name: str, *, minimum: int = 1, maximum: int = 120) -> str:
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    result = value.strip()
    if not minimum <= len(result) <= maximum:
        raise ToolError(f"{name} has an invalid length")
    return result


def _rows(value: object) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _record(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if isinstance(data, dict):
        return data
    return value


def _interleave(groups: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Keep mixed searches useful instead of letting one provider fill the page."""

    result: list[dict[str, Any]] = []
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index < len(group):
                result.append(group[index])
                if len(result) == limit:
                    return result
    return result


def _first(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] is not None:
            return item[name]
    return None


def _year(value: object) -> int | None:
    if isinstance(value, int) and 1800 <= value <= 3000:
        return value
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        parsed = int(value[:4])
        return parsed if 1800 <= parsed <= 3000 else None
    return None


def _bool(value: object) -> bool:
    return value is True


def _poster_url(item: dict[str, Any]) -> str | None:
    """Return the provider's public poster URL, never its private relative path."""

    candidates: list[object] = [item.get("remotePoster")]
    images = item.get("images")
    if isinstance(images, list):
        candidates.extend(
            image.get("remoteUrl")
            for image in images
            if isinstance(image, dict) and image.get("coverType") == "poster"
        )
    for candidate in candidates:
        if not isinstance(candidate, str) or len(candidate) > 2048:
            continue
        parsed = urlsplit(candidate)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        ):
            return candidate
    return None


def _season_numbers(item: dict[str, Any]) -> list[int]:
    raw = item.get("seasons")
    if not isinstance(raw, list):
        return []
    result: list[int] = []
    for season in raw:
        if not isinstance(season, dict):
            continue
        number = _first(season, "seasonNumber", "season_number")
        if isinstance(number, int) and number > 0:
            result.append(number)
    return sorted(set(result))


def _movie_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
    external_id = _first(item, "tmdbId", "tmdb_id")
    title = _first(item, "title", "originalTitle")
    if not isinstance(external_id, int) or external_id <= 0 or not isinstance(title, str):
        return None
    radarr_id = item.get("id")
    return {
        "media_type": "movie",
        "tmdb_id": external_id,
        "title": title,
        "year": _year(_first(item, "year", "releaseDate", "inCinemas")),
        "overview": str(item.get("overview") or "")[:1000],
        "poster_url": _poster_url(item),
        "in_radarr": isinstance(radarr_id, int) and radarr_id > 0,
        "downloaded": _bool(item.get("hasFile")),
    }


def _series_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
    external_id = _first(item, "tvdbId", "tvdb_id")
    title = item.get("title")
    if not isinstance(external_id, int) or external_id <= 0 or not isinstance(title, str):
        return None
    sonarr_id = item.get("id")
    return {
        "media_type": "series",
        "tvdb_id": external_id,
        "title": title,
        "year": _year(_first(item, "year", "firstAired")),
        "overview": str(item.get("overview") or "")[:1000],
        "poster_url": _poster_url(item),
        "seasons": _season_numbers(item),
        "in_sonarr": isinstance(sonarr_id, int) and sonarr_id > 0,
        "status": str(item.get("status") or "unknown")[:40],
    }


SHARED_SCHEMAS: dict[str, dict[str, Any]] = {
    "search_media": {
        "description": (
            "Search Radarr and Sonarr for a movie or series. Results include poster_url "
            "when available. On Telegram, the media adapter presents up to four results in one "
            "tabbed poster card; opening a tab swaps its poster in place before the tool returns. "
            "Never use MEDIA for a remote poster URL, repeat the candidate list, or call clarify "
            "for the same results. Pressing Request movie performs the request through the "
            "gateway before the tool returns; when the result reports it already happened, only "
            "confirm the recorded outcome and never call request_movie for it. Choosing a series "
            "identifies it but still requires the desired seasons. Answer "
            "only about the returned result. If in_sonarr is false, say the series is not yet "
            "managed in Sonarr rather than saying episode availability was not reported."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2, "maxLength": 120},
                "media_type": {"type": "string", "enum": ["all", "movie", "series"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "request_movie": {
        "description": "Request one movie by its TMDB ID from a current search result.",
        "inputSchema": {
            "type": "object",
            "properties": {"tmdb_id": {"type": "integer", "minimum": 1}},
            "required": ["tmdb_id"],
            "additionalProperties": False,
        },
    },
    "request_series": {
        "description": "Request one or more seasons of a series by TVDB ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tvdb_id": {"type": "integer", "minimum": 1},
                "seasons": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 1,
                    "maxItems": 50,
                    "uniqueItems": True,
                },
                "anime": {"type": "boolean"},
            },
            "required": ["tvdb_id", "seasons"],
            "additionalProperties": False,
        },
    },
    "request_status": {
        "description": "Show this user's requests and their availability state.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    "download_status": {
        "description": "Show active Sonarr downloads and monitored missing movies.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    "browse_library": {
        "description": "Browse a Plex library with bounded results.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "library": {"type": "string", "maxLength": 100},
                "media_type": {"type": "string", "enum": ["movie", "show", "season", "episode"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
    },
    "media_status": {
        "description": "Summarize the configured Radarr, Sonarr, and Plex libraries.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
}


class ToolService:
    def __init__(self, config: Config, store: Store, upstream: Upstream):
        self.config = config
        self.store = store
        self.upstream = upstream

    async def tools_for(self, role: Role) -> list[dict[str, Any]]:
        if role is Role.BLOCKED:
            return []
        tools = [{"name": name, **SHARED_SCHEMAS[name]} for name in SHARED_TOOLS]
        if role is not Role.ADMIN:
            return tools
        discovered = await self.upstream.list_tools()
        by_name = {
            item["name"]: item
            for item in discovered
            if isinstance(item.get("name"), str) and item["name"] in ADMIN_UPSTREAM_TOOLS
        }
        return tools + [by_name[name] for name in sorted(by_name)]

    async def all_schemas(self) -> list[dict[str, Any]]:
        """Return the closed registration inventory to the trusted Hermes adapter."""

        discovered = await self.upstream.list_tools()
        by_name = {
            item["name"]: item
            for item in discovered
            if isinstance(item.get("name"), str) and item["name"] in ADMIN_UPSTREAM_TOOLS
        }
        missing = ADMIN_UPSTREAM_TOOLS - set(by_name)
        if missing:
            raise UpstreamError(f"pinned upstream is missing tool {sorted(missing)[0]}")
        shared = [
            {"name": name, **SHARED_SCHEMAS[name], "scope": "shared"} for name in SHARED_TOOLS
        ]
        admin = [{**by_name[name], "scope": "admin"} for name in sorted(by_name)]
        return shared + admin

    async def call(self, name: str, arguments: object, actor: Actor, role: Role) -> dict[str, Any]:
        if role is Role.BLOCKED:
            raise ToolError("user is not allowed")
        self.store.observe_actor(actor)
        if name in SHARED_TOOLS:
            handler = getattr(self, f"_{name}")
            result = await handler(arguments, actor, role)
            if not isinstance(result, dict):
                raise RuntimeError("shared tool returned an invalid result")
            return result
        if role is Role.ADMIN and name in ADMIN_UPSTREAM_TOOLS:
            return {"result": await self.upstream.call(name, _object(arguments))}
        raise ToolError("tool is not available for this user")

    async def _search_media(self, arguments: object, _actor: Actor, _role: Role) -> dict[str, Any]:
        args = _exact(arguments, {"query", "media_type", "limit"})
        query = _short_text(args.get("query"), "query", minimum=2)
        media_type = args.get("media_type", "all")
        if media_type not in {"all", "movie", "series"}:
            raise ToolError("media_type must be all, movie, or series")
        limit = args.get("limit", 8)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ToolError("limit must be between 1 and 10")
        calls: list[tuple[str, Any]] = []
        if media_type in {"all", "movie"}:
            calls.append(
                (
                    "movie",
                    self.upstream.call("radarr_search_movie", {"term": query, "limit": limit}),
                )
            )
        if media_type in {"all", "series"}:
            calls.append(
                (
                    "series",
                    self.upstream.call("sonarr_search_series", {"term": query, "limit": limit}),
                )
            )
        values = await asyncio.gather(*(call for _, call in calls), return_exceptions=True)
        groups: list[list[dict[str, Any]]] = []
        errors: list[str] = []
        for (kind, _), value in zip(calls, values, strict=True):
            if isinstance(value, BaseException):
                if not isinstance(value, Exception):
                    raise value
                errors.append(kind)
                continue
            mapper = _movie_candidate if kind == "movie" else _series_candidate
            groups.append(
                [candidate for item in _rows(value) if (candidate := mapper(item)) is not None]
            )
        results = _interleave(groups, limit)
        if not results and errors:
            raise UpstreamError("media search is temporarily unavailable")
        return {"query": query, "results": results, "unavailable_sources": errors}

    async def _request_movie(self, arguments: object, actor: Actor, _role: Role) -> dict[str, Any]:
        args = _exact(arguments, {"tmdb_id"})
        tmdb_id = _positive(args.get("tmdb_id"), "tmdb_id")
        lookup = await self.upstream.call(
            "radarr_search_movie", {"term": f"tmdb:{tmdb_id}", "limit": 10}
        )
        source = next(
            (item for item in _rows(lookup) if _first(item, "tmdbId", "tmdb_id") == tmdb_id),
            None,
        )
        if source is None:
            raise ToolError("TMDB ID was not found by Radarr")
        candidate = _movie_candidate(source)
        if candidate is None or candidate["year"] is None:
            raise ToolError("Radarr returned incomplete movie metadata")
        existing_id = source.get("id")
        try:
            visible_in_plex = await self._movie_in_plex(tmdb_id, candidate["title"])
        except UpstreamError:
            visible_in_plex = False
        request_id = self.store.begin_request(
            media_type="movie",
            external_id=tmdb_id,
            seasons=(),
            title=candidate["title"],
            year=candidate["year"],
            actor=actor,
        )
        try:
            action = await self._fulfill_movie_request(
                tmdb_id=tmdb_id,
                candidate=candidate,
                existing_id=existing_id,
                visible_in_plex=visible_in_plex,
            )
        except Exception:
            self.store.mark_request_unknown(request_id)
            raise
        self.store.complete_request(request_id, action)
        return {
            "request_id": request_id,
            "status": action,
            "movie": {
                "tmdb_id": tmdb_id,
                "title": candidate["title"],
                "year": candidate["year"],
            },
        }

    async def _fulfill_movie_request(
        self,
        *,
        tmdb_id: int,
        candidate: dict[str, Any],
        existing_id: object,
        visible_in_plex: bool,
    ) -> str:
        if visible_in_plex:
            return "available"
        if isinstance(existing_id, int) and existing_id > 0:
            if candidate["downloaded"]:
                return "awaiting_plex"
            await self.upstream.call("radarr_search_movie_releases", {"id": existing_id})
            return "search_started"
        await self.upstream.call(
            "radarr_add_movie",
            {
                "tmdbId": tmdb_id,
                "title": candidate["title"],
                "year": candidate["year"],
                "qualityProfileId": self.config.radarr_profile_id,
                "rootFolderPath": self.config.radarr_root,
                "minimumAvailability": "released",
                "monitored": True,
                "searchForMovie": True,
                "tags": list(self.config.radarr_tags),
            },
        )
        return "requested"

    async def _movie_in_plex(self, tmdb_id: int, title: str) -> bool:
        raw = await self.upstream.call(
            "plex_search", {"query": title, "limit": 20, "searchTypes": ["movies"]}
        )
        if not isinstance(raw, dict):
            raise UpstreamError("Plex search returned an invalid response")
        container = raw.get("MediaContainer")
        if not isinstance(container, dict) or not isinstance(container.get("Hub"), list):
            return False
        candidates: list[dict[str, Any]] = []
        for hub in container["Hub"]:
            if not isinstance(hub, dict) or not isinstance(hub.get("Metadata"), list):
                continue
            candidates.extend(item for item in hub["Metadata"] if isinstance(item, dict))
        for item in candidates[:20]:
            if item.get("type") != "movie":
                continue
            rating_key = item.get("ratingKey")
            if not isinstance(rating_key, str) or not rating_key:
                continue
            metadata = await self.upstream.call("plex_get_metadata", {"ratingKey": rating_key})
            for record in self._plex_rows(metadata, "Metadata"):
                guides = record.get("Guid")
                if not isinstance(guides, list):
                    continue
                for guide in guides:
                    value = guide.get("id") if isinstance(guide, dict) else None
                    if not isinstance(value, str) or not value.startswith("tmdb://"):
                        continue
                    raw_id = value.removeprefix("tmdb://").split("?", 1)[0]
                    if raw_id.isdigit() and int(raw_id) == tmdb_id:
                        return True
        return False

    async def _request_series(self, arguments: object, actor: Actor, _role: Role) -> dict[str, Any]:
        args = _exact(arguments, {"tvdb_id", "seasons", "anime"})
        tvdb_id = _positive(args.get("tvdb_id"), "tvdb_id")
        raw_seasons = args.get("seasons")
        if not isinstance(raw_seasons, list) or not raw_seasons or len(raw_seasons) > 50:
            raise ToolError("seasons must be a non-empty array")
        seasons = tuple(sorted({_positive(item, "season") for item in raw_seasons}))
        anime = args.get("anime", False)
        if not isinstance(anime, bool):
            raise ToolError("anime must be a boolean")
        lookup = await self.upstream.call(
            "sonarr_search_series", {"term": f"tvdb:{tvdb_id}", "limit": 10}
        )
        source = next(
            (item for item in _rows(lookup) if _first(item, "tvdbId", "tvdb_id") == tvdb_id),
            None,
        )
        if source is None:
            raise ToolError("TVDB ID was not found by Sonarr")
        candidate = _series_candidate(source)
        if candidate is None:
            raise ToolError("Sonarr returned incomplete series metadata")
        known_seasons = set(candidate["seasons"])
        missing = set(seasons) - known_seasons
        if known_seasons and missing:
            raise ToolError(f"season {min(missing)} does not exist for this series")
        existing_id = source.get("id")
        request_id = self.store.begin_request(
            media_type="series",
            external_id=tvdb_id,
            seasons=seasons,
            title=candidate["title"],
            year=candidate["year"],
            actor=actor,
            options={"anime": anime},
        )
        try:
            action = await self._fulfill_series_request(
                tvdb_id=tvdb_id,
                seasons=seasons,
                anime=anime,
                candidate=candidate,
                existing_id=existing_id,
                known_seasons=known_seasons,
            )
        except Exception:
            self.store.mark_request_unknown(request_id)
            raise
        self.store.complete_request(request_id, action)
        return {
            "request_id": request_id,
            "status": action,
            "series": {
                "tvdb_id": tvdb_id,
                "title": candidate["title"],
                "year": candidate["year"],
                "seasons": list(seasons),
            },
        }

    async def _fulfill_series_request(
        self,
        *,
        tvdb_id: int,
        seasons: tuple[int, ...],
        anime: bool,
        candidate: dict[str, Any],
        existing_id: object,
        known_seasons: set[int],
    ) -> str:
        if isinstance(existing_id, int) and existing_id > 0:
            current = _record(
                await self.upstream.call("sonarr_get_series_by_id", {"id": existing_id})
            )
            if current is None:
                raise ToolError("Sonarr returned incomplete series settings")
            updated_series = dict(current)
            options = current.get("seasons")
            if not isinstance(options, list):
                raise ToolError("Sonarr returned incomplete season settings")
            updated_options: list[dict[str, Any]] = []
            current_seasons: set[int] = set()
            for option in options:
                if not isinstance(option, dict):
                    continue
                updated = dict(option)
                number = _first(updated, "seasonNumber", "season_number")
                if isinstance(number, int):
                    current_seasons.add(number)
                    if number in seasons:
                        updated["monitored"] = True
                updated_options.append(updated)
            unavailable = set(seasons) - current_seasons
            if unavailable:
                raise ToolError(f"season {min(unavailable)} is unavailable in Sonarr")
            updated_series["seasons"] = updated_options
            await self.upstream.call(
                "sonarr_update_series", {"id": existing_id, "series": updated_series}
            )
            episode_ids: list[int] = []
            for season in seasons:
                episodes = await self.upstream.call(
                    "sonarr_get_episodes", {"seriesId": existing_id, "seasonNumber": season}
                )
                episode_ids.extend(
                    int(item["id"])
                    for item in _rows(episodes)
                    if isinstance(item.get("id"), int) and int(item["id"]) > 0
                )
            if episode_ids:
                await self.upstream.call(
                    "sonarr_update_episode_monitoring",
                    {"episodeIds": sorted(set(episode_ids)), "monitored": True},
                )
            for season in seasons:
                await self.upstream.call(
                    "sonarr_search_season", {"seriesId": existing_id, "seasonNumber": season}
                )
        else:
            season_options = [
                {"seasonNumber": number, "monitored": number in seasons}
                for number in sorted(known_seasons | set(seasons))
            ]
            await self.upstream.call(
                "sonarr_add_series",
                {
                    "tvdbId": tvdb_id,
                    "title": candidate["title"],
                    "qualityProfileId": (
                        self.config.sonarr_anime_profile_id
                        if anime
                        else self.config.sonarr_profile_id
                    ),
                    "rootFolderPath": self.config.sonarr_root,
                    "monitored": True,
                    "seasonFolder": True,
                    "seriesType": "anime" if anime else "standard",
                    "tags": list(self.config.sonarr_tags),
                    "seasons": season_options,
                    "searchForMissingEpisodes": True,
                },
            )
            return "requested"
        return "monitoring_updated"

    async def reconcile_pending_requests(
        self, *, updated_before: int | None = None
    ) -> dict[str, int]:
        """Repair request intents left pending or unknown by an interrupted operation."""

        return await self.reconcile_request_intents(
            self.store.pending_request_intents(updated_before=updated_before)
        )

    async def reconcile_request_intents(self, intents: list[dict[str, Any]]) -> dict[str, int]:
        """Repair an immutable snapshot of pending or unknown request intents."""

        repaired = 0
        unresolved = 0
        for intent in intents:
            request_id = int(intent["id"])
            try:
                if intent["media_type"] == "movie":
                    status = await self._reconcile_movie_intent(intent)
                else:
                    status = await self._reconcile_series_intent(intent)
            except Exception:
                self.store.mark_request_unknown(request_id)
                unresolved += 1
                continue
            self.store.complete_request(request_id, status, record_activity=False)
            repaired += 1
        return {"repaired": repaired, "unresolved": unresolved}

    async def _reconcile_movie_intent(self, intent: dict[str, Any]) -> str:
        tmdb_id = int(intent["external_id"])
        lookup = await self.upstream.call(
            "radarr_search_movie", {"term": f"tmdb:{tmdb_id}", "limit": 10}
        )
        source = next(
            (item for item in _rows(lookup) if _first(item, "tmdbId", "tmdb_id") == tmdb_id),
            None,
        )
        if source is None:
            raise ToolError("TMDB ID was not found while reconciling")
        candidate = _movie_candidate(source)
        if candidate is None or candidate["year"] is None:
            raise ToolError("Radarr returned incomplete movie metadata while reconciling")
        try:
            visible = await self._movie_in_plex(tmdb_id, candidate["title"])
        except UpstreamError:
            visible = False
        return await self._fulfill_movie_request(
            tmdb_id=tmdb_id,
            candidate=candidate,
            existing_id=source.get("id"),
            visible_in_plex=visible,
        )

    async def _reconcile_series_intent(self, intent: dict[str, Any]) -> str:
        tvdb_id = int(intent["external_id"])
        seasons = tuple(int(item) for item in intent["seasons"])
        anime = intent["options"].get("anime", False)
        if not isinstance(anime, bool):
            raise ToolError("stored series options are invalid")
        lookup = await self.upstream.call(
            "sonarr_search_series", {"term": f"tvdb:{tvdb_id}", "limit": 10}
        )
        source = next(
            (item for item in _rows(lookup) if _first(item, "tvdbId", "tvdb_id") == tvdb_id),
            None,
        )
        if source is None:
            raise ToolError("TVDB ID was not found while reconciling")
        candidate = _series_candidate(source)
        if candidate is None:
            raise ToolError("Sonarr returned incomplete series metadata while reconciling")
        known_seasons = set(candidate["seasons"])
        missing = set(seasons) - known_seasons
        if known_seasons and missing:
            raise ToolError("stored series season is unavailable while reconciling")
        return await self._fulfill_series_request(
            tvdb_id=tvdb_id,
            seasons=seasons,
            anime=anime,
            candidate=candidate,
            existing_id=source.get("id"),
            known_seasons=known_seasons,
        )

    async def _request_status(self, arguments: object, actor: Actor, role: Role) -> dict[str, Any]:
        _exact(arguments, set())
        return {"requests": self.store.requests_for(actor.user_id, all_users=role is Role.ADMIN)}

    async def _download_status(
        self, arguments: object, _actor: Actor, _role: Role
    ) -> dict[str, Any]:
        _exact(arguments, set())
        values = await asyncio.gather(
            self.upstream.radarr_queue(limit=50),
            self.upstream.call("sonarr_get_queue", {"limit": 50}),
            self.upstream.call(
                "radarr_get_movies",
                {"limit": 50, "filters": {"monitored": True, "hasFile": False}},
            ),
            return_exceptions=True,
        )
        unavailable: list[str] = []
        for name, value in zip(
            ("radarr_queue", "sonarr_queue", "missing_movies"), values, strict=True
        ):
            if isinstance(value, BaseException):
                if not isinstance(value, Exception):
                    raise value
                unavailable.append(name)
        if len(unavailable) == len(values):
            raise UpstreamError("download status is temporarily unavailable")
        movie_queue, series_queue, missing_movies = values
        return {
            "movie_downloads": (
                [self._queue_item(item) for item in movie_queue]
                if isinstance(movie_queue, list)
                else []
            ),
            "series_downloads": (
                [self._queue_item(item) for item in _rows(series_queue)]
                if not isinstance(series_queue, BaseException)
                else []
            ),
            "missing_movies": [
                candidate
                for item in _rows(missing_movies)
                if (candidate := _movie_candidate(item)) is not None
            ]
            if not isinstance(missing_movies, BaseException)
            else [],
            "unavailable_sources": unavailable,
        }

    @staticmethod
    def _queue_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": str(_first(item, "title", "seriesTitle") or "Unknown")[:200],
            "status": str(item.get("status") or "unknown")[:80],
            "size": item.get("size"),
            "size_left": _first(item, "sizeleft", "sizeLeft"),
            "time_left": _first(item, "timeleft", "timeLeft"),
        }

    async def _browse_library(
        self, arguments: object, _actor: Actor, _role: Role
    ) -> dict[str, Any]:
        args = _exact(arguments, {"library", "media_type", "limit"})
        library = args.get("library")
        if library is not None:
            library = _short_text(library, "library", maximum=100)
        media_type = args.get("media_type")
        type_ids = {"movie": 1, "show": 2, "season": 3, "episode": 4}
        if media_type is not None and media_type not in type_ids:
            raise ToolError("media_type is invalid")
        limit = args.get("limit", 25)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ToolError("limit must be between 1 and 50")
        libraries_raw = await self.upstream.call("plex_get_libraries", {})
        libraries = self._plex_rows(libraries_raw, "Directory")
        summaries = [
            {"key": str(item.get("key") or ""), "title": str(item.get("title") or "")[:100]}
            for item in libraries
            if item.get("key") is not None
        ]
        if library is None:
            return {"libraries": summaries, "items": []}
        selected = next(
            (
                item
                for item in summaries
                if item["key"] == library or item["title"].casefold() == library.casefold()
            ),
            None,
        )
        if selected is None:
            raise ToolError("Plex library was not found")
        call_args: dict[str, Any] = {"key": selected["key"], "size": limit}
        if media_type is not None:
            call_args["type"] = type_ids[media_type]
        raw_items = await self.upstream.call("plex_get_library_items", call_args)
        items = [self._plex_item(item) for item in self._plex_rows(raw_items, "Metadata")][:limit]
        return {"library": selected, "items": items}

    @staticmethod
    def _plex_rows(value: object, key: str) -> list[dict[str, Any]]:
        if not isinstance(value, dict):
            return []
        container = value.get("MediaContainer", value)
        if not isinstance(container, dict):
            return []
        rows = container.get(key, [])
        return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []

    @staticmethod
    def _plex_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "rating_key": str(_first(item, "ratingKey", "key") or "")[:80],
            "type": str(item.get("type") or "unknown")[:30],
            "title": str(item.get("title") or "Unknown")[:200],
            "year": _year(item.get("year")),
            "show_title": str(item.get("grandparentTitle") or "")[:200] or None,
            "season": item.get("parentIndex") if isinstance(item.get("parentIndex"), int) else None,
            "episode": item.get("index") if isinstance(item.get("index"), int) else None,
        }

    async def _media_status(self, arguments: object, _actor: Actor, _role: Role) -> dict[str, Any]:
        _exact(arguments, set())
        movies, series, libraries = await asyncio.gather(
            self.upstream.call("radarr_get_movies", {"limit": 1}),
            self.upstream.call("sonarr_get_series", {"limit": 1}),
            self.upstream.call("plex_get_libraries", {}),
        )
        return {
            "radarr_movies": self._total(movies),
            "sonarr_series": self._total(series),
            "plex_libraries": len(self._plex_rows(libraries, "Directory")),
        }

    @staticmethod
    def _total(value: object) -> int:
        if isinstance(value, dict) and isinstance(value.get("total"), int):
            return int(value["total"])
        return len(_rows(value))
