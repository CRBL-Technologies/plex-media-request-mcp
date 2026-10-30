from __future__ import annotations

import asyncio
import json
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from hermes_media import compat as compat_module
from hermes_media import plugin
from hermes_media.soul import SOUL_MD, install_soul
from hermes_media.trusted import (
    TrustError,
    actor_from_event,
    actor_scope,
    claim_card_slot,
    current_event,
    current_role,
    require_actor,
)
from media_gateway.constants import ADMIN_UPSTREAM_TOOLS, SHARED_TOOLS
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


def text_event(text: str, user_id: int = 1001) -> Value:
    result = event(user_id)
    result.text = text  # type: ignore[attr-defined]
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


def session_owner() -> SimpleNamespace:
    config = SimpleNamespace(group_sessions_per_user=False, thread_sessions_per_user=False)

    def original_key(source: Any, key_source: Any = None) -> str:
        from gateway.session import build_session_key

        return str(
            build_session_key(
                key_source if key_source is not None else source,
                group_sessions_per_user=config.group_sessions_per_user,
                thread_sessions_per_user=config.thread_sessions_per_user,
            )
        )

    return SimpleNamespace(
        config=config,
        session_store=SimpleNamespace(
            config=config,
            _generate_session_key=original_key,
            _resolve_profile_for_key=lambda _source: None,
        ),
        _is_user_authorized=lambda source: source.user_id == "1001",
        _principal_authorized=lambda source, **_kwargs: source.user_id == "1001",
        _handle_message=lambda _event: None,
        _adapter_for_source=lambda _source: None,
        _profile_name_for_source=lambda _source, **_kwargs: None,
    )


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
    # The hint constrains what may be claimed, never which tool to reach for.
    assert "only source of truth for this library" in platform_hint
    assert "never report something as requested unless a tool said so" in platform_hint.lower()
    assert "only thing you can change" not in platform_hint
    assert "administrators may also manage the library" in platform_hint
    assert "Choose the tools yourself" in platform_hint
    for prescription in (
        "call search_media in the current turn",
        "one at a time",
        "ask which was meant",
        "exactly 4",
        "call request_titles once",
    ):
        assert prescription not in platform_hint, prescription
    assert {item["name"] for item in context.tools} == set(SHARED_TOOLS) | ADMIN_UPSTREAM_TOOLS
    search = next(item for item in context.tools if item["name"] == "search_media")
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER):
        result = await search["handler"]({"query": "test"})
    assert result == '{"called":"search_media"}'
    assert gateway.calls == [(1001, "search_media", {"query": "test"})]


async def test_recommendations_return_conversational_status_without_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecommendationGateway(FakeGateway):
        async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((actor.user_id, name, arguments))
            return {
                "query": "recommendations",
                "presentation": "recommendations",
                "results": [
                    {
                        "media_type": "movie",
                        "tmdb_id": 11,
                        "title": "Arrival",
                        "year": 2016,
                        "poster_url": "https://image.tmdb.org/arrival.jpg",
                    },
                    {
                        "media_type": "movie",
                        "tmdb_id": 12,
                        "title": "Ex Machina",
                        "year": 2014,
                        "poster_url": "https://image.tmdb.org/ex-machina.jpg",
                    },
                    {
                        "media_type": "movie",
                        "tmdb_id": 13,
                        "title": "Annihilation",
                        "year": 2018,
                        "poster_url": "https://image.tmdb.org/annihilation.jpg",
                    },
                    {
                        "media_type": "movie",
                        "tmdb_id": 14,
                        "title": "Dark City",
                        "year": 1998,
                        "poster_url": "https://image.tmdb.org/dark-city.jpg",
                    },
                ],
            }

    class PresentationAdapter:
        def __init__(self) -> None:
            self._media_delivery_loop = asyncio.get_running_loop()
            self.photos_sent: list[dict[str, Any]] = []

        async def send_image(self, **_values: Any) -> SimpleNamespace:
            raise AssertionError("recommendations must not send images")

    gateway = RecommendationGateway()
    adapter = PresentationAdapter()
    monkeypatch.setattr(plugin, "_client", gateway)
    monkeypatch.setattr(plugin, "_active_adapter", adapter)
    actor = Actor(user_id=1001, chat_id=1001)
    arguments = {
        "titles": [
            "Arrival (2016)",
            "Ex Machina (2014)",
            "Annihilation (2018)",
            "Dark City (1998)",
        ],
        "media_type": "movie",
    }
    with actor_scope(actor, Role.USER):
        raw = await plugin._handler("recommend_media")(arguments)

    result = json.loads(raw)
    assert result["telegram_presentation"]["selection_status"] == "conversational"
    assert "conversational" in result["telegram_presentation"]["instruction"].lower()
    assert len(result["results"]) == 4
    assert adapter.photos_sent == []
    assert gateway.calls == [(1001, "recommend_media", arguments)]


@pytest.mark.parametrize("downloaded", [True, False])
async def test_one_resolved_recommendation_sends_its_poster_and_plex_link(
    monkeypatch: pytest.MonkeyPatch,
    downloaded: bool,
) -> None:
    class RecommendationGateway(FakeGateway):
        async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((actor.user_id, name, arguments))
            return {
                "query": "recommendations",
                "presentation": "recommendations",
                "results": [
                    {
                        "media_type": "movie",
                        "tmdb_id": 329865,
                        "title": "Arrival",
                        "year": 2016,
                        "overview": "A linguist tries to understand visitors from another world.",
                        "poster_url": "https://image.tmdb.org/arrival.jpg",
                        "downloaded": downloaded,
                        "plex_url": "https://watch.plex.tv/movie/arrival" if downloaded else None,
                    }
                ],
            }

    class Button:
        def __init__(self, text: str, *, callback_data: str = "", url: str = "") -> None:
            self.text = text
            self.callback_data = callback_data
            self.url = url

    class Markup:
        def __init__(self, rows: list[list[Button]]) -> None:
            self.inline_keyboard = rows

    class PresentationAdapter:
        def __init__(self) -> None:
            self._media_delivery_loop = asyncio.get_running_loop()
            self._bot = SimpleNamespace(send_photo=self.send_photo)
            self.photos: list[dict[str, Any]] = []

        async def send_photo(self, **values: Any) -> SimpleNamespace:
            self.photos.append(values)
            return SimpleNamespace(message_id=42)

    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = Button  # type: ignore[attr-defined]
    telegram.InlineKeyboardMarkup = Markup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    gateway = RecommendationGateway()
    adapter = PresentationAdapter()
    monkeypatch.setattr(plugin, "_client", gateway)
    monkeypatch.setattr(plugin, "_active_adapter", adapter)
    actor = Actor(user_id=1001, chat_id=1001)
    arguments = {"titles": ["Arrival (2016)"], "media_type": "movie"}
    with actor_scope(actor, Role.USER):
        raw = await plugin._handler("recommend_media")(arguments)

    result = json.loads(raw)
    assert result["telegram_presentation"]["poster_cards_delivered"] is True
    assert "why it fits" in result["telegram_presentation"]["instruction"]
    assert len(adapter.photos) == 1
    card = adapter.photos[0]
    assert card["photo"] == "https://image.tmdb.org/arrival.jpg"
    if downloaded:
        button = card["reply_markup"].inline_keyboard[0][0]
        assert button.text == "▶ Open in Plex"
        assert button.url == "https://watch.plex.tv/movie/arrival"
    else:
        assert card["reply_markup"] is None
    # Resolving a choice shows its poster but does not request the movie.
    assert gateway.calls == [(1001, "recommend_media", arguments)]


