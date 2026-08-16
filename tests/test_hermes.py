from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from hermes_media import plugin
from hermes_media.trusted import TrustError, actor_from_event, actor_scope
from media_gateway.constants import ADMIN_UPSTREAM_TOOLS, SHARED_TOOLS
from media_gateway.tools import SHARED_SCHEMAS
from media_gateway.types import Actor, Role


@dataclass
class Value:
    id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    chat_id: int | None = None
    user_id: int | None = None


def event(user_id: int = 1001) -> Value:
    sender = Value(id=user_id, username="philippe", first_name="Philippe")
    chat = Value(id=user_id)
    message = Value()
    message.from_user = sender  # type: ignore[attr-defined]
    message.chat = chat  # type: ignore[attr-defined]
    source = Value(user_id=user_id, chat_id=user_id)
    result = Value()
    result.raw_message = message  # type: ignore[attr-defined]
    result.source = source  # type: ignore[attr-defined]
    return result


def test_actor_comes_only_from_native_event() -> None:
    actor = actor_from_event(event())
    assert actor == Actor(
        user_id=1001,
        chat_id=1001,
        username="philippe",
        first_name="Philippe",
    )
    synthetic = Value()
    synthetic.source = Value(user_id=1001, chat_id=1001)  # type: ignore[attr-defined]
    with pytest.raises(TrustError, match="native"):
        actor_from_event(synthetic)


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, dict[str, Any]]] = []

    def schemas(self) -> list[dict[str, Any]]:
        schema = {"type": "object", "additionalProperties": False}
        return [
            {
                "name": name,
                "description": f"Description for {name}",
                "inputSchema": schema,
                "scope": "shared",
            }
            for name in SHARED_TOOLS
        ] + [
            {
                "name": name,
                "description": f"Description for {name}",
                "inputSchema": schema,
                "scope": "admin",
            }
            for name in sorted(ADMIN_UPSTREAM_TOOLS)
        ]

    async def observe(self, actor: Actor, *, blocked: bool = False) -> Role:
        assert not blocked
        if actor.user_id == 7777:
            return Role.BLOCKED
        return Role.ADMIN if actor.user_id == 9001 else Role.USER

    async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((actor.user_id, name, arguments))
        return {"called": name}


class FakeContext:
    def __init__(self) -> None:
        self.platforms: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []

    def register_platform(self, **values: Any) -> None:
        self.platforms.append(values)

    def register_tool(self, **values: Any) -> None:
        self.tools.append(values)


async def test_plugin_registers_closed_inventory_and_binds_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway()
    monkeypatch.setattr(plugin, "_client", gateway)
    context = FakeContext()
    plugin.register(context)
    assert len(context.platforms) == 1
    platform_hint = context.platforms[0]["platform_hint"]
    assert platform_hint == plugin.PLATFORM_HINT
    assert "search_media in the current turn" in platform_hint
    assert "Never reuse search results from conversation history" in platform_hint
    assert "search_media itself handles any ambiguous-result selection" in platform_hint
    assert {item["name"] for item in context.tools} == set(SHARED_TOOLS) | ADMIN_UPSTREAM_TOOLS
    search = next(item for item in context.tools if item["name"] == "search_media")
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER):
        result = await search["handler"]({"query": "test"})
    assert result == '{"called":"search_media"}'
    assert gateway.calls == [(1001, "search_media", {"query": "test"})]


