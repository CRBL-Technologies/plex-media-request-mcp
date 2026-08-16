"""Exercise the media picker against the pinned Hermes runtime APIs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gateway.platforms.base import Platform
from gateway.session import SessionSource
from tools import clarify_gateway

from hermes_media import plugin
from hermes_media.trusted import session_key_from_event
from media_gateway.types import Actor


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


if __name__ == "__main__":
    asyncio.run(main())