def test_a_card_offers_a_plex_link_or_no_button(monkeypatch: pytest.MonkeyPatch) -> None:
    """A card can only ever link to Plex. It never carries an action."""

    class Button:
        def __init__(self, text: str, *, callback_data: str = "", url: str = "") -> None:
            self.text = text
            self.callback_data = callback_data
            self.url = url

    class Markup:
        def __init__(self, rows: list[list[Button]]) -> None:
            self.inline_keyboard = rows

    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = Button  # type: ignore[attr-defined]
    telegram.InlineKeyboardMarkup = Markup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    from hermes_media.compat import _single_result_markup

    markup = _single_result_markup(
        candidate={
            "media_type": "movie",
            "tmdb_id": 693134,
            "downloaded": True,
            "plex_url": "https://watch.plex.tv/movie/dune-part-two",
        }
    )
    buttons = [b for row in markup.inline_keyboard for b in row]  # type: ignore[attr-defined]
    assert len(buttons) == 1
    assert buttons[0].text == "▶ Open in Plex"
    assert buttons[0].url == "https://watch.plex.tv/movie/dune-part-two"
    assert buttons[0].callback_data == ""

    # No link means no button; nothing here can start a request.
    assert _single_result_markup(candidate={"media_type": "movie", "tmdb_id": 27205}) is None
    assert _single_result_markup(candidate={"media_type": "series", "tvdb_id": 411959}) is None
    assert (
        _single_result_markup(
            candidate={"media_type": "series", "tvdb_id": 411959, "downloaded": True}
        )
        is None
    )


async def test_single_search_result_sends_a_poster_without_any_action(
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
                        "season_states": [
                            {
                                "number": 1,
                                "files": 3,
                                "episodes": 3,
                                "status": "airing",
                                "next_airing": "2026-09-07T01:00:00Z",
                            }
                        ],
                    }
                ],
                "unavailable_sources": [],
            }

    class Button:
        def __init__(self, text: str, *, callback_data: str = "", url: str = "") -> None:
            self.text = text
            self.callback_data = callback_data
            self.url = url

    class Markup:
        def __init__(self, rows: list[list[Button]]) -> None:
            self.inline_keyboard = rows

    class PresentationAdapter:
        def __init__(self) -> None:
            self._media_delivery_loop = asyncio.get_running_loop()
            self._bot = SimpleNamespace(send_photo=self.send_photo)
            self.photos: list[dict[str, Any]] = []

        async def send_photo(self, **values: Any) -> SimpleNamespace:
            self.photos.append(values)
            return SimpleNamespace(message_id=42)

        async def send_clarify(self, **_values: Any) -> SimpleNamespace:
            raise AssertionError("a single result must not open the picker")

    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = Button  # type: ignore[attr-defined]
    telegram.InlineKeyboardMarkup = Markup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    gateway = SearchGateway()
    adapter = PresentationAdapter()
    monkeypatch.setattr(plugin, "_client", gateway)
    monkeypatch.setattr(plugin, "_active_adapter", adapter)
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER):
        raw = await plugin._handler("search_media")({"query": "3 Body Problem 2024"})

    result = json.loads(raw)
    assert len(adapter.photos) == 1
    card = adapter.photos[0]
    assert card["photo"] == "https://artworks.thetvdb.com/three-body.jpg"
    # Not on Plex, so there is no link and therefore no button at all.
    assert card["reply_markup"] is None
    assert len(result["results"]) == 1
    assert result["telegram_presentation"]["selection_status"] == "single_result"
    instruction = result["telegram_presentation"]["instruction"]
    assert "label it 'Airing'" in instruction
    assert "Never describe an airing season as complete or finished" in instruction


def test_presentation_drops_unnamable_rows_and_nothing_else() -> None:
    """A row the model cannot name is dropped; a matched title never is.

    The old limit of four was the picker's tab count. Cutting results here is
    invisible downstream: ``unmatched_titles`` reports only what the providers
    failed to match, so a truncated title reads as one nobody asked about.
    """

    rows: list[dict[str, Any]] = [
        {"media_type": "movie", "tmdb_id": index, "title": f"Film {index}", "year": 2000 + index}
        for index in range(1, 13)
    ]
    rows.insert(2, {"media_type": "movie"})  # No id and no title: unnamable.
    candidates = plugin._search_presentation({"results": rows})

    assert [c["title"] for c in candidates] == [f"Film {index}" for index in range(1, 13)]


async def test_a_long_recommendation_reaches_the_model_whole() -> None:
    """recommend_media takes up to twenty titles, so twenty must survive.

    Eight of twelve matched titles used to vanish between the gateway and the
    model. Because they matched, ``unmatched_titles`` stayed empty, so nothing
    in the result said the reply was answering about a third of the list.
    """

    result: dict[str, Any] = {
        "presentation": "recommendations",
        "unmatched_titles": [],
        "results": [
            {"media_type": "movie", "tmdb_id": index, "title": f"Film {index}", "year": 2010}
            for index in range(1, 13)
        ],
    }

    decorated = await plugin._decorate_search_result(1001, result)

    assert [c["title"] for c in decorated["results"]] == [f"Film {i}" for i in range(1, 13)]
    assert decorated["telegram_presentation"]["poster_cards_delivered"] is False