async def test_search_sends_poster_album_and_returns_only_native_picker_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SearchGateway(FakeGateway):
        async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((actor.user_id, name, arguments))
            return {
                "query": "House",
                "results": [
                    {
                        "media_type": "movie",
                        "tmdb_id": 11,
                        "title": "The Last House",
                        "year": 2026,
                        "poster_url": "https://image.tmdb.org/one.jpg",
                    },
                    {
                        "media_type": "series",
                        "tvdb_id": 22,
                        "title": "House",
                        "year": 2004,
                        "poster_url": "https://artworks.thetvdb.com/two.jpg",
                    },
                ],
                "unavailable_sources": [],
            }

    class PresentationAdapter:
        def __init__(self) -> None:
            self._media_delivery_loop = asyncio.get_running_loop()
            self.albums: list[tuple[str, list[tuple[str, str]]]] = []
            self.prompts: list[dict[str, Any]] = []

        async def send_multiple_images(self, chat_id: str, images: list[tuple[str, str]]) -> None:
            self.albums.append((chat_id, images))

        async def send_clarify(self, **values: Any) -> SimpleNamespace:
            self.prompts.append(values)
            return SimpleNamespace(success=True)

    class ClarifyGateway:
        def __init__(self) -> None:
            self.registered: list[dict[str, Any]] = []

        def register(self, **values: Any) -> None:
            self.registered.append(values)

        @staticmethod
        def get_clarify_timeout() -> int:
            return 60

        @staticmethod
        def wait_for_response(_clarify_id: str, _timeout: float) -> str:
            return "House (2004) · Series · TVDB 22"

        @staticmethod
        def resolve_gateway_clarify(_clarify_id: str, _response: str) -> bool:
            return True

    gateway = SearchGateway()
    adapter = PresentationAdapter()
    clarify = ClarifyGateway()
    monkeypatch.setattr(plugin, "_client", gateway)
    monkeypatch.setattr(plugin, "_active_adapter", adapter)
    monkeypatch.setattr(plugin, "_clarify_gateway", lambda: clarify)
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER, "agent:main:telegram:dm:1001"):
        raw = await plugin._handler("search_media")({"query": "House"})

    result = json.loads(raw)
    assert adapter.albums == [
        (
            "1001",
            [
                (
                    "https://image.tmdb.org/one.jpg",
                    "1 · The Last House (2026) · Movie · TMDB 11",
                ),
                (
                    "https://artworks.thetvdb.com/two.jpg",
                    "2 · House (2004) · Series · TVDB 22",
                ),
            ],
        )
    ]
    assert len(adapter.prompts) == 1
    assert adapter.prompts[0]["chat_id"] == "1001"
    assert adapter.prompts[0]["question"] == "Which result did you mean?"
    assert adapter.prompts[0]["choices"] == [
        "The Last House (2026) · Movie · TMDB 11",
        "House (2004) · Series · TVDB 22",
    ]
    assert clarify.registered[0]["session_key"] == "agent:main:telegram:dm:1001"
    assert result["results"] == [
        {
            "media_type": "series",
            "tvdb_id": 22,
            "title": "House",
            "year": 2004,
            "poster_url": "https://artworks.thetvdb.com/two.jpg",
        }
    ]
    assert result["telegram_presentation"]["poster_cards_delivered"] is True
    assert result["telegram_presentation"]["selection_status"] == "selected"
    assert "candidate list" in result["telegram_presentation"]["instruction"]


async def test_single_search_result_sends_one_poster_without_opening_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SearchGateway(FakeGateway):
        async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((actor.user_id, name, arguments))
            return {
                "query": "3 Body Problem 2024",
                "results": [
                    {
                        "media_type": "series",
                        "tvdb_id": 411959,
                        "title": "3 Body Problem",
                        "year": 2024,
                        "poster_url": "https://artworks.thetvdb.com/three-body.jpg",
                    }
                ],
                "unavailable_sources": [],
            }

    class PresentationAdapter:
        def __init__(self) -> None:
            self._media_delivery_loop = asyncio.get_running_loop()
            self.images: list[dict[str, Any]] = []

        async def send_image(self, **values: Any) -> SimpleNamespace:
            self.images.append(values)
            return SimpleNamespace(success=True)

        async def send_clarify(self, **_values: Any) -> SimpleNamespace:
            raise AssertionError("a single result must not open the picker")

    gateway = SearchGateway()
    adapter = PresentationAdapter()
    monkeypatch.setattr(plugin, "_client", gateway)
    monkeypatch.setattr(plugin, "_active_adapter", adapter)
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER):
        raw = await plugin._handler("search_media")({"query": "3 Body Problem 2024"})

    result = json.loads(raw)
    assert adapter.images == [
        {
            "chat_id": "1001",
            "image_url": "https://artworks.thetvdb.com/three-body.jpg",
            "caption": "1 · 3 Body Problem (2024) · Series · TVDB 411959",
        }
    ]
    assert len(result["results"]) == 1
    assert result["telegram_presentation"]["selection_status"] == "single_result"


