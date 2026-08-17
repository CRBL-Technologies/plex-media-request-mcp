"""Public Plex watch links, resolved with one request per title.

``watch.plex.tv`` is the link to send. The Plex mobile apps register it as a
universal link, so it opens the app, and its Universal Details screen lists
every source for the title including this server. Reaching the file does cost
one extra tap there.

Do not "improve" that by sending ``app.plex.tv/desktop/#!/server/...`` instead.
It names the exact item, but it is a web-client route that no Plex app claims,
so it opens the browser client rather than the app -- which was tried, and is
worse. There is no https form that opens the app on a specific server item;
Plex tracks that as an open feature request. ``server_details_url`` therefore
stays a last resort for a title with no slug, where the alternative is no link.

A slug comes from Plex's metadata provider for a TMDB or TVDB id. Searching the
library and walking its results costs one call per candidate and answers a
different question -- whether the title sits in *this* library -- so it must not
be used to build a link. Availability is Radarr's and Sonarr's to report.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

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


def metadata_slug(metadata: Mapping[str, Any], kind: str) -> str | None:
    """Read the slug a Plex webhook payload already carries for its own item."""

    key = {"movie": "slug", "show": "slug", "season": "parentSlug", "episode": "grandparentSlug"}[
        kind
    ]
    return valid_slug(metadata.get(key))


def watch_slug(value: object) -> str | None:
    """Recover the slug from a link this module built, or None for any other."""

    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.hostname != "watch.plex.tv" or len(parts) < 2:
        return None
    if parts[0] not in {"movie", "show"}:
        return None
    return valid_slug(parts[1])


def server_details_url(*, machine_id: str, rating_key: str) -> str:
    """Name one item on this server, for when no slug exists.

    Opens Plex's browser client, not the app, so this is strictly the fallback
    -- see this module's docstring. It is still better than sending no link.
    """

    key = quote(f"/library/metadata/{rating_key}", safe="")
    return f"https://app.plex.tv/desktop/#!/server/{quote(machine_id, safe='')}/details?key={key}"


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