def test_tool_contracts_describe_capability_not_procedure() -> None:
    """Each description says what the tool does; choosing is the model's job."""

    from media_gateway.tools import SHARED_SCHEMAS

    search = SHARED_SCHEMAS["search_media"]["description"]
    assert "seasons_complete" in search and "plex_url" in search
    assert "posted to the chat as a poster" in search

    batch = SHARED_SCHEMAS["recommend_media"]
    assert "unmatched_titles" in batch["description"]
    assert "posted to the chat as a poster" in batch["description"]
    # The four-title rule was the picker's tab count, not a real constraint.
    assert batch["inputSchema"]["properties"]["titles"]["minItems"] == 1
    assert batch["inputSchema"]["properties"]["titles"]["maxItems"] == 20

    bulk = SHARED_SCHEMAS["request_titles"]["description"]
    assert "up to a hundred at once" in bulk
    assert "could not be matched" in bulk

    for name in ("request_movie", "request_series"):
        request = SHARED_SCHEMAS[name]["description"]
        assert "authoritative outcome" in request
        assert "sufficient to confirm" in request

    history = SHARED_SCHEMAS["request_status"]["description"]
    assert "historical requests" in history
    assert "not confirmation of a request mutation" in history

    for name in ("search_media", "recommend_media", "request_titles", "series_seasons"):
        text = SHARED_SCHEMAS[name]["description"]
        for prescription in ("never search", "Use this whenever", "never ask", "one at a time"):
            assert prescription not in text, f"{name}: {prescription}"


async def test_adapter_verifies_actor_before_trusting_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway()
    monkeypatch.setattr(plugin, "_client", gateway)
    adapter = plugin.MediaTelegramAdapter(SimpleNamespace(extra={}))
    adapter.gateway_runner = session_owner()
    assert adapter._is_user_authorized_from_message(object()) is True
    allowed = event(1001)
    await adapter.handle_message(allowed)
    assert allowed.source.role_authorized is True  # type: ignore[attr-defined]
    with pytest.raises(PermissionError, match="not allowed"):
        await adapter.handle_message(event(7777))


async def test_execution_rebinds_actor_role_and_card_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plugin, "_client", FakeGateway())
    adapter = plugin.MediaTelegramAdapter(SimpleNamespace(extra={}))
    adapter.gateway_runner = session_owner()
    seen: list[tuple[int, Role | None, bool]] = []

    async def handler(_event: object) -> None:
        seen.append((require_actor().user_id, current_role(), claim_card_slot()))

    adapter.set_message_handler(handler)
    with actor_scope(Actor(user_id=9001, chat_id=9001), Role.ADMIN):
        assert claim_card_slot()
        await adapter._message_handler(event(1001))
        await adapter._message_handler(event(1001))
        with pytest.raises(PermissionError, match="not allowed"):
            await adapter._message_handler(event(7777))
        # Native continuations retain their authenticated origin, not a new actor.
        await adapter._message_handler(SimpleNamespace(source=Value(user_id=9001, chat_id=9001)))
        with pytest.raises(TrustError, match="matching trusted"):
            await adapter._message_handler(
                SimpleNamespace(source=Value(user_id=1001, chat_id=1001))
            )
    assert seen == [(1001, Role.USER, True), (1001, Role.USER, True), (9001, Role.ADMIN, True)]
    for config in (adapter.gateway_runner.config, adapter.gateway_runner.session_store.config):
        assert config.group_sessions_per_user is False
        assert config.thread_sessions_per_user is False
    assert adapter._message_handler.__self__ is adapter.gateway_runner
    assert adapter.config.extra == {
        "group_sessions_per_user": True,
        "thread_sessions_per_user": True,
    }


def test_missing_native_source_identity_is_not_a_shared_session_fallback() -> None:
    incoming = event(1001)
    incoming.source.user_id = None  # type: ignore[attr-defined]
    with pytest.raises(TrustError, match="identities differ"):
        actor_from_event(incoming)


@pytest.mark.parametrize("chat_type,thread_id", [("group", None), ("forum", "77")])
async def test_pinned_runtime_queued_users_remain_isolated(
    monkeypatch: pytest.MonkeyPatch,
    chat_type: str,
    thread_id: str | None,
) -> None:
    """Run with the pinned Hermes source on PYTHONPATH, without contacting Telegram."""
    base = pytest.importorskip("gateway.platforms.base")
    config_module = pytest.importorskip("gateway.config")
    session_module = pytest.importorskip("gateway.session")
    monkeypatch.setattr(plugin, "_client", FakeGateway())
    config = config_module.PlatformConfig(extra={}, typing_indicator=False)
    config.extra["ingest_unmentioned_group_messages"] = True
    monkeypatch.setenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES", "true")
    adapter = plugin.MediaTelegramAdapter(config)
    adapter.gateway_runner = session_owner()
    entered = asyncio.Event()
    release = asyncio.Event()
    other_done = asyncio.Event()
    followup_done = asyncio.Event()
    seen: list[tuple[str, int, Role | None, bool]] = []

    async def handler(incoming: Any) -> None:
        seen.append((incoming.text, require_actor().user_id, current_role(), claim_card_slot()))
        if incoming.text == "admin":
            entered.set()
            await release.wait()
        elif incoming.text == "user":
            other_done.set()
        else:
            followup_done.set()

    adapter.set_message_handler(handler)
    assert adapter._telegram_observe_unmentioned_group_messages() is False

    def incoming(text: str, user: int) -> Any:
        raw = SimpleNamespace(from_user=SimpleNamespace(id=user), chat=SimpleNamespace(id=-100))
        return base.MessageEvent(
            text=text,
            message_type=base.MessageType.TEXT,
            raw_message=raw,
            source=session_module.SessionSource(
                platform=config_module.Platform.TELEGRAM,
                chat_id="-100",
                user_id=str(user),
                chat_type=chat_type,
                thread_id=thread_id,
            ),
        )

    key_for = adapter.gateway_runner.session_store._generate_session_key
    assert key_for(incoming("admin", 9001).source) != key_for(incoming("user", 1001).source)
    # The canonical history key and adapter queue key must agree.
    assert key_for(incoming("user", 1001).source) == session_module.build_session_key(
        incoming("user", 1001).source,
        group_sessions_per_user=True,
        thread_sessions_per_user=True,
    )
    slack_sources = [
        session_module.SessionSource(
            platform=config_module.Platform.SLACK,
            chat_id="C1",
            user_id=user,
            chat_type="group",
            thread_id="T1",
            scope_id="W1",
        )
        for user in ("U1", "U2")
    ]
    assert key_for(slack_sources[0]) == key_for(slack_sources[1])

    try:
        await adapter.handle_message(incoming("admin", 9001))
        await asyncio.wait_for(entered.wait(), 2)
        await adapter.handle_message(incoming("followup", 9001))
        await adapter.handle_message(incoming("user", 1001))
        await asyncio.wait_for(other_done.wait(), 2)
        release.set()
        await asyncio.wait_for(followup_done.wait(), 2)
        assert seen == [
            ("admin", 9001, Role.ADMIN, True),
            ("user", 1001, Role.USER, True),
            ("followup", 9001, Role.ADMIN, True),
        ]
    finally:
        release.set()
        tasks = list(adapter._background_tasks)
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
    adapter.set_message_handler(None)
    assert adapter._message_handler is None
    await adapter.handle_message(incoming("after drain", 1001))
    assert len(seen) == 3


