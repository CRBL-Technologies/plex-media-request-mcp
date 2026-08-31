"""Stable shared tools plus a closed administrator proxy."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from . import plex_watch
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


def _season_argument(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 1000:
        raise ToolError("season must be a non-negative integer")
    return value


def _short_text(value: object, name: str, *, minimum: int = 1, maximum: int = 120) -> str:
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    result = value.strip()
    if not minimum <= len(result) <= maximum:
        raise ToolError(f"{name} has an invalid length")
    return result


def _recommendation_target(value: str) -> tuple[str, int | None]:
    match = re.fullmatch(r"\s*(.*?)\s*(?:\((\d{4})\))?\s*", value)
    title = " ".join((match.group(1) if match else value).casefold().split())
    year = int(match.group(2)) if match and match.group(2) else None
    return title, year


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
    """Every season Sonarr knows, including 0 for specials."""

    raw = item.get("seasons")
    if not isinstance(raw, list):
        return []
    result: list[int] = []
    for season in raw:
        if not isinstance(season, dict):
            continue
        number = _first(season, "seasonNumber", "season_number")
        if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
            result.append(number)
    return sorted(set(result))


def _season_states(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-season episode counts, so a caller can tell complete from missing.

    ``monitored`` alone is not availability: a season can be monitored with no
    files at all, which means it was asked for and is still searching.
    """

    raw = item.get("seasons")
    if not isinstance(raw, list):
        return []
    states: list[dict[str, Any]] = []
    for season in raw:
        if not isinstance(season, dict):
            continue
        number = _first(season, "seasonNumber", "season_number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            continue
        stats = season.get("statistics")
        stats = stats if isinstance(stats, dict) else {}
        files = stats.get("episodeFileCount")
        # Sonarr's episodeCount is what has aired; totalEpisodeCount also
        # includes future episodes. Availability is complete when every aired
        # episode is held, not only after an ongoing season broadcasts its
        # finale.
        total = _first(stats, "episodeCount", "totalEpisodeCount")
        files = files if isinstance(files, int) and files >= 0 else 0
        total = total if isinstance(total, int) and total >= 0 else 0
        states.append(
            {
                "number": number,
                "files": files,
                "episodes": total,
                "monitored": season.get("monitored") is True,
                "complete": total > 0 and files >= total,
                "partial": 0 < files < total,
            }
        )
    return sorted(states, key=lambda state: int(state["number"]))


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
            "Search Radarr and Sonarr for one movie or series. Each match reports its year, "
            "media type, poster_url, and whether the file is held: downloaded for a movie, and "
            "for a series seasons_complete and seasons_missing, with downloaded meaning every "
            "aired season is present. A lone held title also carries plex_url. When exactly one "
            "title matches, it is posted to the chat as a poster."
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
    "recommend_media": {
        "description": (
            "Resolve one or several titles, when you already know which titles you want to "
            "check. Each is matched exactly -- include a year as 'Title (2016)' when known -- "
            "and returned with the same fields as search_media. Titles with no exact match come "
            "back in unmatched_titles rather than being guessed at. One matched title is posted "
            "to the chat as a poster; several titles are left for a conversational reply."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "titles": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 2, "maxLength": 120},
                    "minItems": 1,
                    "maxItems": 20,
                    "uniqueItems": True,
                },
                "media_type": {"type": "string", "enum": ["movie", "series", "all"]},
            },
            "required": ["titles", "media_type"],
            "additionalProperties": False,
        },
    },
    "request_movie": {
        "description": ("Request one movie by TMDB ID."),
        "inputSchema": {
            "type": "object",
            "properties": {"tmdb_id": {"type": "integer", "minimum": 1}},
            "required": ["tmdb_id"],
            "additionalProperties": False,
        },
    },
    "request_series": {
        "description": ("Request specific seasons of one series by TVDB ID. Season 0 is specials."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tvdb_id": {"type": "integer", "minimum": 1},
                "seasons": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
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
    "request_titles": {
        "description": (
            "Request every title in a list, up to a hundred at once. Each is matched exactly "
            "and then requested: a movie is added, a series is requested for every season "
            "except specials. A title repeated in the list is requested once. The result "
            "reports what was requested, what was already available, what could not be "
            "matched, what was a duplicate, and what failed, so nothing has to be asked "
            "mid-run."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "titles": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 2, "maxLength": 120},
                    "minItems": 1,
                    "maxItems": 100,
                },
                "media_type": {"type": "string", "enum": ["all", "movie", "series"]},
            },
            "required": ["titles"],
            "additionalProperties": False,
        },
    },
    "series_seasons": {
        "description": (
            "Report every season of a series with how many episodes Sonarr holds, so a reply "
            "can say which seasons are complete, partial or missing. Season 0 is specials. A "
            "search result carries no per-season counts, so this is the way to answer a season "
            "question."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"tvdb_id": {"type": "integer", "minimum": 1}},
            "required": ["tvdb_id"],
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

    async def _search_candidates(
        self, query: str, media_type: str, limit: int
    ) -> tuple[list[dict[str, Any]], list[str]]:
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
        # (provider kind, external id) -> the provider's own library id. TMDB
        # and TVDB occupy separate namespaces and can legitimately contain the
        # same integer, so the kind must remain part of the key.
        library_ids: dict[tuple[str, int], int] = {}
        for (kind, _), value in zip(calls, values, strict=True):
            if isinstance(value, BaseException):
                if not isinstance(value, Exception):
                    raise value
                errors.append(kind)
                continue
            mapper = _movie_candidate if kind == "movie" else _series_candidate
            group: list[dict[str, Any]] = []
            for item in _rows(value):
                candidate = mapper(item)
                if candidate is None:
                    continue
                provider_id = item.get("id")
                if isinstance(provider_id, int) and provider_id > 0:
                    key = "tmdb_id" if kind == "movie" else "tvdb_id"
                    library_ids[kind, candidate[key]] = provider_id
                group.append(candidate)
            groups.append(group)
        results = _interleave(groups, limit)
        await asyncio.gather(
            self._enrich_from_library(
                results=results,
                library_ids=library_ids,
                kind="movie",
                id_field="tmdb_id",
                tool="radarr_get_movie",
                apply=self._apply_movie_availability,
                # A movie the lookup already reports as held needs no read.
                skip=lambda candidate: bool(candidate.get("downloaded")),
            ),
            self._enrich_from_library(
                results=results,
                library_ids=library_ids,
                kind="series",
                id_field="tvdb_id",
                tool="sonarr_get_series_by_id",
                apply=self._apply_series_availability,
            ),
        )
        return results, errors

    async def _enrich_from_library(
        self,
        *,
        results: list[dict[str, Any]],
        library_ids: dict[tuple[str, int], int],
        kind: str,
        id_field: str,
        tool: str,
        apply: Callable[[dict[str, Any], dict[str, Any]], None],
        skip: Callable[[dict[str, Any]], bool] = lambda _candidate: False,
    ) -> None:
        """Read each tracked candidate's library record and fold it back in.

        Both providers answer "does this exist" from their lookup and "do we
        hold it" only from the library record, so both corrections share this
        shape: pick the tracked candidates, read their records concurrently,
        and never spend a call on a title the provider does not track.
        """

        targets: list[tuple[dict[str, Any], int]] = []
        for candidate in results:
            if candidate.get("media_type") != kind or skip(candidate):
                continue
            external_id = candidate.get(id_field)
            if not isinstance(external_id, int):
                continue
            provider_id = library_ids.get((kind, external_id))
            if provider_id is not None:
                targets.append((candidate, provider_id))
        if not targets:
            return

        async def resolve(candidate: dict[str, Any], provider_id: int) -> None:
            record = _record(await self.upstream.call(tool, {"id": provider_id}))
            if record is not None:
                apply(candidate, record)

        outcomes = await asyncio.gather(
            *(resolve(candidate, provider_id) for candidate, provider_id in targets),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                raise outcome

    @staticmethod
    def _apply_series_availability(candidate: dict[str, Any], record: dict[str, Any]) -> None:
        """Say which seasons a tracked series already has.

        Sonarr's lookup carries season numbers but null statistics, so a search
        result cannot tell a series that is fully held from one merely tracked.
        """

        states = _season_states(record)
        if not states:
            return
        complete = [int(state["number"]) for state in states if state["complete"]]
        # A season Sonarr lists with no episodes has not aired yet, so it is
        # not missing: there is nothing to acquire, and counting it would keep
        # every ongoing show permanently unavailable.
        missing = [
            int(state["number"])
            for state in states
            if not state["complete"] and int(state["episodes"]) > 0
        ]
        candidate["seasons_complete"] = complete
        candidate["seasons_missing"] = missing
        # "Downloaded" for a series means every aired season is complete, so
        # the model never calls a half-held show available.
        candidate["downloaded"] = bool(complete) and not missing

    @staticmethod
    def _apply_movie_availability(candidate: dict[str, Any], record: dict[str, Any]) -> None:
        """Correct ``downloaded`` from a movie's library record.

        Radarr's lookup answers "does this film exist", not "do we hold it": it
        returns the catalogue entry, where ``hasFile`` is null even for a film
        on disk. Reading it there reports every title as missing, which makes
        the bot offer to add films the user can already watch.
        """

        if _bool(record.get("hasFile")):
            candidate["downloaded"] = True

    async def _enrich_plex_urls(self, results: list[dict[str, Any]]) -> None:
        """Attach ``plex_url`` to a lone held result, at a cost of one call.

        Only a single-result card renders a link, so a multi-result set and a
        recommendation batch resolve nothing: enriching them would spend one
        request per title on a field no caller reads. Radarr and Sonarr report
        availability, so no Plex library traversal is involved -- the lookup
        turns an external id into a slug and nothing more.

        A series qualifies once any season is complete, because a link to a
        partly held show still opens something watchable.
        """

        if len(results) != 1:
            return
        candidate = results[0]
        media_type = candidate.get("media_type")
        if media_type == "movie":
            external_id = candidate.get("tmdb_id")
            held = bool(candidate.get("downloaded"))
        elif media_type == "series":
            external_id = candidate.get("tvdb_id")
            held = bool(candidate.get("seasons_complete"))
        else:
            return
        if not held or not isinstance(external_id, int) or external_id <= 0:
            return
        slug = await plex_watch.lookup_slug(
            token_file=self.config.upstream_token_file,
            media_type=media_type,
            external_id=external_id,
        )
        if slug is None:
            return
        # Deliberately the watch.plex.tv form: it opens the Plex app, where the
        # server appears as a source. See plex_watch's docstring.
        candidate["plex_url"] = plex_watch.watch_url(media_type=media_type, slug=slug)

    async def _search_media(self, arguments: object, _actor: Actor, _role: Role) -> dict[str, Any]:
        args = _exact(arguments, {"query", "media_type", "limit"})
        query = _short_text(args.get("query"), "query", minimum=2)
        media_type = args.get("media_type", "all")
        if media_type not in {"all", "movie", "series"}:
            raise ToolError("media_type must be all, movie, or series")
        limit = args.get("limit", 8)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ToolError("limit must be between 1 and 10")
        results, errors = await self._search_candidates(query, media_type, limit)
        if not results and errors:
            raise UpstreamError("media search is temporarily unavailable")
        await self._enrich_plex_urls(results)
        return {"query": query, "results": results, "unavailable_sources": errors}

    @staticmethod
    def _recommendation_choice(
        query: str,
        candidates: list[dict[str, Any]],
        seen: set[tuple[str, int]],
    ) -> dict[str, Any] | None:
        wanted_title, wanted_year = _recommendation_target(query)

        def identity(candidate: dict[str, Any]) -> tuple[str, int] | None:
            media_type = candidate.get("media_type")
            external_id = candidate.get("tmdb_id" if media_type == "movie" else "tvdb_id")
            if media_type not in {"movie", "series"} or not isinstance(external_id, int):
                return None
            return media_type, external_id

        def exact_match(candidate: dict[str, Any]) -> bool:
            title = " ".join(str(candidate.get("title") or "").casefold().split())
            return title == wanted_title and (
                wanted_year is None or candidate.get("year") == wanted_year
            )

        for candidate in candidates:
            key = identity(candidate)
            if exact_match(candidate) and key is not None and key not in seen:
                seen.add(key)
                return candidate
        return None

    async def _recommend_media(
        self, arguments: object, _actor: Actor, _role: Role
    ) -> dict[str, Any]:
        args = _exact(arguments, {"titles", "media_type"})
        raw_titles = args.get("titles")
        if not isinstance(raw_titles, list) or not 1 <= len(raw_titles) <= 20:
            raise ToolError("titles must contain 1 to 20 items")
        titles = [_short_text(item, "title", minimum=2) for item in raw_titles]
        if len({_recommendation_target(title)[0] for title in titles}) != len(titles):
            raise ToolError("titles must be distinct")
        media_type = args.get("media_type")
        if media_type not in {"all", "movie", "series"}:
            raise ToolError("media_type must be all, movie, or series")

        searches = await asyncio.gather(
            *(self._search_candidates(title, media_type, 5) for title in titles)
        )
        results: list[dict[str, Any]] = []
        errors: set[str] = set()
        seen: set[tuple[str, int]] = set()
        unmatched: list[str] = []
        for title, (candidates, unavailable) in zip(titles, searches, strict=True):
            errors.update(unavailable)
            choice = self._recommendation_choice(title, candidates, seen)
            if choice is not None:
                results.append(choice)
            else:
                # Radarr and Sonarr only match a title exactly, so a
                # mis-remembered or unreleased one resolves to nothing. Name it
                # rather than dropping it, or four suggestions silently become
                # three with no way to tell which went missing.
                unmatched.append(title)
        if not results and errors:
            raise UpstreamError("media recommendation lookup is temporarily unavailable")
        await self._enrich_plex_urls(results)
        return {
            "query": "recommendations",
            "requested_titles": titles,
            "unmatched_titles": unmatched,
            "results": results,
            "unavailable_sources": sorted(errors),
            "presentation": "recommendations",
        }

    async def _request_titles(self, arguments: object, actor: Actor, role: Role) -> dict[str, Any]:
        """Request a whole list at once, reporting rather than asking.

        Sending a list is a bulk instruction, not thirty separate questions, so
        every title is resolved by the same exact match a recommendation uses
        and requested straight away. Anything ambiguous or unknown is named in
        the result instead of interrupting the run.
        """

        args = _exact(arguments, {"titles", "media_type"})
        raw = args.get("titles")
        if not isinstance(raw, list) or not raw or len(raw) > 100:
            raise ToolError("titles must be an array of 1 to 100 items")
        asked = [_short_text(item, "title", minimum=2) for item in raw]
        # A repeated title must not become a second film. One `seen` set spans
        # the run so two different queries cannot claim the same match, which
        # means a duplicate falls through to the next candidate sharing that
        # title -- "Dune" twice would add both 2021 and 1984. Collapse them
        # here and report it, rather than rejecting a hundred-title paste over
        # one repeat.
        titles: list[str] = []
        duplicates: list[str] = []
        folded: set[str] = set()
        for title in asked:
            key = " ".join(title.casefold().split())
            (duplicates if key in folded else titles).append(title)
            folded.add(key)
        media_type = args.get("media_type", "all")
        if media_type not in {"all", "movie", "series"}:
            raise ToolError("media_type must be all, movie, or series")

        # Providers are shared with the rest of the household, so a long list
        # is walked a few at a time rather than opened all at once.
        gate = asyncio.Semaphore(4)
        seen: set[tuple[str, int]] = set()
        lock = asyncio.Lock()

        async def handle(title: str) -> dict[str, Any]:
            async with gate:
                try:
                    candidates, _errors = await self._search_candidates(title, media_type, 5)
                except UpstreamError:
                    return {"title": title, "state": "failed", "detail": "provider unavailable"}
                async with lock:
                    choice = self._recommendation_choice(title, candidates, seen)
                if choice is None:
                    return {"title": title, "state": "unmatched"}
                name = (
                    f"{choice['title']} ({choice['year']})"
                    if choice.get("year")
                    else choice["title"]
                )
                try:
                    if choice["media_type"] == "movie":
                        done = await self._request_movie(
                            {"tmdb_id": choice["tmdb_id"]}, actor, role
                        )
                    else:
                        # Season 0 is specials. Asking for a whole show does
                        # not mean asking for those, and a hundred-title list
                        # would pull them for every series in it. They stay
                        # available by naming them to request_series.
                        seasons = [
                            n for n in (choice.get("seasons") or []) if isinstance(n, int) and n > 0
                        ]
                        if not seasons:
                            return {"title": title, "matched": name, "state": "unmatched"}
                        done = await self._request_series(
                            {"tvdb_id": choice["tvdb_id"], "seasons": seasons}, actor, role
                        )
                except (ToolError, UpstreamError) as exc:
                    return {
                        "title": title,
                        "matched": name,
                        "state": "failed",
                        "detail": str(exc)[:120],
                    }
                status = str(done.get("status") or "requested")
                return {
                    "title": title,
                    "matched": name,
                    "media_type": choice["media_type"],
                    "state": "available" if status == "available" else "requested",
                    "status": status,
                }

        outcomes = await asyncio.gather(*(handle(title) for title in titles))
        buckets: dict[str, list[dict[str, Any]]] = {}
        for outcome in outcomes:
            buckets.setdefault(outcome["state"], []).append(outcome)
        return {
            "requested": buckets.get("requested", []),
            "already_available": buckets.get("available", []),
            "unmatched": [o["title"] for o in buckets.get("unmatched", [])],
            "failed": buckets.get("failed", []),
            "duplicates": duplicates,
            "counts": {
                "asked": len(asked),
                "duplicates": len(duplicates),
                "requested": len(buckets.get("requested", [])),
                "already_available": len(buckets.get("available", [])),
                "unmatched": len(buckets.get("unmatched", [])),
                "failed": len(buckets.get("failed", [])),
            },
        }

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
        held = await self._movie_is_held(existing_id)
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
                held=held,
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

    async def _movie_is_held(self, existing_id: object) -> bool:
        """Whether Radarr already holds the file for a tracked movie.

        Radarr's lookup answers "does this film exist", not "do we hold it": it
        returns the catalogue entry, where hasFile is null even for a film on
        disk. Only the library record carries the answer, and it is one call --
        the alternative, searching Plex and reading metadata per candidate,
        costs up to twenty-one and answers a question Radarr already can.
        """

        if not isinstance(existing_id, int) or existing_id <= 0:
            return False
        try:
            record = _record(await self.upstream.call("radarr_get_movie", {"id": existing_id}))
        except UpstreamError:
            return False
        return record is not None and _bool(record.get("hasFile"))

    async def _fulfill_movie_request(
        self,
        *,
        tmdb_id: int,
        candidate: dict[str, Any],
        existing_id: object,
        held: bool,
    ) -> str:
        if held:
            return "available"
        if isinstance(existing_id, int) and existing_id > 0:
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

    async def _series_seasons(
        self, arguments: object, _actor: Actor, _role: Role
    ) -> dict[str, Any]:
        args = _exact(arguments, {"tvdb_id"})
        tvdb_id = _positive(args.get("tvdb_id"), "tvdb_id")
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
        sonarr_id = source.get("id")
        record = source
        if isinstance(sonarr_id, int) and sonarr_id > 0:
            # The lookup response carries season numbers but leaves statistics
            # null, so episode counts need the tracked series itself.
            current = _record(
                await self.upstream.call("sonarr_get_series_by_id", {"id": sonarr_id})
            )
            if current is not None:
                record = current
        return {
            "tvdb_id": tvdb_id,
            "title": candidate["title"],
            "year": candidate["year"],
            "in_sonarr": isinstance(sonarr_id, int) and sonarr_id > 0,
            "seasons": _season_states(record),
        }

    async def _request_series(self, arguments: object, actor: Actor, _role: Role) -> dict[str, Any]:
        args = _exact(arguments, {"tvdb_id", "seasons", "anime"})
        tvdb_id = _positive(args.get("tvdb_id"), "tvdb_id")
        raw_seasons = args.get("seasons")
        if not isinstance(raw_seasons, list) or not raw_seasons or len(raw_seasons) > 50:
            raise ToolError("seasons must be a non-empty array")
        # Season 0 is the specials season, so a season number is non-negative
        # rather than positive.
        seasons = tuple(sorted({_season_argument(item) for item in raw_seasons}))
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
        return await self._fulfill_movie_request(
            tmdb_id=tmdb_id,
            candidate=candidate,
            existing_id=source.get("id"),
            held=await self._movie_is_held(source.get("id")),
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
