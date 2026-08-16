"""Exercise the media picker against the pinned Hermes runtime APIs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gateway.platforms.base import Platform
from gateway.session import SessionSource
from tools import clarify_gateway

from hermes_media import plugin
from hermes_media.trusted import session_key_from_event
from media_gateway.types import Actor, Role


class PickerAdapter:
    def __init__(self) -> None:
        self._media_delivery_loop: asyncio.AbstractEventLoop | None = None
        self.prompt: dict[str, object] | None = None

    async def send_clarify(self, **values: object) -> SimpleNamespace:
        self.prompt = values
        clarify_id = values["clarify_id"]
        choices = values["choices"]
        assert isinstance(clarify_id, str)
        assert isinstance(choices, list)
        assert clarify_gateway.resolve_gateway_clarify(clarify_id, str(choices[1]))
        return SimpleNamespace(success=True)


class WaitingPickerAdapter(plugin.MediaTelegramAdapter):
    def __init__(self) -> None:
        self.config = SimpleNamespace(extra={})
        self._media_delivery_loop: asyncio.AbstractEventLoop | None = None
        self._clarify_state: dict[str, str] = {}
        self.prompt: dict[str, object] | None = None

    async def send_clarify(self, **values: object) -> SimpleNamespace:
        self.prompt = values
        return SimpleNamespace(success=True)


async def main() -> None:
    native_adapter = plugin._native_adapter()
    assert callable(getattr(native_adapter, "send_clarify", None))

    actor = Actor(user_id=1001, chat_id=1001)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="1001",
        chat_type="dm",
        user_id="1001",
    )
    session_key = session_key_from_event(SimpleNamespace(source=source), actor)
    assert session_key == "agent:main:telegram:dm:1001"

    adapter = PickerAdapter()
    adapter._media_delivery_loop = asyncio.get_running_loop()
    plugin._active_adapter = adapter  # type: ignore[assignment]
    result = await plugin._decorate_search_result(
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

    assert adapter.prompt is not None
    assert adapter.prompt["session_key"] == session_key
    assert result["results"] == [
        {"media_type": "movie", "tmdb_id": 22, "title": "House", "year": 2024}
    ]
    assert result["telegram_presentation"]["selection_status"] == "selected"

    waiting_adapter = WaitingPickerAdapter()
    waiting_adapter._media_delivery_loop = asyncio.get_running_loop()
    plugin._active_adapter = waiting_adapter  # type: ignore[assignment]
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
        if waiting_adapter.prompt is not None:
            break
        await asyncio.sleep(0.01)
    assert waiting_adapter.prompt is not None
    forwarded: list[str] = []

    async def native_handle(_self: object, event: object) -> None:
        forwarded.append(str(getattr(event, "text", "")))

    class Gateway:
        @staticmethod
        async def observe(_actor: Actor) -> Role:
            return Role.USER

    original_handle = plugin._NativeAdapter.handle_message
    plugin._NativeAdapter.handle_message = native_handle  # type: ignore[method-assign]
    plugin._client = Gateway()  # type: ignore[assignment]
    sender = SimpleNamespace(id=actor.user_id)
    message = SimpleNamespace(from_user=sender, sender_chat=None, chat=sender, text="avengers")
    event = SimpleNamespace(source=source, raw_message=message, text="avengers")
    try:
        await waiting_adapter.handle_message(event)
    finally:
        plugin._NativeAdapter.handle_message = original_handle  # type: ignore[method-assign]
    assert forwarded == ["avengers"]
    superseded = await asyncio.wait_for(pending, timeout=2)
    assert superseded["results"] == []
    assert superseded["telegram_presentation"]["selection_status"] == "superseded"


if __name__ == "__main__":
    asyncio.run(main())