@pytest.mark.parametrize("photo", [True, False])
@pytest.mark.parametrize("chat_type,thread_id", [("forum", "77"), ("dm", "77"), ("dm", None)])
async def test_pinned_runtime_cards_preserve_topic_and_dm_anchor(
    monkeypatch: pytest.MonkeyPatch,
    photo: bool,
    chat_type: str,
    thread_id: str | None,
) -> None:
    base = pytest.importorskip("gateway.platforms.base")
    config_module = pytest.importorskip("gateway.config")
    session_module = pytest.importorskip("gateway.session")
    adapter = plugin.MediaTelegramAdapter(config_module.PlatformConfig())
    sent: list[dict[str, Any]] = []

    async def send(**kwargs: Any) -> SimpleNamespace:
        sent.append(kwargs)
        return SimpleNamespace(message_id=123)

    adapter._bot = SimpleNamespace(send_photo=send, send_message=send)
    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = object  # type: ignore[attr-defined]
    telegram.InlineKeyboardMarkup = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    incoming = base.MessageEvent(
        text="movie",
        message_type=base.MessageType.TEXT,
        message_id="456",
        source=session_module.SessionSource(
            platform=config_module.Platform.TELEGRAM,
            chat_id="1001",
            user_id="1001",
            chat_type=chat_type,
            thread_id=thread_id,
        ),
    )
    await compat_module.send_single_result_card(
        adapter,
        chat_id=1001,
        poster_url="https://example.com/poster.jpg" if photo else None,
        caption="Movie",
        candidate={},
        event=incoming,
    )
    assert sent[0]["message_thread_id"] == (77 if thread_id else None)
    assert sent[0].get("reply_to_message_id") == (456 if chat_type == "dm" else None)


async def test_pinned_runtime_callbacks_keep_sender_and_chat_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_module = pytest.importorskip("gateway.config")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    (tmp_path / ".env").write_text("TELEGRAM_ALLOWED_USERS=1001\n")
    adapter = plugin.MediaTelegramAdapter(config_module.PlatformConfig())
    adapter.gateway_runner = session_owner()
    compat_module.isolate_actor_sessions(adapter)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "7777")
    selected: list[str] = []

    async def choose(_chat_id: str, value: str) -> str:
        selected.append(value)
        return "selected"

    async def answer(**_kwargs: Any) -> None:
        return None

    adapter._choice_picker_state["1001"] = {
        "choices": [{"value": "safe"}],
        "on_choice_selected": choose,
        "msg_id": 321,
        "session_key": adapter._source_session_key(
            adapter.build_source(chat_id="1001", user_id="1001")
        ),
    }

    def tap(sender: int, chat: int) -> SimpleNamespace:
        return SimpleNamespace(
            callback_query=SimpleNamespace(
                data="cp:0",
                from_user=SimpleNamespace(id=sender, first_name="Caller"),
                message=SimpleNamespace(
                    chat_id=chat,
                    chat=SimpleNamespace(type="private"),
                    from_user=SimpleNamespace(id=1001),
                    message_thread_id=None,
                    message_id=321,
                ),
                answer=answer,
                edit_message_text=answer,
            )
        )

    await adapter._handle_callback_query(tap(1001, 2002), None)
    await adapter._handle_callback_query(tap(7777, 1001), None)
    assert selected == []
    assert "1001" in adapter._choice_picker_state
    await adapter._handle_callback_query(tap(1001, 1001), None)
    assert selected == ["safe"]


def test_pinned_runtime_session_key_preserves_native_two_argument_contract(tmp_path: Path) -> None:
    config_module = pytest.importorskip("gateway.config")
    session_module = pytest.importorskip("gateway.session")
    store = session_module.SessionStore(
        tmp_path,
        config_module.GatewayConfig(group_sessions_per_user=False, multiplex_profiles=True),
    )
    adapter = plugin.MediaTelegramAdapter(config_module.PlatformConfig())
    adapter.gateway_runner = SimpleNamespace(session_store=store)
    compat_module.isolate_actor_sessions(adapter)
    source = session_module.SessionSource(
        platform=config_module.Platform.SLACK,
        chat_id="current",
        user_id="U1",
        chat_type="group",
        profile="owner",
    )
    key_source = session_module.SessionSource(
        platform=config_module.Platform.SLACK,
        chat_id="legacy",
        user_id="U2",
        chat_type="group",
        profile="not-owner",
    )
    assert store._generate_session_key(source, key_source) == session_module.build_session_key(
        key_source,
        group_sessions_per_user=False,
        profile="owner",
    )