async def test_failed_picker_never_returns_an_ambiguous_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SearchGateway(FakeGateway):
        async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((actor.user_id, name, arguments))
            return {
                "query": "House",
                "results": [
                    {"media_type": "movie", "tmdb_id": 11, "title": "House", "year": 2026},
                    {"media_type": "movie", "tmdb_id": 22, "title": "House", "year": 2024},
                ],
            }

    class PresentationAdapter:
        def __init__(self) -> None:
            self._media_delivery_loop = asyncio.get_running_loop()

        async def send_clarify(self, **_values: Any) -> SimpleNamespace:
            return SimpleNamespace(success=False)

    class ClarifyGateway:
        @staticmethod
        def register(**_values: Any) -> None:
            return None

        @staticmethod
        def resolve_gateway_clarify(_clarify_id: str, _response: str) -> bool:
            return True

        @staticmethod
        def wait_for_response(_clarify_id: str, _timeout: float) -> str:
            return ""

        @staticmethod
        def get_clarify_timeout() -> int:
            return 60

    monkeypatch.setattr(plugin, "_client", SearchGateway())
    monkeypatch.setattr(plugin, "_active_adapter", PresentationAdapter())
    monkeypatch.setattr(plugin, "_clarify_gateway", lambda: ClarifyGateway())
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER):
        raw = await plugin._handler("search_media")({"query": "House"})

    result = json.loads(raw)
    assert result["results"] == []
    assert result["telegram_presentation"]["selection_status"] == "unavailable"
    assert "do not guess" in result["telegram_presentation"]["instruction"]


def test_search_presentation_keeps_card_numbers_aligned_after_invalid_rows() -> None:
    cards, choices, candidates = plugin._search_presentation(
        {
            "results": [
                {"media_type": "movie", "title": "Missing ID"},
                {
                    "media_type": "movie",
                    "tmdb_id": 22,
                    "title": "House",
                    "year": 2024,
                    "poster_url": "https://image.tmdb.org/house.jpg",
                },
            ]
        }
    )
    assert choices == ["House (2024) · Movie · TMDB 22"]
    assert cards == [("https://image.tmdb.org/house.jpg", "1 · House (2024) · Movie · TMDB 22")]
    assert candidates[0]["tmdb_id"] == 22


def test_search_tool_contract_owns_native_telegram_selection() -> None:
    text = SHARED_SCHEMAS["search_media"]["description"]
    assert "before the tool returns" in text
    assert "call clarify" in text
    assert "Never use MEDIA" in text


async def test_adapter_verifies_actor_before_trusting_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway()
    monkeypatch.setattr(plugin, "_client", gateway)
    adapter = plugin.MediaTelegramAdapter(object())
    assert adapter.authorization_is_upstream is True
    assert adapter._is_user_authorized_from_message(object()) is True
    allowed = event(1001)
    assert await adapter.handle_message(allowed) is allowed
    with pytest.raises(PermissionError, match="not allowed"):
        await adapter.handle_message(event(7777))


def test_platform_visibility_adds_search_only_for_media_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools_config = SimpleNamespace(
        _get_platform_tools=lambda _config, _platform, *_args, **_kwargs: {"native"}
    )
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.tools_config = tools_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)

    plugin._visibility_patch()
    resolver = tools_config._get_platform_tools
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER):
        assert resolver({}, "telegram") == {plugin.SHARED_TOOLSET, plugin.SEARCH_TOOLSET}
    with actor_scope(actor, Role.ADMIN):
        assert resolver({}, "telegram") == {
            plugin.SHARED_TOOLSET,
            plugin.ADMIN_TOOLSET,
            plugin.SEARCH_TOOLSET,
        }
    assert resolver({}, "discord") == {"native"}


def test_web_search_guardrail_uses_canonical_hermes_config() -> None:
    plugin.validate_search_guardrail(
        {"tool_loop_guardrails": {"loop_caps": {"max_web_searches": 10}}}
    )
    with pytest.raises(RuntimeError, match="max_web_searches"):
        plugin.validate_search_guardrail({})
    with pytest.raises(RuntimeError, match="max_web_searches"):
        plugin.validate_search_guardrail(
            {"tool_loop_guardrails": {"loop_caps": {"max_web_searches": 50}}}
        )


def test_platform_hint_requires_effective_telegram_override() -> None:
    plugin.validate_platform_hint(
        {"platform_hints": {"telegram": {"append": plugin.PLATFORM_HINT}}}
    )
    with pytest.raises(RuntimeError, match=r"platform_hints\.telegram\.append"):
        plugin.validate_platform_hint({})
    with pytest.raises(RuntimeError, match=r"platform_hints\.telegram\.append"):
        plugin.validate_platform_hint({"platform_hints": {"telegram": plugin.PLATFORM_HINT}})
