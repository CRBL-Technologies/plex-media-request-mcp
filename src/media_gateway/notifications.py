"""Plex observations and Telegram availability notifications."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import defaultdict
from contextlib import suppress
from typing import Any
from urllib.parse import quote

import httpx

from .config import Config
from .policy import Policy
from .secrets import read_dotenv
from .store import Store
from .types import Actor
from .upstream import Upstream

LOGGER = logging.getLogger(__name__)


def _positive(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
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
            season = _positive(metadata.get("index"))
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
            season = _positive(metadata.get("parentIndex"))
            episode = _positive(metadata.get("index"))
        title = str(metadata.get("title") or "Untitled")[:300]
        encoded_key = quote(f"/library/metadata/{rating_key}", safe="")
        url = (
            "https://app.plex.tv/desktop/#!/server/"
            f"{quote(self.config.plex_machine_id, safe='')}/details?key={encoded_key}"
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
                await asyncio.wait_for(self._stop.wait(), timeout=15)
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
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/getChat", json={"chat_id": user_id}
            )
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

    async def flush(self) -> None:
        now = int(time.time())
        before = now - self.config.notification_delay_seconds
        # Read young rows too: a season batch is delivered only after the
        # entire group has been quiet for the configured delay. Filtering in
        # SQL first would send early episodes while later ones were arriving.
        events = self.store.pending_media_events(now, limit=500)
        grouped: dict[tuple[object, ...], list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            key: tuple[object, ...]
            if event["media_type"] == "series":
                key = (
                    "series",
                    event["external_id"] or event["parent_rating_key"] or event["show_title"],
                    event["season_number"],
                )
            else:
                key = ("movie", event["event_key"])
            grouped[key].append(event)
        for batch in grouped.values():
            if max(int(item["observed_at"]) for item in batch) > before:
                continue
            batch = await self._enrich(batch)
            await self._deliver_batch(batch)

    async def _enrich(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ready: list[dict[str, Any]] = []
        for event in events:
            if event.get("external_id") is None:
                is_series = event.get("media_type") == "series"
                lookup_key = (
                    event.get("parent_rating_key") if is_series else event.get("rating_key")
                )
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
            ready.append(event)
        return ready

    async def _deliver_batch(self, batch: list[dict[str, Any]]) -> None:
        first = batch[0]
        keys = [str(item["event_key"]) for item in batch]
        recipients = set(self.policy.snapshot().admins)
        recipients |= self.store.request_chats(
            media_type=str(first["media_type"]),
            external_id=first["external_id"],
            season_number=first["season_number"],
        )
        if not recipients:
            return
        for chat_id in recipients:
            pending = [
                item
                for item in batch
                if not self.store.delivered([str(item["event_key"])], chat_id)
            ]
            if not pending:
                continue
            await self._send(chat_id, self._message(pending), str(pending[0]["plex_url"]))
            self.store.mark_delivered([str(item["event_key"]) for item in pending], chat_id)
        # Keep an unresolved episode durable after notifying administrators.
        # Once Plex exposes its show TVDB ID, the requester can still be found
        # and notified without sending the administrator a duplicate.
        if first["external_id"] is None:
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
            label = f"Season {season}" if season is not None else "New series"
            return f"📺 <b>Available in Plex</b>\n{show} · {label}"
        if len(batch) > 1:
            label = f"Season {season}" if season is not None else "New episodes"
            return f"📺 <b>Available in Plex</b>\n{show} · {label} ({len(episodes)} episodes)"
        episode = first["episode_number"]
        marker = ""
        if season is not None and episode is not None:
            marker = f" · S{int(season):02d}E{int(episode):02d}"
        title = html.escape(str(first["title"]))
        return f"📺 <b>Available in Plex</b>\n{show}{marker} · {title}"

    async def _send(self, chat_id: int, text: str, plex_url: str) -> None:
        token = self._telegram_token()
        body = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": [[{"text": "Open in Plex", "url": plex_url}]]},
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage", json=body
            )
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