async def test_pinned_runtime_injected_callback_auth_rejects_unlisted_users(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from media_gateway.policy import Policy

    config_module = pytest.importorskip("gateway.config")
    runner_module = pytest.importorskip("gateway.run")
    base = pytest.importorskip("gateway.platforms.base")
    session_module = pytest.importorskip("gateway.session")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    policy_file = tmp_path / ".env"
    policy_file.write_text("TELEGRAM_ALLOWED_USERS=1001,9001\n")
    policy = Policy(policy_file)
    adapter = plugin.MediaTelegramAdapter(config_module.PlatformConfig())
    runner = object.__new__(runner_module.GatewayRunner)
    runner.config = config_module.GatewayConfig()
    runner.adapters = {config_module.Platform.TELEGRAM: adapter}
    runner.session_store = session_owner().session_store
    adapter.gateway_runner = runner
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1001")
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setattr(plugin, "_client", FakeGateway())

    async def handler(_event: Any) -> None:
        pass

    adapter.set_message_handler(handler)
    adapter.set_authorization_check(runner._make_adapter_auth_check(adapter.platform))
    assert adapter._is_callback_user_authorized("1001", chat_id="1001") is True
    assert adapter._is_callback_user_authorized("7777", chat_id="7777") is False
    approved = base.MessageEvent(
        text="approved by the gateway",
        message_type=base.MessageType.TEXT,
        raw_message=SimpleNamespace(
            from_user=SimpleNamespace(id=9001),
            chat=SimpleNamespace(id=9001),
        ),
        source=session_module.SessionSource(
            platform=config_module.Platform.TELEGRAM,
            chat_id="9001",
            user_id="9001",
        ),
    )
    assert runner._is_user_authorized(approved.source) is True
    await adapter._message_handler(approved)
    assert runner._is_user_authorized(approved.source) is True
    approved.source.is_bot = True
    monkeypatch.setattr(
        runner, "_bot_loop_guard_instance", lambda: SimpleNamespace(blocked=lambda _key: True)
    )
    assert runner._is_user_authorized(approved.source) is False
    approved.source.is_bot = False
    policy.set_allowed(9001, allowed=False)
    assert adapter._is_callback_user_authorized("9001", chat_id="9001") is False
    # A previously marked source and the process allowlist cannot restore access.
    assert runner._is_user_authorized(approved.source) is False
    policy.set_allowed(1001, allowed=False)
    policy.set_allowed(7777, allowed=True)
    assert policy.snapshot().role(7777) is Role.USER, policy_file.read_text()
    assert adapter._is_callback_user_authorized("1001", chat_id="1001") is False
    assert adapter._is_callback_user_authorized("7777", chat_id="7777") is True
    policy_file.write_text("TELEGRAM_ALLOWED_USERS=invalid\n")
    assert adapter._is_callback_user_authorized("1001", chat_id="1001") is False
    assert runner._is_user_authorized(approved.source) is False
    policy_file.unlink()
    assert adapter._is_callback_user_authorized("1001", chat_id="1001") is False
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    assert runner._is_user_authorized(approved.source) is False
    # Other platforms retain the native authorization contract.
    slack = session_module.SessionSource(
        platform=config_module.Platform.SLACK, chat_id="C1", user_id="U1"
    )
    assert runner._is_user_authorized(slack) is True


async def test_pinned_runtime_callbacks_use_receiving_profile_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_module = pytest.importorskip("gateway.config")
    runner_module = pytest.importorskip("gateway.run")
    session_module = pytest.importorskip("gateway.session")
    primary, secondary = tmp_path / "primary", tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    (primary / ".env").write_text("TELEGRAM_ALLOWED_USERS=1001\n")
    (secondary / ".env").write_text("TELEGRAM_ALLOWED_USERS=1002\n")
    monkeypatch.setenv("HERMES_HOME", str(primary))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1001")
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    adapter = plugin.MediaTelegramAdapter(config_module.PlatformConfig(extra={}))
    adapter.set_owner_profile("secondary")
    runner = object.__new__(runner_module.GatewayRunner)
    runner.config = config_module.GatewayConfig()
    runner.adapters = {}
    runner._profile_adapters = {"secondary": {config_module.Platform.TELEGRAM: adapter}}
    runner._multiplex_on = lambda: True
    runner._routed_profile_home = lambda _name: secondary
    runner.session_store = session_module.SessionStore(tmp_path / "sessions", runner.config)
    adapter.gateway_runner = runner

    async def handler(_event: Any) -> None:
        pass

    adapter.set_message_handler(handler)
    assert adapter._is_callback_user_authorized("1001", chat_id="1001") is False
    adapter.set_authorization_check(
        runner._make_adapter_auth_check(config_module.Platform.TELEGRAM, "secondary")
    )
    assert adapter._is_callback_user_authorized("1001", chat_id="1001") is False
    assert adapter._is_callback_user_authorized("1002", chat_id="1002") is True
    (secondary / ".env").write_text("TELEGRAM_ALLOWED_USERS=\n")
    assert adapter._is_callback_user_authorized("1002", chat_id="1002") is False
    (secondary / ".env").unlink()
    assert adapter._is_callback_user_authorized("1001", chat_id="1001") is False


@pytest.mark.parametrize("user_id", [1001, 9001])
async def test_pinned_runtime_durable_turn_rechecks_live_policy_and_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, user_id: int
) -> None:
    """Exercise the native persisted envelope, preflight and direct runner replay."""
    config_module = pytest.importorskip("gateway.config")
    runner_module = pytest.importorskip("gateway.run")
    session_module = pytest.importorskip("gateway.session")
    base = pytest.importorskip("gateway.platforms.base")
    envelope = pytest.importorskip("gateway.session_envelope")
    ingress = pytest.importorskip("gateway.session_ingress")
    context = pytest.importorskip("gateway.session_ingress_context")
    state = pytest.importorskip("hermes_state")
    runtime = pytest.importorskip("hermes_state_runtime")
    from media_gateway.policy import Policy

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1001")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    policy_file = tmp_path / ".env"
    policy_file.write_text("TELEGRAM_ALLOWED_USERS=1001\nTELEGRAM_ADMIN_USERS=9001\n")
    adapter = plugin.MediaTelegramAdapter(config_module.PlatformConfig(extra={}))
    runner = object.__new__(runner_module.GatewayRunner)
    runner.config = config_module.GatewayConfig()
    runner.adapters = {config_module.Platform.TELEGRAM: adapter}
    runner.session_store = session_module.SessionStore(tmp_path / "sessions", runner.config)
    runner._adapter_credential_fingerprint = lambda _adapter: None
    adapter.gateway_runner = runner
    observed: list[tuple[int, Role | None, bool, bool]] = []

    async def handler(incoming: Any) -> None:
        assert adapter._media_delivery_loop is asyncio.get_running_loop()
        observed.append(
            (
                require_actor().user_id,
                current_role(),
                claim_card_slot(),
                current_event() is incoming,
            )
        )
        assert not claim_card_slot()

    runner._handle_message = handler
    db = state.SessionDB(tmp_path / "state.db")
    authority = SimpleNamespace(
        runner=runner,
        db=db,
        profile_id=str(tmp_path),
        sessions={},
        native_waiters=set(),
        pending_results={},
        pending_deliveries={},
    )
    runner.session_authority = authority
    context.register_transport_home(runner, None, tmp_path)
    monkeypatch.setattr(plugin, "_client", FakeGateway())
    adapter.set_message_handler(runner._primary_message_handler())
    incoming = base.MessageEvent(
        text="queued request",
        message_id="123",
        raw_message=SimpleNamespace(
            from_user=SimpleNamespace(id=user_id), chat=SimpleNamespace(id=-100)
        ),
        source=adapter.build_source(
            chat_id="-100",
            user_id=str(user_id),
            chat_type="group",
        ),
    )
    compat_module.mark_authorized_event(incoming)

    # Do not hydrate profile secrets in this offline synthetic fixture.
    async def execution_scope(_authority: Any, _home: Any) -> Any:
        return nullcontext()

    monkeypatch.setattr(ingress, "_execution_scope", execution_scope)
    try:
        with context.native_callback(runner, incoming, tmp_path):
            payload = await envelope.prepare_native(runner, incoming)
        payload = json.loads(json.dumps(payload))  # No raw update or ContextVar survives storage.
        entry = runner.session_store.get_or_create_session(incoming.source)
        ref = SimpleNamespace(session_id=entry.session_id)
        route = runner.session_store._generate_session_key(incoming.source)
        authority.sessions[ref.session_id] = SimpleNamespace(source=incoming.source, route=route)
        row = {"admission_id": "queued-1", "request_id": "123", "generation": 1, "payload": payload}

        if user_id == 1001:
            Policy(policy_file).set_allowed(user_id, allowed=False)
            with pytest.raises(runtime.RuntimeStoreError):
                await envelope.check_native_route(
                    runner, payload, ref.session_id, incoming.source, adapter
                )
            Policy(policy_file).set_allowed(user_id, allowed=True)
        await envelope.check_native_route(runner, payload, ref.session_id, incoming.source, adapter)
        await ingress.execute_admission(authority, ref, row)
        expected_role = Role.ADMIN if user_id == 9001 else Role.USER
        assert observed == [(user_id, expected_role, True, True)]
        with pytest.raises(TrustError, match="no trusted"):
            require_actor()
        # Replay must reset an unrelated inherited actor and spent card budget too.
        with actor_scope(Actor(user_id=7777, chat_id=7777), Role.ADMIN):
            assert claim_card_slot()
            await ingress.execute_admission(authority, ref, row)
            assert require_actor().user_id == 7777
            assert not claim_card_slot()
        assert observed == [(user_id, expected_role, True, True)] * 2

        if user_id == 1001:
            Policy(policy_file).set_allowed(user_id, allowed=False)
        else:
            policy_file.write_text("TELEGRAM_ALLOWED_USERS=1001\nTELEGRAM_ADMIN_USERS=\n")
        with pytest.raises(runtime.RuntimeStoreError):
            await envelope.check_native_route(
                runner, payload, ref.session_id, incoming.source, adapter
            )
        # Also close a revocation between preflight and execution.
        with pytest.raises(PermissionError):
            await ingress.execute_admission(authority, ref, row)
        assert len(observed) == 2
    finally:
        db.close()


