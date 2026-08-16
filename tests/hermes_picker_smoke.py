"""Exercise the media picker against the pinned Hermes runtime APIs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gateway.platforms.base import Platform
from gateway.session import SessionSource

from hermes_media import compat, plugin
from hermes_media.compat import NATIVE_CALLBACK_METHOD
from hermes_media.compat import native_adapter as compat_native_adapter
from hermes_media.trusted import actor_scope, session_key_from_event
from media_gateway.types import Actor, Role


class WaitingPickerAdapter(plugin.MediaTelegramAdapter):
    def __init__(self) -> None:
        self.config = SimpleNamespace(extra={})
        self._media_delivery_loop: asyncio.AbstractEventLoop | None = None
        self.card: dict[str, object] | None = None
        self._bot = SimpleNamespace(send_photo=self.send_photo, send_message=self.send_message)
        self._crbl_media_callback_handler = plugin._handle_media_picker_callback

    async def send_photo(self, **values: object) -> SimpleNamespace:
        self.card = values
        return SimpleNamespace(message_id=42)

    async def send_message(self, **values: object) -> SimpleNamespace:
        self.card = values
        return SimpleNamespace(message_id=43)


async def main() -> None:
    native_adapter = plugin._NativeAdapter
    assert callable(getattr(native_adapter, "send_clarify", None))
    assert callable(getattr(native_adapter, NATIVE_CALLBACK_METHOD, None))
    # The registration gate must still be able to detect a missing native
    # adapter by identity, and the CRBL subclass must not mask that.
    assert native_adapter is not plugin._FallbackAdapter
    assert compat_native_adapter(plugin._FallbackAdapter) is not plugin._FallbackAdapter

    class _Sentinel:
        pass

    original_module = compat.NATIVE_MODULE
    compat.NATIVE_MODULE = "crbl.missing.telegram.adapter"
    try:
        assert compat_native_adapter(_Sentinel) is _Sentinel
    finally:
        compat.NATIVE_MODULE = original_module

    actor = Actor(user_id=1001, chat_id=1001)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="1001",
        chat_type="dm",
        user_id="1001",
    )
    session_key = session_key_from_event(SimpleNamespace(source=source), actor)
    assert session_key == "agent:main:telegram:dm:1001"

    waiting_adapter = WaitingPickerAdapter()
    waiting_adapter._media_delivery_loop = asyncio.get_running_loop()
    plugin._active_adapter = waiting_adapter  # type: ignore[assignment]
    with actor_scope(actor, Role.USER, session_key):
        pending = asyncio.create_task(
            plugin._decorate_search_result(
                actor.chat_id,
                session_key,
                {
                    "query": "House",
                    "results": [
                        {
                            "media_type": "series",
                            "tvdb_id": 11,
                            "title": "House",
                            "year": 2004,
                            "poster_url": "https://artworks.thetvdb.com/house.jpg",
                        },
                        {
                            "media_type": "movie",
                            "tmdb_id": 22,
                            "title": "House",
                            "year": 2024,
                            "poster_url": "https://image.tmdb.org/house.jpg",
                        },
                    ],
                },
            )
        )
    for _ in range(100):
        if waiting_adapter.card is not None:
            break
        await asyncio.sleep(0.01)
    assert waiting_adapter.card is not None
    markup = waiting_adapter.card["reply_markup"]
    rows = markup.inline_keyboard
    assert rows[0][0].text == "●  📺 House · 2004"
    assert rows[1][0].text == "○  🎬 House · 2024"

    class Query:
        data = rows[1][0].callback_data
        from_user = SimpleNamespace(id=actor.user_id)
        message = SimpleNamespace(chat_id=actor.chat_id)

        async def answer(self, **_values: object) -> None:
            return None

        async def edit_message_media(self, **values: object) -> None:
            self.media_values = values

        async def edit_message_caption(self, **values: object) -> None:
            self.caption_values = values

    query = Query()
    update = SimpleNamespace(callback_query=query)
    await waiting_adapter._handle_callback_query(update, None)
    assert query.media_values["media"].media == "https://image.tmdb.org/house.jpg"
    query.data = query.media_values["reply_markup"].inline_keyboard[-1][0].callback_data
    await waiting_adapter._handle_callback_query(update, None)
    selected = await asyncio.wait_for(pending, timeout=2)
    assert selected["results"][0]["tmdb_id"] == 22
    assert selected["telegram_presentation"]["selection_status"] == "request_selected"

    await _assert_cancel_closes_card(actor, session_key)
    await _assert_new_message_supersedes_card(actor, source, session_key)


async def _assert_cancel_closes_card(actor: Actor, session_key: str) -> None:
    """Cancel must resolve the card and request nothing."""

    adapter = WaitingPickerAdapter()
    adapter._media_delivery_loop = asyncio.get_running_loop()
    plugin._active_adapter = adapter  # type: ignore[assignment]
    with actor_scope(actor, Role.USER, session_key):
        pending = asyncio.create_task(
            plugin._decorate_search_result(
                actor.chat_id,
                session_key,
                {
                    "query": "House",
                    "results": [
                        {"media_type": "movie", "tmdb_id": 11, "title": "House", "year": 2026},
                        {"media_type": "movie", "tmdb_id": 22, "title": "House", "year": 2024},
                    ],
                },
            )
        )
    for _ in range(100):
        if adapter.card is not None:
            break
        await asyncio.sleep(0.01)
    assert adapter.card is not None

    class CancelQuery:
        data = adapter.card["reply_markup"].inline_keyboard[-1][1].callback_data
        from_user = SimpleNamespace(id=actor.user_id)
        message = SimpleNamespace(chat_id=actor.chat_id)

        async def answer(self, **_values: object) -> None:
            return None

        async def edit_message_caption(self, **values: object) -> None:
            self.caption_values = values

    query = CancelQuery()
    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)
    cancelled = await asyncio.wait_for(pending, timeout=2)
    assert cancelled["results"] == []
    assert cancelled["telegram_presentation"]["selection_status"] == "cancelled"
    assert query.caption_values["reply_markup"] is None


async def _assert_new_message_supersedes_card(
    actor: Actor, source: object, session_key: str
) -> None:
    """A fresh message must supersede the card instead of answering it."""

    adapter = WaitingPickerAdapter()
    adapter._media_delivery_loop = asyncio.get_running_loop()
    plugin._active_adapter = adapter  # type: ignore[assignment]
    with actor_scope(actor, Role.USER, session_key):
        pending = asyncio.create_task(
            plugin._decorate_search_result(
                actor.chat_id,
                session_key,
                {
                    "query": "House",
                    "results": [
                        {"media_type": "movie", "tmdb_id": 11, "title": "House", "year": 2026},
                        {"media_type": "movie", "tmdb_id": 22, "title": "House", "year": 2024},
                    ],
                },
            )
        )
    for _ in range(100):
        if adapter.card is not None:
            break
        await asyncio.sleep(0.01)
    assert adapter.card is not None
    forwarded: list[str] = []

    async def native_handle(_self: object, event: object) -> None:
        forwarded.append(str(getattr(event, "text", "")))

    class Gateway:
        @staticmethod
        async def observe(_actor: Actor) -> Role:
            return Role.USER

    original_handle = plugin._NativeAdapter.handle_message
    plugin._NativeAdapter.handle_message = native_handle  # type: ignore[method-assign]
    original_client = plugin._client
    plugin._client = Gateway()  # type: ignore[assignment]
    sender = SimpleNamespace(id=actor.user_id)
    message = SimpleNamespace(from_user=sender, sender_chat=None, chat=sender, text="avengers")
    event = SimpleNamespace(source=source, raw_message=message, text="avengers")
    try:
        await adapter.handle_message(event)
    finally:
        plugin._NativeAdapter.handle_message = original_handle  # type: ignore[method-assign]
        plugin._client = original_client
    assert forwarded == ["avengers"]
    superseded = await asyncio.wait_for(pending, timeout=2)
    assert superseded["results"] == []
    assert superseded["telegram_presentation"]["selection_status"] == "superseded"


if __name__ == "__main__":
    asyncio.run(main())
