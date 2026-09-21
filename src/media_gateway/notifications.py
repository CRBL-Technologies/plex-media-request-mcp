"""Plex observations and Telegram availability notifications."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import time
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import httpx

from . import plex_watch
from .config import Config
from .episode_facts import season_episode_facts, season_is_finished
from .policy import Policy
from .provider_metadata import public_poster_url
from .secrets import read_dotenv
from .store import Store
from .types import Actor
from .upstream import Upstream

LOGGER = logging.getLogger(__name__)
# How often pending batches are re-evaluated. The quiet window is a threshold
# each pass tests rather than a timer that fires, so this cycle sets the floor
# on how soon an arrival can be announced.
FLUSH_INTERVAL_SECONDS = 5
PRUNE_INTERVAL_SECONDS = 60 * 60
MAX_RETRY_BACKOFF_SECONDS = 15 * 60


class _TelegramSendError(RuntimeError):
    def __init__(
        self, status_code: int | None, *, retryable: bool, retry_after_seconds: int | None = None
    ):
        detail = f" ({status_code})" if status_code is not None else ""
        super().__init__(f"Telegram notification failed{detail}")
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


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


def _request_cycle_ids(cycles: list[dict[str, Any]]) -> set[tuple[int, int, int]]:
    return {
        (int(cycle["request_id"]), int(cycle["generation"]), int(cycle["chat_id"]))
        for cycle in cycles
    }


def _group_pending_events(
    events: list[dict[str, Any]],
) -> dict[tuple[object, ...], list[dict[str, Any]]]:
    """Group expanded sibling episodes while keeping unrelated shows apart."""

    parent_to_external: dict[str, int] = {}
    for event in events:
        external_id = event.get("external_id")
        parent_key = event.get("parent_rating_key")
        if (
            event.get("media_type") == "series"
            and isinstance(external_id, int)
            and not isinstance(external_id, bool)
            and isinstance(parent_key, str)
            and parent_key
        ):
            parent_to_external[parent_key] = external_id

    grouped: dict[tuple[object, ...], list[dict[str, Any]]] = defaultdict(list)
    for source in events:
        event = dict(source)
        if event.get("media_type") != "series":
            grouped["movie", event["event_key"]].append(event)
            continue
        external_id = event.get("external_id")
        parent_key = event.get("parent_rating_key")
        if (
            (isinstance(external_id, bool) or not isinstance(external_id, int))
            and isinstance(parent_key, str)
            and parent_key in parent_to_external
        ):
            external_id = parent_to_external[parent_key]
            event["external_id"] = external_id
        if isinstance(external_id, int) and not isinstance(external_id, bool):
            identity: tuple[str, object] = ("external", external_id)
        elif isinstance(parent_key, str) and parent_key:
            identity = ("rating", parent_key)
        else:
            title = str(event.get("show_title") or event.get("title") or "")
            identity = ("title", " ".join(title.casefold().split()))
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
        self._retry_state: dict[tuple[int, tuple[str, ...]], tuple[int, float]] = {}
        self._next_prune_at = time.monotonic() + PRUNE_INTERVAL_SECONDS

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

    async def run(self) -> None:
        if self.config.telegram_identity_sync:
            with suppress(Exception):
                await self.sync_policy_users()
        while not self._stop.is_set():
            if time.monotonic() >= self._next_prune_at:
                try:
                    self.store.prune()
                except Exception:
                    LOGGER.exception("periodic state pruning failed")
                finally:
                    self._next_prune_at = time.monotonic() + PRUNE_INTERVAL_SECONDS
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
        for event in events:
            if _event_kind(event) in {"show", "season"}:
                await self._expand_container(event)
        events = [
            item
            for item in self.store.pending_media_events(now, limit=500)
            if _event_kind(item) not in {"show", "season"}
        ]
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
            request_cycles: list[dict[str, Any]] | None = None
            original_external_id = batch[0].get("external_id")
            original_season = batch[0].get("season_number")
            if isinstance(original_external_id, int) and (
                key[0] == "movie" or isinstance(original_season, int)
            ):
                request_cycles = self.store.request_cycles(
                    media_type=str(batch[0]["media_type"]),
                    external_id=original_external_id,
                    season_number=original_season,
                )
                if key[0] == "series":
                    pending_cycle = any(
                        cycle["state"] != "available"
                        and original_season not in cycle["fulfilled_seasons"]
                        for cycle in request_cycles
                    )
                    for event in batch:
                        event["request_cycle_pending"] = pending_cycle
            batch = await self._enrich(batch)
            if request_cycles is not None:
                fresh_cycles = self.store.request_cycles(
                    media_type=str(batch[0]["media_type"]),
                    external_id=int(batch[0]["external_id"]),
                    season_number=batch[0]["season_number"],
                )
                if _request_cycle_ids(fresh_cycles) != _request_cycle_ids(request_cycles):
                    continue
            if batch[0].get("season_import_pending"):
                continue
            if (
                key[0] == "series"
                and request_cycles is None
                and original_external_id is None
                and isinstance(batch[0].get("external_id"), int)
            ):
                # The identity was learned across an await. Persist it now and
                # capture request generations before the next verification pass.
                continue
            await self._deliver_batch(batch, request_cycles=request_cycles)

    async def _expand_container(self, event: dict[str, Any]) -> None:
        """Turn a show/season observation into actual Plex episode observations."""

        await self._identify(event)
        if event.get("external_id") is None:
            return
        try:
            episodes = await self.upstream.plex_episodes(str(event["rating_key"]))
        except Exception:
            LOGGER.warning("Plex container episodes are unavailable; will retry")
            return
        represented: set[int] = set()
        for item in episodes:
            season = _season_index(item.get("parentIndex"))
            episode = _positive(item.get("index"))
            key = item.get("ratingKey")
            if (
                item.get("type") != "episode"
                or not item.get("Media")
                or not isinstance(key, str)
                or not key
                or season is None
                or episode is None
            ):
                continue
            self.store.add_media_event(
                event_key=f"episode:{key}",
                media_type="series",
                external_id=event.get("external_id"),
                rating_key=key,
                title=str(item.get("title") or f"Episode {episode}"),
                show_title=str(
                    item.get("grandparentTitle") or event.get("show_title") or event["title"]
                ),
                season_number=season,
                episode_number=episode,
                parent_rating_key=item.get("grandparentRatingKey")
                or event.get("parent_rating_key")
                or (event["rating_key"] if _event_kind(event) == "show" else None),
                plex_url=plex_watch.server_details_url(
                    machine_id=self.config.plex_machine_id, rating_key=key
                ),
                observed_at=int(event["observed_at"]),
            )
            represented.add(season)
        requested = self.store.requested_seasons(event["external_id"])
        if _event_kind(event) == "season":
            requested &= {event.get("season_number")}
        # Each requested season needs an episode to drive readiness retries.
        # A show webhook can arrive while only an older season is visible.
        if represented and requested.issubset(represented):
            self.store.mark_events_notified([str(event["event_key"])])

    async def _identify(self, event: dict[str, Any]) -> None:
        if event.get("external_id") is not None:
            return
        is_series = event.get("media_type") == "series"
        lookup_key = event.get("rating_key")
        if is_series and event.get("parent_rating_key"):
            lookup_key = event["parent_rating_key"]
        try:
            if not isinstance(lookup_key, str) or not lookup_key:
                return
            metadata = await self.upstream.call("plex_get_metadata", {"ratingKey": lookup_key})
            candidates = plex_watch.metadata_objects(metadata) if isinstance(metadata, dict) else []
            external_id = (
                _external_id(candidates[0], "tvdb" if is_series else "tmdb") if candidates else None
            )
        except Exception:
            return
        if external_id is not None:
            event["external_id"] = external_id
            self.store.set_media_external_id(str(event["event_key"]), external_id)

    async def _enrich(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ready: list[dict[str, Any]] = []
        resolved_slugs: dict[tuple[str, int], str | None] = {}
        resolved_posters: dict[tuple[str, int], str | None] = {}
        records: dict[tuple[str, int], dict[str, Any]] = {}
        for event in events:
            await self._identify(event)
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
                        records[poster_key] = await self._provider_record(
                            media_type=media_type, external_id=external_id
                        )
                        resolved_posters[poster_key] = public_poster_url(records[poster_key])
                    except Exception:
                        # Artwork is optional. A provider outage must not hold
                        # back an otherwise valid availability notification.
                        LOGGER.warning("poster lookup failed for a Plex notification")
                        resolved_posters[poster_key] = None
                poster_url = resolved_posters[poster_key]
                if poster_url is not None:
                    event["poster_url"] = poster_url
            ready.append(event)
        if ready and ready[0]["media_type"] == "series":
            source = next((item for item in ready if _event_kind(item) == "episode"), ready[0])
            external_id = source.get("external_id")
            record = records.get(("series", external_id)) if isinstance(external_id, int) else None
            ready[0]["completed_season_size"] = await self._completed_season_size(source, record)
            if isinstance(source.get("season_import_id"), str):
                ready[0]["season_import_id"] = source["season_import_id"]
            ready[0]["season_import_pending"] = source.get("season_import_pending", False) or (
                ready[0]["completed_season_size"] is None
                and isinstance(external_id, int)
                and source.get("season_number") in self.store.requested_seasons(external_id)
            )
        return ready

    async def _provider_record(self, *, media_type: str, external_id: int) -> dict[str, Any]:
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
                return item
        return {}

    async def _completed_season_size(
        self, event: dict[str, Any], source: dict[str, Any] | None = None
    ) -> int | None:
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
            if source is None:
                source = await self._provider_record(media_type="series", external_id=external_id)
            sonarr_id = _positive(source.get("id")) if source is not None else None
            if sonarr_id is None:
                return None
            raw = await self.upstream.call("sonarr_get_series_by_id", {"id": sonarr_id})
        except Exception:
            # Do not permanently discard a possible finale during a transient
            # Sonarr failure. The durable event will be checked again.
            LOGGER.warning("season completion lookup failed for a Plex notification")
            return None
        record = raw.get("data", raw) if isinstance(raw, dict) else None
        seasons = record.get("seasons") if isinstance(record, dict) else None
        if not isinstance(record, dict) or not isinstance(seasons, list):
            return None
        for season in seasons:
            if not isinstance(season, dict) or season.get("seasonNumber") != season_number:
                continue
            try:
                raw = await self.upstream.call(
                    "sonarr_get_episodes", {"seriesId": sonarr_id, "seasonNumber": season_number}
                )
                episodes = raw.get("data", raw) if isinstance(raw, dict) else raw
                if not isinstance(episodes, list) or not episodes:
                    return None
                facts = season_episode_facts(episodes, season_number, now=datetime.now(UTC))
                if facts is None or facts.total == 0:
                    return None
                expected = {
                    int(episode["episodeNumber"])
                    for episode in episodes
                    if episode.get("seasonNumber") == season_number
                }
                if not season_is_finished(
                    facts,
                    season_number=season_number,
                    series_status=record.get("status"),
                    has_later_season=any(
                        isinstance(item, dict)
                        and isinstance(item.get("seasonNumber"), int)
                        and item["seasonNumber"] > season_number
                        for item in seasons
                    ),
                ):
                    # Weekly releases remain episode notifications.
                    return 0
                requested = event.get("request_cycle_pending")
                if not isinstance(requested, bool):
                    requested = season_number in self.store.requested_seasons(external_id)
                if not facts.all_have_files:
                    event["season_import_pending"] = requested
                    return 0
                key = str(event["rating_key"])
                if _event_kind(event) == "episode":
                    metadata = await self.upstream.call("plex_get_metadata", {"ratingKey": key})
                    objects = plex_watch.metadata_objects(metadata)
                    key = str(objects[0]["parentRatingKey"])
                plex_episodes = await self.upstream.plex_episodes(key)
                visible: dict[int, str | None] = {}
                for item in plex_episodes:
                    index = _positive(item.get("index"))
                    if (
                        item.get("type") != "episode"
                        or item.get("parentIndex") != season_number
                        or not item.get("Media")
                        or index is None
                    ):
                        continue
                    rating_key = item.get("ratingKey")
                    visible[index] = (
                        rating_key if isinstance(rating_key, str) and rating_key else None
                    )
                if not expected.issubset(visible):
                    event["season_import_pending"] = requested
                    return None
                if all(visible[number] is not None for number in expected):
                    identity = "|".join(
                        f"{number}:{visible[number]}" for number in sorted(expected)
                    )
                    event["season_import_id"] = hashlib.sha256(identity.encode()).hexdigest()[:16]
                return facts.total
            except Exception:
                LOGGER.warning("season episode verification is unavailable; will retry")
                return None
        return None

    async def _verified_selected_seasons(
        self, event: dict[str, Any], request_cycles: list[dict[str, Any]]
    ) -> tuple[set[int], bool]:
        """Verify selected older seasons when this show's new season completes."""

        current = event.get("season_number")
        external_id = event.get("external_id")
        if not isinstance(current, int) or not isinstance(external_id, int):
            return set(), False
        verified = {current}
        outstanding = {
            season
            for cycle in request_cycles
            if cycle["state"] != "available"
            for season in cycle["seasons"]
            if isinstance(season, int)
            and not isinstance(season, bool)
            and season not in cycle["fulfilled_seasons"]
            and season != current
        }
        show_key = event.get("parent_rating_key")
        if not outstanding or not isinstance(show_key, str) or not show_key:
            return verified, bool(outstanding)
        try:
            source = await self._provider_record(media_type="series", external_id=external_id)
        except Exception:
            return verified, True
        for season in sorted(outstanding):
            probe = {
                **event,
                "event_key": f"show:{show_key}",
                "rating_key": show_key,
                "season_number": season,
                "episode_number": None,
                "request_cycle_pending": True,
            }
            size = await self._completed_season_size(probe, source)
            if size is None:
                return verified, True
            if size > 0:
                verified.add(season)
        return verified, False

    async def _deliver_batch(
        self,
        batch: list[dict[str, Any]],
        *,
        request_cycles: list[dict[str, Any]] | None = None,
    ) -> None:
        first = batch[0]
        keys = [str(item["event_key"]) for item in batch]
        policy = self.policy.snapshot()
        lone_episode = _is_single_episode(batch)
        completed_season_size = first.get("completed_season_size")
        season_completed = isinstance(completed_season_size, int) and completed_season_size > 0
        season_verification_pending = False
        external_id = first.get("external_id")
        season_number = first.get("season_number")
        captured_cycle_ids: set[tuple[int, int, int]] = set()
        track_request_cycles = isinstance(external_id, int) and (
            first["media_type"] == "movie" or isinstance(season_number, int)
        )
        if track_request_cycles:
            assert isinstance(external_id, int)
            if request_cycles is None:
                request_cycles = self.store.request_cycles(
                    media_type=str(first["media_type"]),
                    external_id=external_id,
                    season_number=season_number,
                )
            captured_cycle_ids = _request_cycle_ids(request_cycles)
            fresh_cycles = self.store.request_cycles(
                media_type=str(first["media_type"]),
                external_id=external_id,
                season_number=season_number,
            )
            if _request_cycle_ids(fresh_cycles) != captured_cycle_ids:
                return
            if season_completed:
                verified, season_verification_pending = await self._verified_selected_seasons(
                    first, request_cycles
                )
                fresh_cycles = self.store.request_cycles(
                    media_type=str(first["media_type"]),
                    external_id=external_id,
                    season_number=season_number,
                )
                if _request_cycle_ids(fresh_cycles) != captured_cycle_ids:
                    return
                self.store.mark_series_seasons_available(request_cycles, verified)
            requester_destinations = {
                (int(cycle["user_id"]), int(cycle["chat_id"])) for cycle in request_cycles
            }
        else:
            request_cycles = []
            requester_destinations = self.store.request_destinations(
                media_type=str(first["media_type"]),
                external_id=external_id,
                season_number=season_number,
            )
        # A removed user keeps historical request state for audit, but must no
        # longer receive messages. Filter by trusted requester identity while
        # preserving the original private or group chat destination.
        recipients = {
            chat_id for user_id, chat_id in requester_destinations if user_id in policy.allowed
        }
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
        deferred = False
        for chat_id in recipients:
            completion_keys: list[str] = []
            if season_completed and isinstance(external_id, int) and isinstance(season_number, int):
                import_id = first.get("season_import_id")
                physical = import_id if isinstance(import_id, str) else str(completed_season_size)
                direct_cycles = sorted(
                    f"{cycle['request_id']}.{cycle['generation']}"
                    for cycle in request_cycles
                    if int(cycle["chat_id"]) == chat_id
                )
                request_generation = (
                    hashlib.sha256("|".join(direct_cycles).encode()).hexdigest()[:12]
                    if direct_cycles
                    else "library"
                )
                completion_keys = [
                    f"season-complete:{external_id}:{season_number}:{physical}:{request_generation}"
                ]
            if completion_keys and self.store.delivered(completion_keys, chat_id):
                self.store.mark_delivered(keys, chat_id)
                continue
            pending = (
                list(batch)
                if completion_keys
                else [
                    item
                    for item in batch
                    if not self.store.delivered([str(item["event_key"])], chat_id)
                ]
            )
            if not pending:
                continue
            retry_key = (chat_id, tuple(str(item["event_key"]) for item in pending))
            retry = self._retry_state.get(retry_key)
            if retry is not None and time.monotonic() < retry[1]:
                deferred = True
                continue
            pending[0] = {**pending[0], "completed_season_size": completed_season_size}
            poster_url = pending[0].get("poster_url")
            message = self._message(pending)
            plex_url = str(pending[0]["plex_url"])
            try:
                if isinstance(poster_url, str):
                    await self._send(chat_id, message, plex_url, poster_url=poster_url)
                else:
                    await self._send(chat_id, message, plex_url)
            except Exception as exc:
                if isinstance(exc, _TelegramSendError) and not exc.retryable:
                    LOGGER.warning(
                        "suppressing terminal Telegram delivery failure for chat %s", chat_id
                    )
                    self.store.mark_delivered(
                        [str(item["event_key"]) for item in pending] + completion_keys,
                        chat_id,
                    )
                    self._retry_state.pop(retry_key, None)
                    continue
                attempts = (retry[0] if retry is not None else 0) + 1
                requested_delay = (
                    exc.retry_after_seconds if isinstance(exc, _TelegramSendError) else None
                )
                delay = min(
                    max(
                        FLUSH_INTERVAL_SECONDS * 2 ** min(attempts - 1, 8),
                        requested_delay or 0,
                    ),
                    MAX_RETRY_BACKOFF_SECONDS,
                )
                self._retry_state[retry_key] = (attempts, time.monotonic() + delay)
                deferred = True
                LOGGER.warning("Telegram delivery failed for chat %s; retrying later", chat_id)
                continue
            self._retry_state.pop(retry_key, None)
            self.store.mark_delivered(
                [str(item["event_key"]) for item in pending] + completion_keys,
                chat_id,
            )
        if track_request_cycles:
            assert isinstance(external_id, int)
            fresh_cycles = self.store.request_cycles(
                media_type=str(first["media_type"]),
                external_id=external_id,
                season_number=season_number,
            )
            if _request_cycle_ids(fresh_cycles) != captured_cycle_ids:
                return
        # Keep an unresolved episode durable after notifying administrators.
        # Once Plex exposes its show TVDB ID, the requester can still be found
        # and notified without sending the administrator a duplicate.
        if (
            deferred
            or season_verification_pending
            or first["external_id"] is None
            or completion_unknown
        ):
            return
        if first["media_type"] == "movie" and isinstance(first["external_id"], int):
            self.store.mark_movie_available(int(first["external_id"]), request_cycles)
        self.store.mark_events_notified(keys)

    @staticmethod
    def _message(batch: list[dict[str, Any]]) -> str:
        first = batch[0]
        if first["media_type"] == "movie":
            return f"🍿 <b>Available in Plex</b>\n{html.escape(str(first['title']))}"
        show = html.escape(str(first["show_title"] or first["title"]))
        season = first["season_number"]
        completed_season_size = first.get("completed_season_size")
        if isinstance(completed_season_size, int) and completed_season_size > 0:
            label = _season_label(season) or "Season"
            return (
                f"📺 <b>Season complete in Plex</b>\n"
                f"{show} · {label} ({completed_season_size} episodes)"
            )
        episodes = [item for item in batch if item["episode_number"] is not None]
        if not episodes:
            label = _season_label(season) or "New series"
            return f"📺 <b>Available in Plex</b>\n{show} · {label}"
        if len(episodes) > 1:
            label = _season_label(season) or "New episodes"
            return f"📺 <b>Available in Plex</b>\n{show} · {label} ({len(episodes)} episodes)"
        first = episodes[0]
        episode = first["episode_number"]
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
            raise _TelegramSendError(None, retryable=True) from None
        if response.is_error:
            # Do not raise HTTPStatusError: its message includes the bot token
            # embedded in the request URL.
            retry_after: int | None = None
            if response.status_code == 429:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                raw_retry = (
                    body.get("parameters", {}).get("retry_after")
                    if isinstance(body, dict) and isinstance(body.get("parameters"), dict)
                    else response.headers.get("Retry-After")
                )
                retry_after = _positive(raw_retry)
            raise _TelegramSendError(
                response.status_code,
                retryable=response.status_code != 403,
                retry_after_seconds=retry_after,
            )

    def _telegram_token(self) -> str:
        values = read_dotenv(self.config.policy_file, {"TELEGRAM_BOT_TOKEN"})
        token = values.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("Telegram bot token is missing")
        return token