@pytest.mark.parametrize("picker", ["model", "choice"])
@pytest.mark.parametrize("thread_id", [None, "77"])
async def test_pinned_runtime_picker_requires_owner_session_and_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, picker: str, thread_id: str | None
) -> None:
    config_module = pytest.importorskip("gateway.config")
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    policy_file = tmp_path / ".env"
    policy_file.write_text("TELEGRAM_ALLOWED_USERS=1001,1002\nTELEGRAM_ADMIN_USERS=9001\n")
    adapter = plugin.MediaTelegramAdapter(config_module.PlatformConfig(extra={}))
    adapter.gateway_runner = session_owner()
    compat_module.isolate_actor_sessions(adapter)
    selected: list[tuple[Any, ...]] = []
    replies: list[dict[str, Any]] = []

    async def choose(*args: Any) -> str:
        selected.append(args)
        return "selected"

    async def answer(**kwargs: Any) -> None:
        replies.append(kwargs)

    def tap(sender: int, *, message_id: int = 321, topic: str | None = thread_id) -> Any:
        return SimpleNamespace(
            callback_query=SimpleNamespace(
                data="mc:0" if picker == "model" else "cp:0",
                from_user=SimpleNamespace(id=sender, first_name="Caller"),
                message=SimpleNamespace(
                    chat_id=-100,
                    chat=SimpleNamespace(id=-100, type="supergroup", is_forum=topic is not None),
                    message_id=message_id,
                    message_thread_id=int(topic) if topic else None,
                    is_topic_message=topic is not None,
                    from_user=SimpleNamespace(id=42),  # The bot is not the picker owner.
                ),
                answer=answer,
                edit_message_text=answer,
            )
        )

    source = adapter.build_source(
        chat_id="-100",
        user_id="9001",
        chat_type="forum" if thread_id else "group",
        thread_id=thread_id,
    )
    state = {
        "session_key": adapter._source_session_key(source),
        "msg_id": 321,
        "choices": [{"value": "high"}],
        "on_choice_selected": choose,
        "model_list": ["test-model"],
        "selected_provider": "test-provider",
        "on_model_selected": choose,
    }
    states = getattr(adapter, f"_{picker}_picker_state")
    states["-100"] = state
    for update in (tap(1001), tap(1002), tap(9001, message_id=320), tap(9001, topic="88")):
        await adapter._handle_callback_query(update, None)
        assert selected == []
        assert states["-100"] is state
    if picker == "model":
        for action in ("mp:test", "mpg:0", "mpv:0", "mm:0", "mb", "mx", "mg:0"):
            other_user = tap(1001)
            other_user.callback_query.data = action
            await adapter._handle_callback_query(other_user, None)
            assert states["-100"] is state
            assert selected == []
    for field in ("session_key", "msg_id"):
        value = state.pop(field)
        await adapter._handle_callback_query(tap(9001), None)
        assert states["-100"] is state
        assert selected == []
        state[field] = value
    policy_file.write_text("TELEGRAM_ALLOWED_USERS=1001,1002\nTELEGRAM_ADMIN_USERS=\n")
    await adapter._handle_callback_query(tap(9001), None)
    assert states["-100"] is state
    assert selected == []
    policy_file.write_text("TELEGRAM_ALLOWED_USERS=1001,1002\nTELEGRAM_ADMIN_USERS=9001\n")
    await adapter._handle_callback_query(tap(9001), None)
    expected = ("-100", "test-model", "test-provider") if picker == "model" else ("-100", "high")
    assert selected == [expected]
    assert "-100" not in states


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


