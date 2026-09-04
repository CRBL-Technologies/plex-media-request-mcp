"""Plex observations and Telegram availability notifications."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import defaultdict
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import plex_watch
from .config import Config
from .policy import Policy
from .secrets import read_dotenv
from .store import Store
from .types import Actor
from .upstream import Upstream

LOGGER = logging.getLogger(__name__)
# How often pending batches are re-evaluated. The quiet window is a threshold
# each pass tests rather than a timer that fires, so this cycle sets the floor
# on how soon an arrival can be announced.
FLUSH_INTERVAL_SECONDS = 5


def _positive(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _season_index(value: object) -> int | None:
    """A season number, where 0 is the specials season rather than absent.

    Episode numbers stay strictly positive; only seasons reach zero.
    """

    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _season_label(season: object) -> str:
    if season is None:
        return ""
    return "Specials" if season == 0 else f"Season {season}"


def _queue_rows(value: object) -> list[dict[str, Any]]:
    """Rows from a Sonarr queue response, whatever envelope it arrives in."""

    for candidate in (value, isinstance(value, dict) and value.get("data")):
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            records = candidate.get("records")
            if isinstance(records, list):
                return [item for item in records if isinstance(item, dict)]
    return []


def _provider_rows(value: object) -> list[dict[str, Any]]:
    """Records returned by the upstream Radarr and Sonarr lookup tools."""

    if isinstance(value, dict):
        value = value.get("data")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _public_poster_url(item: dict[str, Any]) -> str | None:
    """Select a provider-hosted HTTPS poster that Telegram can retrieve."""

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


def _external_id(metadata: dict[str, Any], provider: str) -> int | None:
    guides: list[str] = []
    raw = metadata.get("Guid")
    if isinstance(raw, list):
        guides.extend(str(item.get("id")) for item in raw if isinstance(item, dict))
    for key in ("guid", "grandparentGuid"):
        if isinstance(metadata.get(key), str):
            guides.append(metadata[key])
    prefix = f"{provider}://"
    for guide in guides:
        if guide.startswith(prefix):
            return _positive(guide.removeprefix(prefix).split("?", 1)[0])
    return None


def _guide_id(value: object, provider: str) -> int | None:
    if not isinstance(value, str):
        return None
    prefix = f"{provider}://"
    if not value.startswith(prefix):
        return None
    return _positive(value.removeprefix(prefix).split("?", 1)[0])


def _event_kind(event: dict[str, Any]) -> str:
    return str(event.get("event_key") or "").partition(":")[0]


def _series_identity(
    event: dict[str, Any], rating_to_external: dict[str, int]
) -> tuple[str, object]:
    """Name one show consistently across its show, season and episode events."""

    external_id = event.get("external_id")
    parent_key = event.get("parent_rating_key")
    rating_key = event.get("rating_key")
    if isinstance(external_id, bool) or not isinstance(external_id, int):
        if isinstance(parent_key, str):
            external_id = rating_to_external.get(parent_key)
        if (isinstance(external_id, bool) or not isinstance(external_id, int)) and isinstance(
            rating_key, str
        ):
            external_id = rating_to_external.get(rating_key)
    if isinstance(external_id, int) and not isinstance(external_id, bool):
        return "external", external_id
    if isinstance(parent_key, str) and parent_key:
        return "rating", parent_key
    if _event_kind(event) == "show" and isinstance(rating_key, str) and rating_key:
        return "rating", rating_key
    title = str(event.get("show_title") or event.get("title") or event.get("event_key") or "")
    return "title", " ".join(title.casefold().split())


def _group_pending_events(
    events: list[dict[str, Any]],
) -> dict[tuple[object, ...], list[dict[str, Any]]]:
    """Coalesce Plex's show, season and episode events into season batches.

    A newly discovered series normally emits a show event followed by season
    and episode events. Grouping the show before its season is known sends a
    redundant "new series" message beside the season notification. Use the
    parent rating key and provider ID to identify the show, then attach a
    seasonless show event to the first explicit season in the same pending
    window. A standalone show event remains its own notification.
    """

    rating_to_external: dict[str, int] = {}
    for event in events:
        if event.get("media_type") != "series":
            continue
        external_id = event.get("external_id")
        if isinstance(external_id, bool) or not isinstance(external_id, int):
            continue
        parent_key = event.get("parent_rating_key")
        if isinstance(parent_key, str) and parent_key:
            rating_to_external[parent_key] = external_id
        rating_key = event.get("rating_key")
        if _event_kind(event) == "show" and isinstance(rating_key, str) and rating_key:
            rating_to_external[rating_key] = external_id

    seasons: dict[tuple[str, object], set[int]] = defaultdict(set)
    for event in events:
        if event.get("media_type") != "series":
            continue
        season = event.get("season_number")
        if isinstance(season, int) and not isinstance(season, bool):
            seasons[_series_identity(event, rating_to_external)].add(season)

    grouped: dict[tuple[object, ...], list[dict[str, Any]]] = defaultdict(list)
    for source in events:
        event = dict(source)
        if event.get("media_type") != "series":
            grouped["movie", event["event_key"]].append(event)
            continue
        identity = _series_identity(event, rating_to_external)
        if identity[0] == "external" and (
            isinstance(event.get("external_id"), bool)
            or not isinstance(event.get("external_id"), int)
        ):
            # A sibling season/episode often carries the provider ID that the
            # top-level show webhook omitted. Share it inside this delivery
            # batch so requester matching does not depend on which event is
            # ordered first.
            event["external_id"] = identity[1]
        if event.get("season_number") is None and seasons[identity]:
            # With several seasons, attaching the one show event to the first
            # avoids a redundant general notification while keeping one batch
            # per season.
            ordinary = [season for season in seasons[identity] if season > 0]
            event["season_number"] = min(ordinary or seasons[identity])
        grouped["series", identity, event.get("season_number")].append(event)
    return grouped


def _is_single_episode(batch: list[dict[str, Any]]) -> bool:
    return (
        len(batch) == 1
        and batch[0].get("media_type") == "series"
        and _event_kind(batch[0]) == "episode"
        and isinstance(batch[0].get("episode_number"), int)
        and not isinstance(batch[0].get("episode_number"), bool)
    )


class Notifications:
    def __init__(self, config: Config, store: Store, policy: Policy, upstream: Upstream):
        self.config = config
        self.store = store
        self.policy = policy
        self.upstream = upstream
        self._stop = asyncio.Event()

    async def observe_plex(self, payload: object) -> bool:
        if not isinstance(payload, dict) or payload.get("event") != "library.new":
            return False
        metadata = payload.get("Metadata")
        if not isinstance(metadata, dict):
            return False
        kind = metadata.get("type")
        if kind not in {"movie", "show", "season", "episode"}:
            return False
        rating_key = str(metadata.get("ratingKey") or "").strip()
        if not rating_key or len(rating_key) > 100:
            return False
        external_id: int | None
        parent_key: str | None = None
        if kind == "movie":
            external_id = _external_id(metadata, "tmdb")
            show_title = None
            season = None
            episode = None
        elif kind == "show":
            external_id = _external_id(metadata, "tvdb")
            show_title = str(metadata.get("title") or "Untitled")[:300]
            season = None
            episode = None
        elif kind == "season":
            external_id = _guide_id(metadata.get("parentGuid"), "tvdb")
            raw_parent_key = metadata.get("parentRatingKey")
            parent_key = raw_parent_key if isinstance(raw_parent_key, str) else None
            show_title = (
                str(metadata.get("parentTitle"))[:300] if metadata.get("parentTitle") else None
            )
            season = _season_index(metadata.get("index"))
            episode = None
        else:
            external_id = _guide_id(metadata.get("grandparentGuid"), "tvdb")
            raw_parent_key = metadata.get("grandparentRatingKey")
            parent_key = raw_parent_key if isinstance(raw_parent_key, str) else None
            show_title = (
                str(metadata.get("grandparentTitle"))[:300]
                if metadata.get("grandparentTitle")
                else None
            )
            season = _season_index(metadata.get("parentIndex"))
            episode = _positive(metadata.get("index"))
        title = str(metadata.get("title") or "Untitled")[:300]
        # A watch.plex.tv link opens the Plex app; the server route below opens
        # the browser client instead, so it is only for a title with no slug.
        # See plex_watch's docstring before changing this preference.
        slug = plex_watch.metadata_slug(metadata, kind)
        if slug is not None:
            url = plex_watch.watch_url(
                media_type="movie" if kind == "movie" else "series",
                slug=slug,
                season_number=season,
                episode_number=episode,
            )
        else:
            url = plex_watch.server_details_url(
                machine_id=self.config.plex_machine_id, rating_key=rating_key
            )
        return self.store.add_media_event(
            event_key=f"{kind}:{rating_key}",
            media_type="movie" if kind == "movie" else "series",
            external_id=external_id,
            rating_key=rating_key,
            title=title,
            show_title=show_title,
            season_number=season,
            episode_number=episode,
            parent_rating_key=parent_key,
            plex_url=url,
        )

    @staticmethod
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

    async def run(self) -> None:
        if self.config.telegram_identity_sync:
            with suppress(Exception):
                await self.sync_policy_users()
        while not self._stop.is_set():
            try:
                await self.flush()
            except Exception:
                # Pending rows remain durable and are retried next cycle.
                LOGGER.exception("notification flush failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=FLUSH_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()

    async def sync_policy_users(self) -> None:
        snapshot = self.policy.snapshot()
        for user_id in sorted(snapshot.allowed):
            with suppress(Exception):
                await self.sync_user(user_id)

    async def sync_user(self, user_id: int) -> None:
        token = self._telegram_token()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/getChat", json={"chat_id": user_id}
                )
        except httpx.HTTPError:
            # HTTPX exception messages include the request URL. Telegram puts
            # the bot token in that URL, so replace the exception at this
            # boundary before a caller can log it.
            raise RuntimeError("Telegram identity lookup failed") from None
        if response.is_error:
            raise RuntimeError(f"Telegram identity lookup failed ({response.status_code})")
        value = response.json().get("result")
        if not isinstance(value, dict):
            raise RuntimeError("Telegram returned an invalid chat")
        self.store.observe_actor(
            Actor(
                user_id=user_id,
                chat_id=user_id,
                username=value.get("username") if isinstance(value.get("username"), str) else None,
                first_name=(
                    value.get("first_name") if isinstance(value.get("first_name"), str) else None
                ),
                last_name=(
                    value.get("last_name") if isinstance(value.get("last_name"), str) else None
                ),
            ),
            record_activity=False,
        )

    async def _series_still_arriving(self) -> set[tuple[str, object]] | None:
        """Series Sonarr is still fetching, or None when it cannot be asked.

        This can only ever hold a notification back, never release one. The
        queue empties when Sonarr finishes importing, which happens before Plex
        scans the files and emits the webhooks these events come from, so an
        empty queue is not evidence that an arrival is complete -- a season
        pack is a single queue item that becomes many webhooks after it drains.
        Waiting for quiet is what actually groups them.
        """

        try:
            raw = await self.upstream.call("sonarr_get_queue", {"limit": 50})
        except Exception:
            # Fail open: the quiet window has already elapsed, and a provider
            # that cannot be reached must not hold notifications for ever.
            LOGGER.warning("Sonarr queue is unavailable; delivering on quiet alone")
            return None
        identities: set[tuple[str, object]] = set()
        for item in _queue_rows(raw):
            series = item.get("series")
            if isinstance(series, dict):
                tvdb = _positive(series.get("tvdbId"))
                if tvdb is not None:
                    identities.add(("tvdb", tvdb))
                nested = series.get("title")
                if isinstance(nested, str) and nested.strip():
                    identities.add(("title", nested.strip().casefold()))
            for key in ("seriesTitle", "title"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    identities.add(("title", value.strip().casefold()))
        return identities

    @staticmethod
    def _matches_queue(event: dict[str, Any], identities: set[tuple[str, object]]) -> bool:
        external_id = event.get("external_id")
        if isinstance(external_id, int) and ("tvdb", external_id) in identities:
            return True
        title = event.get("show_title") or event.get("title")
        return isinstance(title, str) and ("title", title.strip().casefold()) in identities

    async def flush(self) -> None:
        now = int(time.time())
        before = now - self.config.notification_delay_seconds
        # Read young rows too: a season batch is delivered only after the
        # entire group has been quiet for the configured delay. Filtering in
        # SQL first would send early episodes while later ones were arriving.
        events = self.store.pending_media_events(now, limit=500)
        grouped = _group_pending_events(events)
        arriving: set[tuple[str, object]] | None = None
        asked_sonarr = False
        for key, batch in grouped.items():
            # The delay exists so a season import arrives as one message rather
            # than one per episode. A movie is its own batch and has nothing to
            # wait for, so making it sit out the window only delays the news.
            if key[0] == "series":
                if max(int(item["observed_at"]) for item in batch) > before:
                    continue
                # Quiet, but Sonarr may still be fetching the rest of the
                # season. One queue read serves every batch in this pass.
                if not asked_sonarr:
                    arriving = await self._series_still_arriving()
                    asked_sonarr = True
                if arriving and self._matches_queue(batch[0], arriving):
                    continue
            batch = await self._enrich(batch)
            await self._deliver_batch(batch)

    async def _enrich(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ready: list[dict[str, Any]] = []
        resolved_slugs: dict[tuple[str, int], str | None] = {}
        resolved_posters: dict[tuple[str, int], str | None] = {}
        for event in events:
            if event.get("external_id") is None:
                is_series = event.get("media_type") == "series"
                lookup_key = event.get("rating_key")
                if is_series and event.get("parent_rating_key"):
                    lookup_key = event.get("parent_rating_key")
                provider = "tvdb" if is_series else "tmdb"
                try:
                    if not isinstance(lookup_key, str) or not lookup_key:
                        raise ValueError("Plex metadata key is unavailable")
                    metadata = await self.upstream.call(
                        "plex_get_metadata", {"ratingKey": lookup_key}
                    )
                    candidates = (
                        self._metadata_objects(metadata) if isinstance(metadata, dict) else []
                    )
                    external_id = _external_id(candidates[0], provider) if candidates else None
                except Exception:
                    external_id = None
                if external_id is not None:
                    event["external_id"] = external_id
                    self.store.set_media_external_id(str(event["event_key"]), external_id)
            if (
                event.get("media_type") == "series"
                and event.get("season_number") is None
                and isinstance(event.get("external_id"), int)
            ):
                requested = self.store.requested_seasons(int(event["external_id"]))
                if len(requested) == 1:
                    # A first-time Plex show event represents the newly added
                    # series as a whole. Preserve the one requested season in
                    # the notification even though Plex omits it from the
                    # top-level webhook payload.
                    event["season_number"] = next(iter(requested))
            # Upgrade a stored server-route link once the id is known, because
            # only a watch.plex.tv link opens the Plex app.
            slug = plex_watch.watch_slug(event.get("plex_url"))
            external_id = event.get("external_id")
            media_type = str(event.get("media_type") or "")
            if slug is None and isinstance(external_id, int) and media_type in {"movie", "series"}:
                key = media_type, external_id
                if key not in resolved_slugs:
                    resolved_slugs[key] = await plex_watch.lookup_slug(
                        token_file=self.config.upstream_token_file,
                        media_type=media_type,
                        external_id=external_id,
                    )
                slug = resolved_slugs[key]
            if slug is not None and media_type in {"movie", "series"}:
                plex_url = plex_watch.watch_url(
                    media_type=media_type,
                    slug=slug,
                    season_number=event.get("season_number"),
                    episode_number=event.get("episode_number"),
                )
                if event.get("plex_url") != plex_url:
                    event["plex_url"] = plex_url
                    self.store.set_media_plex_url(str(event["event_key"]), plex_url)
            if isinstance(external_id, int) and media_type in {"movie", "series"}:
                poster_key = media_type, external_id
                if poster_key not in resolved_posters:
                    try:
                        resolved_posters[poster_key] = await self._poster_url(
                            media_type=media_type, external_id=external_id
                        )
                    except Exception:
                        # Artwork is optional. A provider outage must not hold
                        # back an otherwise valid availability notification.
                        LOGGER.warning("poster lookup failed for a Plex notification")
                        resolved_posters[poster_key] = None
                poster_url = resolved_posters[poster_key]
                if poster_url is not None:
                    event["poster_url"] = poster_url
            ready.append(event)
        if _is_single_episode(ready):
            ready[0]["completed_season_size"] = await self._completed_season_size(ready[0])
        return ready

    async def _poster_url(self, *, media_type: str, external_id: int) -> str | None:
        if media_type == "movie":
            response = await self.upstream.call(
                "radarr_search_movie", {"term": f"tmdb:{external_id}", "limit": 10}
            )
            id_fields = ("tmdbId", "tmdb_id")
        else:
            response = await self.upstream.call(
                "sonarr_search_series", {"term": f"tvdb:{external_id}", "limit": 10}
            )
            id_fields = ("tvdbId", "tvdb_id")
        for item in _provider_rows(response):
            if any(_positive(item.get(field)) == external_id for field in id_fields):
                return _public_poster_url(item)
        return None

    async def _completed_season_size(self, event: dict[str, Any]) -> int | None:
        """Return a finished season's episode count, zero, or None on lookup failure."""

        external_id = event.get("external_id")
        season_number = event.get("season_number")
        if (
            not isinstance(external_id, int)
            or isinstance(external_id, bool)
            or not isinstance(season_number, int)
            or isinstance(season_number, bool)
        ):
            return 0
        try:
            lookup = await self.upstream.call(
                "sonarr_search_series", {"term": f"tvdb:{external_id}", "limit": 10}
            )
            source = next(
                (
                    item
                    for item in _provider_rows(lookup)
                    if _positive(item.get("tvdbId")) == external_id
                    or _positive(item.get("tvdb_id")) == external_id
                ),
                None,
            )
            sonarr_id = _positive(source.get("id")) if source is not None else None
            if sonarr_id is None:
                return 0
            raw = await self.upstream.call("sonarr_get_series_by_id", {"id": sonarr_id})
        except Exception:
            # Do not permanently discard a possible finale during a transient
            # Sonarr failure. The durable event will be checked again.
            LOGGER.warning("season completion lookup failed for a Plex notification")
            return None
        record = raw.get("data", raw) if isinstance(raw, dict) else None
        seasons = record.get("seasons") if isinstance(record, dict) else None
        if not isinstance(seasons, list):
            return 0
        for season in seasons:
            if not isinstance(season, dict) or season.get("seasonNumber") != season_number:
                continue
            stats = season.get("statistics")
            if not isinstance(stats, dict):
                return 0
            files = _positive(stats.get("episodeFileCount"))
            aired = _positive(stats.get("episodeCount"))
            total = _positive(stats.get("totalEpisodeCount"))
            next_airing = stats.get("nextAiring")
            if (
                files is not None
                and aired is not None
                and total is not None
                and files >= total
                and aired >= total
                and not next_airing
            ):
                return total
            return 0
        return 0

    async def _deliver_batch(self, batch: list[dict[str, Any]]) -> None:
        first = batch[0]
        keys = [str(item["event_key"]) for item in batch]
        policy = self.policy.snapshot()
        requester_destinations = self.store.request_destinations(
            media_type=str(first["media_type"]),
            external_id=first["external_id"],
            season_number=first["season_number"],
        )
        # A removed user keeps historical request state for audit, but must no
        # longer receive messages. Filter by trusted requester identity while
        # preserving the original private or group chat destination.
        recipients = {
            chat_id for user_id, chat_id in requester_destinations if user_id in policy.allowed
        }
        lone_episode = _is_single_episode(batch)
        completed_season_size = first.get("completed_season_size")
        season_completed = (
            lone_episode and isinstance(completed_season_size, int) and completed_season_size > 0
        )
        completion_unknown = lone_episode and completed_season_size is None
        # Administrators receive every movie and every show/season batch. A
        # lone weekly episode is requester-only unless it completes a season;
        # an administrator who asked for that season is already present through
        # requester_destinations.
        if not lone_episode or season_completed:
            recipients.update(policy.admins)
        if not recipients:
            # A known lone episode nobody requested is intentionally ignored,
            # not retried every five seconds forever. An unresolved event or a
            # failed finale check stays pending until its identity and season
            # state can be established.
            if first["external_id"] is not None and not completion_unknown:
                self.store.mark_events_notified(keys)
            return
        for chat_id in recipients:
            pending = [
                item
                for item in batch
                if not self.store.delivered([str(item["event_key"])], chat_id)
            ]
            if not pending:
                continue
            poster_url = pending[0].get("poster_url")
            message = self._message(pending)
            plex_url = str(pending[0]["plex_url"])
            if isinstance(poster_url, str):
                await self._send(chat_id, message, plex_url, poster_url=poster_url)
            else:
                await self._send(chat_id, message, plex_url)
            self.store.mark_delivered([str(item["event_key"]) for item in pending], chat_id)
        # Keep an unresolved episode durable after notifying administrators.
        # Once Plex exposes its show TVDB ID, the requester can still be found
        # and notified without sending the administrator a duplicate.
        if first["external_id"] is None or completion_unknown:
            return
        if first["media_type"] == "movie" and isinstance(first["external_id"], int):
            self.store.mark_movie_available(int(first["external_id"]))
        self.store.mark_events_notified(keys)

    @staticmethod
    def _message(batch: list[dict[str, Any]]) -> str:
        first = batch[0]
        if first["media_type"] == "movie":
            return f"🍿 <b>Available in Plex</b>\n{html.escape(str(first['title']))}"
        show = html.escape(str(first["show_title"] or first["title"]))
        season = first["season_number"]
        episodes = [item for item in batch if item["episode_number"] is not None]
        if not episodes:
            label = _season_label(season) or "New series"
            return f"📺 <b>Available in Plex</b>\n{show} · {label}"
        if len(episodes) > 1:
            label = _season_label(season) or "New episodes"
            return f"📺 <b>Available in Plex</b>\n{show} · {label} ({len(episodes)} episodes)"
        if len(batch) > 1:
            label = _season_label(season) or "New series"
            return f"📺 <b>Available in Plex</b>\n{show} · {label}"
        episode = first["episode_number"]
        completed_season_size = first.get("completed_season_size")
        if isinstance(completed_season_size, int) and completed_season_size > 0:
            label = _season_label(season) or "Season"
            marker = ""
            if season is not None and episode is not None:
                marker = f"S{int(season):02d}E{int(episode):02d} · "
            title = html.escape(str(first["title"]))
            return (
                f"📺 <b>Season complete in Plex</b>\n"
                f"{show} · {label} ({completed_season_size} episodes)\n"
                f"Finale: {marker}{title}"
            )
        marker = ""
        if season is not None and episode is not None:
            marker = f" · S{int(season):02d}E{int(episode):02d}"
        title = html.escape(str(first["title"]))
        return f"📺 <b>Available in Plex</b>\n{show}{marker} · {title}"

    async def _send(
        self, chat_id: int, text: str, plex_url: str, *, poster_url: str | None = None
    ) -> None:
        token = self._telegram_token()
        markup = {"inline_keyboard": [[{"text": "Open in Plex", "url": plex_url}]]}
        message_body = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": markup,
        }
        photo_body = {
            "chat_id": chat_id,
            "photo": poster_url,
            "caption": text,
            "parse_mode": "HTML",
            "reply_markup": markup,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                if poster_url is not None:
                    response = await client.post(
                        f"https://api.telegram.org/bot{token}/sendPhoto", json=photo_body
                    )
                    if response.status_code == 400:
                        # A stale provider image must not prevent delivery.
                        response = await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json=message_body,
                        )
                else:
                    response = await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage", json=message_body
                    )
        except httpx.HTTPError:
            # Do not let a network exception copy the token-bearing request
            # URL into the worker log.
            raise RuntimeError("Telegram notification request failed") from None
        if response.is_error:
            # Do not raise HTTPStatusError: its message includes the bot token
            # embedded in the request URL.
            raise RuntimeError(f"Telegram notification failed ({response.status_code})")

    def _telegram_token(self) -> str:
        values = read_dotenv(self.config.policy_file, {"TELEGRAM_BOT_TOKEN"})
        token = values.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("Telegram bot token is missing")
        return token
