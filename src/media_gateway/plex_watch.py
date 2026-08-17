"""Public Plex watch links, resolved with one request per title.

A ``watch.plex.tv`` link needs the canonical slug for a TMDB or TVDB id, which
Plex's metadata provider answers directly. Searching the library and walking
its results costs one call per candidate and answers a different question --
whether the title sits in *this* library -- so it must not be used to build a
link. Availability is Radarr's and Sonarr's to report; this module only turns
an id into a URL.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import httpx

from .secrets import read_dotenv

PLEX_METADATA_MATCH_URL = "https://metadata.provider.plex.tv/library/metadata/matches"
PLEX_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
LOOKUP_TIMEOUT_SECONDS = 8
CACHE_TTL_SECONDS = 3600
CACHE_MAX_ENTRIES = 256

# (media_type, external_id) -> (expires_at, slug). Slugs are stable, so a short
# TTL is enough to keep a card tap or a notification batch to one request.
_cache: dict[tuple[str, int], tuple[float, str | None]] = {}


def valid_slug(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 200 or PLEX_SLUG.fullmatch(value) is None:
        return None
    return value


def watch_url(
    *,
    media_type: str,
    slug: str,
    season_number: object = None,
    episode_number: object = None,
) -> str:
    if media_type == "movie":
        return f"https://watch.plex.tv/movie/{slug}"
    result = f"https://watch.plex.tv/show/{slug}"
    if isinstance(season_number, int) and season_number > 0:
        result += f"/season/{season_number}"
        if isinstance(episode_number, int) and episode_number > 0:
            result += f"/episode/{episode_number}"
    return result


def _metadata_objects(value: dict[str, Any]) -> list[dict[str, Any]]:
    container = value.get("MediaContainer", value)
    if not isinstance(container, dict):
        return []
    raw = container.get("Metadata")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        return [raw]
    return [container]


def _remember(key: tuple[str, int], slug: str | None) -> str | None:
    if len(_cache) >= CACHE_MAX_ENTRIES:
        _cache.clear()
    _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, slug)
    return slug


def clear_cache() -> None:
    _cache.clear()


async def lookup_slug(*, token_file: Path, media_type: str, external_id: int) -> str | None:
    """Resolve one title's Plex slug, or None when Plex does not know it.

    A miss is cached too: a title Plex cannot match will not start answering on
    the next tap, and re-asking would add latency to every card.
    """

    key = (media_type, external_id)
    cached = _cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    try:
        token = read_dotenv(token_file, {"PLEX_API_KEY"}).get("PLEX_API_KEY")
    except (OSError, ValueError):
        return None
    if not token:
        return None
    provider = "tmdb" if media_type == "movie" else "tvdb"
    try:
        async with httpx.AsyncClient(timeout=LOOKUP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                PLEX_METADATA_MATCH_URL,
                params={
                    "guid": f"{provider}://{external_id}",
                    "type": 1 if media_type == "movie" else 2,
                },
                headers={"X-Plex-Token": token, "Accept": "application/json"},
            )
    except httpx.HTTPError:
        # Not cached: a transport failure says nothing about the title.
        return None
    if response.is_error:
        return None
    try:
        value = response.json()
    except ValueError:
        return None
    candidates = _metadata_objects(value) if isinstance(value, dict) else []
    return _remember(key, valid_slug(candidates[0].get("slug")) if candidates else None)