def test_platform_hint_is_installed_from_code_without_any_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLATFORM_HINT is the only copy, so it must reach the prompt unaided."""

    from hermes_media.compat import install_platform_hint

    def native(agent: Any, platform_key: str, default_hint: str) -> str:
        override = getattr(agent, "_platform_hint_overrides", {}).get(platform_key)
        return f"{default_hint}\n\n{override}".strip() if override else default_hint

    system_prompt = ModuleType("agent.system_prompt")
    system_prompt._resolve_platform_hint = native  # type: ignore[attr-defined]
    agent_pkg = ModuleType("agent")
    agent_pkg.system_prompt = system_prompt  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.system_prompt", system_prompt)

    install_platform_hint(platform="telegram", platform_hint=plugin.PLATFORM_HINT)
    resolve = system_prompt._resolve_platform_hint
    assert getattr(resolve, "__crbl_media__", False) is True

    # No config override anywhere: the guidance still lands, and Hermes' own
    # Telegram formatting hint survives next to it.
    empty = SimpleNamespace(_platform_hint_overrides={})
    effective = resolve(empty, "telegram", "BUILTIN TELEGRAM HINT")
    assert plugin.PLATFORM_HINT in effective
    assert "BUILTIN TELEGRAM HINT" in effective

    # Other platforms are untouched.
    assert resolve(empty, "discord", "DISCORD HINT") == "DISCORD HINT"

    # A config override that already carries the guidance must not double it.
    duplicated = SimpleNamespace(_platform_hint_overrides={"telegram": plugin.PLATFORM_HINT})
    assert resolve(duplicated, "telegram", "BUILTIN").count(plugin.PLATFORM_HINT) == 1

    # Installing twice must not append twice.
    install_platform_hint(platform="telegram", platform_hint=plugin.PLATFORM_HINT)
    assert system_prompt._resolve_platform_hint is resolve

    # The hint is no longer mirrored into any config file.
    assert not hasattr(plugin, "validate_platform_hint")


def _fake_hermes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_registry_get: Any,
    soul: str | None = SOUL_MD,
) -> ModuleType:
    """Install a minimal stand-in for the pinned Hermes runtime.

    ``on_registry_get`` runs when the verifier resolves the Telegram platform,
    mirroring the real runtime where that call is what loads the CRBL plugin
    and installs its patches.
    """

    def module(name: str, **values: Any) -> ModuleType:
        made = ModuleType(name)
        for key, value in values.items():
            setattr(made, key, value)
        monkeypatch.setitem(sys.modules, name, made)
        return made

    system_prompt = module(
        "agent.system_prompt",
        _resolve_platform_hint=lambda _agent, _key, default: default,
    )
    module("agent", system_prompt=system_prompt)
    module(
        "agent.prompt_builder",
        PLATFORM_HINTS={"telegram": "BUILTIN"},
        load_soul_md=lambda _context_length=None: soul,
    )
    module(
        "agent.web_search_registry",
        get_active_search_provider=lambda: SimpleNamespace(name="ddgs", is_available=lambda: True),
        get_active_extract_provider=lambda: None,
    )

    class Registry:
        @staticmethod
        def get(_name: str) -> object:
            on_registry_get(system_prompt)
            return SimpleNamespace(
                plugin_name="crbl-media", platform_hint=f"BUILTIN\n\n{plugin.PLATFORM_HINT}"
            )

    module("gateway.platform_registry", platform_registry=Registry())
    module("gateway", platform_registry=sys.modules["gateway.platform_registry"])
    module("gateway.platforms.base", BasePlatformAdapter=type("B", (), {"gateway_runner": None}))
    module("gateway.platforms")
    module("gateway.run", GatewayRunner=type("R", (), {"_running_agents": property(lambda s: {})}))
    module("run_agent", AIAgent=type("A", (), {"interrupt": lambda self: None}))
    module("toolsets", TOOLSETS={"search": {"tools": ["web_search"]}})

    def resolver(_config: Any, _platform: str) -> object:
        return set()

    resolver.__crbl_media__ = True  # type: ignore[attr-defined]
    module("hermes_cli", tools_config=None)
    module("hermes_cli.tools_config", _get_platform_tools=resolver)
    sys.modules["hermes_cli"].tools_config = sys.modules["hermes_cli.tools_config"]
    module(
        "hermes_cli.version_info",
        get_version_info=lambda: SimpleNamespace(
            display_version=compat_module.PINNED_HERMES_RELEASE.removeprefix("v"),
            commit=compat_module.PINNED_HERMES_COMMIT,
        ),
    )

    adapter = type("TelegramAdapter", (), {"_handle_callback_query": lambda self, u, c: None})
    module("plugins.platforms.telegram.adapter", TelegramAdapter=adapter)
    module("plugins.platforms.telegram.adapter_hint", PLATFORM_HINT="BUILTIN")
    module("plugins.platforms.telegram")
    module("plugins.platforms")
    module("plugins")
    return system_prompt


def test_startup_gate_sees_a_patch_installed_while_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hint installer runs during the verifier, not before it.

    Registration -- and therefore the patch -- happens when the verifier
    resolves the Telegram platform. Reading the resolver through a name
    imported at the top of the verifier would still see the original function
    and fail the check, exiting the container.
    """

    def install(system_prompt: ModuleType) -> None:
        compat_module.install_platform_hint(platform="telegram", platform_hint=plugin.PLATFORM_HINT)

    _fake_hermes(monkeypatch, on_registry_get=install)
    manager = SimpleNamespace(_plugin_tool_names=set(SHARED_TOOLS) | ADMIN_UPSTREAM_TOOLS)

    compat_module.verify_pinned_runtime(
        manager=manager,
        expected_tools=set(SHARED_TOOLS) | ADMIN_UPSTREAM_TOOLS,
        platform_hint=plugin.PLATFORM_HINT,
        soul=SOUL_MD,
    )


def test_startup_gate_fails_closed_when_the_installer_never_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_hermes(monkeypatch, on_registry_get=lambda _system_prompt: None)
    manager = SimpleNamespace(_plugin_tool_names=set(SHARED_TOOLS) | ADMIN_UPSTREAM_TOOLS)

    with pytest.raises(RuntimeError, match="platform-hint installer is not active"):
        compat_module.verify_pinned_runtime(
            manager=manager,
            expected_tools=set(SHARED_TOOLS) | ADMIN_UPSTREAM_TOOLS,
            platform_hint=plugin.PLATFORM_HINT,
            soul=SOUL_MD,
        )


@pytest.mark.parametrize(
    "identity",
    [None, "# Media Request Bot\n\nAn edit somebody made on the NAS."],
    ids=["absent", "drifted"],
)
def test_startup_gate_fails_closed_when_the_host_identity_is_not_ours(
    monkeypatch: pytest.MonkeyPatch, identity: str | None
) -> None:
    """The install is silent by design, so only the read-back can catch it.

    SOUL.md reaches the prompt as a plain file in HERMES_HOME. Hermes logs a
    debug line and carries on when it cannot read it, so a failed install, a
    stale host copy and a truncating context limit all look like a bot that
    simply behaves a little differently.
    """

    def install(system_prompt: ModuleType) -> None:
        compat_module.install_platform_hint(platform="telegram", platform_hint=plugin.PLATFORM_HINT)

    _fake_hermes(monkeypatch, on_registry_get=install, soul=identity)
    manager = SimpleNamespace(_plugin_tool_names=set(SHARED_TOOLS) | ADMIN_UPSTREAM_TOOLS)

    with pytest.raises(RuntimeError, match=r"SOUL\.md is not the identity"):
        compat_module.verify_pinned_runtime(
            manager=manager,
            expected_tools=set(SHARED_TOOLS) | ADMIN_UPSTREAM_TOOLS,
            platform_hint=plugin.PLATFORM_HINT,
            soul=SOUL_MD,
        )


def test_the_identity_names_no_tool_and_no_procedure() -> None:
    """SOUL.md is the tier that must survive the tools being renamed.

    Everything it said about tools is what rotted: it described
    repair_blocked_imports, which no longer exists, arguments that would now be
    rejected, and a numbered-reply picker that was deleted. A tool explains
    itself in its own schema; this file explains who is calling them.
    """

    for name in set(SHARED_TOOLS) | {"repair_blocked_imports"}:
        assert name not in SOUL_MD, name
    for procedure in ("requested_by_", "Which season", "numbered", "Use `"):
        assert procedure not in SOUL_MD, procedure

    # What it must still carry: the honesty rules no tool signature expresses.
    assert "Never invent an ETA" in SOUL_MD
    assert "0/10 episodes" in SOUL_MD
    assert "filesystem paths" in SOUL_MD


def test_installing_the_identity_is_idempotent_but_overwrites_a_hand_edit(tmp_path: Path) -> None:
    home = tmp_path / "hermes-home"

    assert install_soul(home) is True
    assert (home / "SOUL.md").read_text(encoding="utf-8") == SOUL_MD
    # Rewriting on every boot would churn the mtime of a bind-mounted file for
    # no reason, so an unchanged identity is left alone.
    assert install_soul(home) is False

    (home / "SOUL.md").write_text("edited on the NAS\n", encoding="utf-8")
    assert install_soul(home) is True
    assert (home / "SOUL.md").read_text(encoding="utf-8") == SOUL_MD

    # The agent runs as a different user than the init script that writes it.
    assert (home / "SOUL.md").stat().st_mode & 0o044


async def test_only_one_card_is_pushed_per_user_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that searches several titles must not post a poster for each.

    Cards are unsolicited: nobody asked for the second one, and it can land
    long after the user moved on.
    """

    class OneResultGateway(FakeGateway):
        async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((actor.user_id, name, arguments))
            title = str(arguments.get("query"))
            return {
                "query": title,
                "results": [
                    {
                        "media_type": "movie",
                        "tmdb_id": abs(hash(title)) % 9999,
                        "title": title,
                        "year": 2024,
                        "poster_url": "https://image.tmdb.org/x.jpg",
                    }
                ],
                "unavailable_sources": [],
            }

    class CountingAdapter:
        def __init__(self) -> None:
            self._media_delivery_loop = asyncio.get_running_loop()
            self.sent: list[dict[str, Any]] = []
            self._bot = SimpleNamespace(send_photo=self.send_photo)

        async def send_photo(self, **values: Any) -> SimpleNamespace:
            self.sent.append(values)
            return SimpleNamespace(message_id=len(self.sent))

    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = object  # type: ignore[attr-defined]
    telegram.InlineKeyboardMarkup = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    adapter = CountingAdapter()
    monkeypatch.setattr(plugin, "_client", OneResultGateway())
    monkeypatch.setattr(plugin, "_active_adapter", adapter)
    actor = Actor(user_id=1001, chat_id=1001)

    with actor_scope(actor, Role.USER):
        first = json.loads(await plugin._handler("search_media")({"query": "Arrival"}))
        second = json.loads(await plugin._handler("search_media")({"query": "Ex Machina"}))

    assert len(adapter.sent) == 1
    assert first["telegram_presentation"]["poster_cards_delivered"] is True
    assert second["telegram_presentation"]["poster_cards_delivered"] is False
    # The instruction has to agree with the flag. Told a poster was sent when
    # none was, the model answers by pointing at a card nobody can see.
    assert "already sent" in first["telegram_presentation"]["instruction"]
    assert "already sent" not in second["telegram_presentation"]["instruction"]
    # The next message earns its own card.
    with actor_scope(actor, Role.USER):
        await plugin._handler("search_media")({"query": "Dark City"})
    assert len(adapter.sent) == 2


def test_media_caption_stays_within_the_telegram_limit_after_escaping() -> None:
    """Telegram rejects a caption over 1024 characters, escaping included."""

    candidate = {
        "media_type": "movie",
        "tmdb_id": 11,
        "title": "<b>" + "Title " * 60,
        "year": 2026,
        "overview": "<i>" + "Overview " * 300,
    }

    caption = plugin._candidate_caption(candidate)

    assert len(caption) <= 1024
    # The title's angle brackets must arrive escaped, not as live markup.
    assert "&lt;b&gt;" in caption
    assert "<b>Title" not in caption
