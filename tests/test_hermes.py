from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar

import pytest

from hermes_media import compat as compat_module
from hermes_media import plugin
from hermes_media.client import GatewayError
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
    assert "call recommend_media exactly once" in platform_hint
    assert {item["name"] for item in context.tools} == set(SHARED_TOOLS) | ADMIN_UPSTREAM_TOOLS
    search = next(item for item in context.tools if item["name"] == "search_media")
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER):
        result = await search["handler"]({"query": "test"})
    assert result == '{"called":"search_media"}'
    assert gateway.calls == [(1001, "search_media", {"query": "test"})]


async def test_recommendation_turn_redirects_single_title_search_before_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway()
    monkeypatch.setattr(plugin, "_client", gateway)
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(
        actor,
        Role.USER,
        "agent:main:telegram:dm:1001",
        turn_text="Something for tonight",
        recommendation_turn=True,
    ):
        raw = await plugin._handler("search_media")({"query": "Source Code", "media_type": "movie"})

    result = json.loads(raw)
    assert result["results"] == []
    assert result["telegram_presentation"]["selection_status"] == "recommendation_batch_required"
    assert "exactly 4 distinct titles" in result["telegram_presentation"]["instruction"]
    assert gateway.calls == []


async def test_direct_title_still_uses_search_during_lingering_recommendation_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway()
    monkeypatch.setattr(plugin, "_client", gateway)
    monkeypatch.setattr(
        plugin,
        "_recommendation_contexts",
        {"agent:main:telegram:dm:1001": float("inf")},
    )
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(
        actor,
        Role.USER,
        "agent:main:telegram:dm:1001",
        turn_text="Avengers",
        recommendation_turn=True,
    ):
        raw = await plugin._handler("search_media")({"query": "Avengers"})

    assert json.loads(raw) == {"called": "search_media"}
    assert gateway.calls == [(1001, "search_media", {"query": "Avengers"})]
    assert plugin._recommendation_contexts == {}


async def test_search_sends_tabbed_card_and_returns_only_selected_result(
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
            self._bot = SimpleNamespace(send_photo=self.send_photo)
            self.card: dict[str, Any] = {}

        async def send_photo(self, **values: Any) -> SimpleNamespace:
            self.card = values
            return SimpleNamespace(message_id=42)

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

    class Button:
        def __init__(self, text: str, *, callback_data: str) -> None:
            self.text = text
            self.callback_data = callback_data

    class Markup:
        def __init__(self, rows: list[list[Button]]) -> None:
            self.inline_keyboard = rows

    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = Button  # type: ignore[attr-defined]
    telegram.InlineKeyboardMarkup = Markup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setattr(plugin, "_client", gateway)
    monkeypatch.setattr(plugin, "_active_adapter", adapter)
    monkeypatch.setattr(plugin, "_clarify_gateway", lambda: clarify)
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER, "agent:main:telegram:dm:1001"):
        raw = await plugin._handler("search_media")({"query": "House"})

    result = json.loads(raw)
    assert adapter.card["chat_id"] == 1001
    assert adapter.card["photo"] == "https://image.tmdb.org/one.jpg"
    rows = adapter.card["reply_markup"].inline_keyboard
    assert [row[0].text for row in rows[:2]] == [
        "●  🎬 The Last House · 2026",
        "○  📺 House · 2004",
    ]
    assert [button.text for button in rows[-1]] == ["+ Request movie", "Cancel"]
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
    assert result["telegram_presentation"]["provider_mutation_performed"] is False
    assert "request the movie" in result["telegram_presentation"]["next_action"]
    assert "candidate list" in result["telegram_presentation"]["instruction"]
    assert "ask which seasons" in result["telegram_presentation"]["instruction"]


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
    with actor_scope(actor, Role.USER, "agent:main:telegram:dm:1001"):
        raw = await plugin._handler("recommend_media")(arguments)

    result = json.loads(raw)
    assert result["telegram_presentation"]["selection_status"] == "conversational"
    assert "conversational" in result["telegram_presentation"]["instruction"].lower()
    assert len(result["results"]) == 4
    assert adapter.photos_sent == []
    assert gateway.calls == [(1001, "recommend_media", arguments)]


async def test_single_result_request_callback_fires_movie_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[int] = []

    class RequestGateway:
        async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            assert name == "request_movie"
            requested.append(arguments["tmdb_id"])
            return {"status": "requested"}

    class Query:
        data = "md:req:m12345"
        from_user = SimpleNamespace(id=1001)
        message = SimpleNamespace(chat_id=1001, photo=None)

        def __init__(self) -> None:
            self.answers: list[str | None] = []
            self.edits: list[dict[str, Any]] = []

        async def answer(self, text: str | None = None) -> None:
            self.answers.append(text)

        async def edit_message_text(self, **values: Any) -> None:
            self.edits.append(values)

    monkeypatch.setattr(plugin, "_client", RequestGateway())
    query = Query()
    update = SimpleNamespace(callback_query=query)
    assert await plugin._handle_media_picker_callback(update, None)
    assert requested == [12345]
    assert "Requesting" in (query.answers[0] or "")


def test_single_result_button_matches_availability(monkeypatch: pytest.MonkeyPatch) -> None:
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

    def only_button(candidate: dict[str, Any]) -> Button | None:
        markup = _single_result_markup(candidate=candidate)
        if markup is None:
            return None
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        assert len(buttons) == 1
        return buttons[0]

    # A downloaded movie with a resolved slug links straight to Plex.
    watch = only_button(
        {
            "media_type": "movie",
            "tmdb_id": 693134,
            "downloaded": True,
            "plex_url": "https://watch.plex.tv/movie/dune-part-two",
        }
    )
    assert watch is not None
    assert watch.text == "▶ Open in Plex"
    assert watch.url == "https://watch.plex.tv/movie/dune-part-two"

    # Downloaded but Plex exposed no slug: no button beats a wrong one.
    assert only_button({"media_type": "movie", "tmdb_id": 27205, "downloaded": True}) is None

    # Not downloaded: request it.
    request = only_button({"media_type": "movie", "tmdb_id": 27205})
    assert request is not None
    assert request.callback_data == "md:req:m27205"

    # A series always routes to the season picker, which owns availability.
    series = only_button({"media_type": "series", "tvdb_id": 411959})
    assert series is not None
    assert series.callback_data == "md:req:s411959"

    # Even a fully held series keeps it: the picker still reports what is there
    # and can re-search a season. The no-button rule is for movies only.
    held = only_button(
        {"media_type": "series", "tvdb_id": 411959, "downloaded": True, "seasons_missing": []}
    )
    assert held is not None
    assert held.callback_data == "md:req:s411959"


async def test_model_driven_request_retires_the_cards_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`request Dune` requests via the model, so the card's button must go.

    Otherwise the button stays live next to an already-requested title and a
    later tap runs the provider operation a second time.
    """

    class SearchThenRequestGateway(FakeGateway):
        async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((actor.user_id, name, arguments))
            if name == "search_media":
                return {
                    "query": "Dune Part Two",
                    "results": [
                        {
                            "media_type": "movie",
                            "tmdb_id": 693134,
                            "title": "Dune: Part Two",
                            "year": 2024,
                            "poster_url": "https://image.tmdb.org/dune.jpg",
                        }
                    ],
                    "unavailable_sources": [],
                }
            return {"status": "requested", "request_id": 1}

    class Button:
        def __init__(self, text: str, *, callback_data: str = "", url: str = "") -> None:
            self.text = text
            self.callback_data = callback_data
            self.url = url

    class Markup:
        def __init__(self, rows: list[list[Button]]) -> None:
            self.inline_keyboard = rows

    class CardAdapter:
        def __init__(self) -> None:
            self._media_delivery_loop = asyncio.get_running_loop()
            self.cleared: list[dict[str, Any]] = []
            self._bot = SimpleNamespace(
                send_photo=self.send_photo,
                edit_message_reply_markup=self.edit_message_reply_markup,
            )

        async def send_photo(self, **_values: Any) -> SimpleNamespace:
            return SimpleNamespace(message_id=4242)

        async def edit_message_reply_markup(self, **values: Any) -> None:
            self.cleared.append(values)

    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = Button  # type: ignore[attr-defined]
    telegram.InlineKeyboardMarkup = Markup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setattr(plugin, "_single_cards", {})
    adapter = CardAdapter()
    monkeypatch.setattr(plugin, "_client", SearchThenRequestGateway())
    monkeypatch.setattr(plugin, "_active_adapter", adapter)
    actor = Actor(user_id=1001, chat_id=1001)

    with actor_scope(actor, Role.USER, "agent:main:telegram:dm:1001"):
        await plugin._handler("search_media")({"query": "Dune Part Two"})
        assert adapter.cleared == []
        # The same turn then requests it, exactly as the platform hint directs.
        await plugin._handler("request_movie")({"tmdb_id": 693134})

    assert adapter.cleared == [{"chat_id": 1001, "message_id": 4242, "reply_markup": None}]
    # The card is forgotten, so a repeat request cannot edit a stale message.
    assert plugin._single_cards == {}


async def test_single_result_text_card_closes_as_text_not_caption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A posterless card is a text message; python-telegram-bot reports its
    ``photo`` as an empty tuple, so closing it must not use edit_message_caption."""

    class RequestGateway:
        async def call(
            self, _actor: Actor, _name: str, _arguments: dict[str, Any]
        ) -> dict[str, Any]:
            return {"status": "requested"}

    class Query:
        data = "md:req:m12345"
        from_user = SimpleNamespace(id=1001)
        # Exactly what PTB hands back for a message that carries no photo.
        message = SimpleNamespace(chat_id=1001, photo=())

        def __init__(self) -> None:
            self.answers: list[str | None] = []
            self.texts: list[dict[str, Any]] = []

        async def answer(self, text: str | None = None) -> None:
            self.answers.append(text)

        async def edit_message_text(self, **values: Any) -> None:
            self.texts.append(values)

        async def edit_message_caption(self, **_values: Any) -> None:
            raise AssertionError("a posterless card must not be closed as a caption")

    monkeypatch.setattr(plugin, "_client", RequestGateway())
    query = Query()
    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)
    # The button must be cleared, or a second tap fires a duplicate request.
    assert query.texts and query.texts[-1]["reply_markup"] is None


SEVERANCE_SEASONS = {
    "tvdb_id": 371980,
    "title": "Severance",
    "year": 2022,
    "in_sonarr": True,
    "seasons": [
        {
            "number": 0,
            "files": 0,
            "episodes": 21,
            "monitored": False,
            "complete": False,
            "partial": False,
        },
        {
            "number": 1,
            "files": 9,
            "episodes": 9,
            "monitored": True,
            "complete": True,
            "partial": False,
        },
        {
            "number": 2,
            "files": 0,
            "episodes": 10,
            "monitored": False,
            "complete": False,
            "partial": False,
        },
        # Announced but nothing aired: shown, but not something to request.
        {
            "number": 3,
            "files": 0,
            "episodes": 0,
            "monitored": False,
            "complete": False,
            "partial": False,
        },
    ],
}


class SeasonButton:
    def __init__(self, text: str, *, callback_data: str = "", url: str = "") -> None:
        self.text = text
        self.callback_data = callback_data
        self.url = url


class SeasonMarkup:
    def __init__(self, rows: list[list[SeasonButton]]) -> None:
        self.inline_keyboard = rows


class SeasonQuery:
    """One tap on a season picker, recording what the card became."""

    from_user = SimpleNamespace(id=1001)

    def __init__(self, data: str, *, message_id: int = 500) -> None:
        self.data = data
        self.message = SimpleNamespace(chat_id=1001, message_id=message_id, photo=())
        self.answers: list[str | None] = []
        self.markups: list[Any] = []
        self.closed: list[dict[str, Any]] = []

    async def answer(self, text: str | None = None) -> None:
        self.answers.append(text)

    async def edit_message_reply_markup(self, **values: Any) -> None:
        self.markups.append(values.get("reply_markup"))

    async def edit_message_text(self, **values: Any) -> None:
        self.closed.append(values)


class SeasonAdapter:
    def __init__(self) -> None:
        self._media_delivery_loop = asyncio.get_running_loop()
        self.sent: list[dict[str, Any]] = []
        self.cleared: list[dict[str, Any]] = []
        self._bot = SimpleNamespace(
            send_message=self.send_message,
            edit_message_reply_markup=self.edit_message_reply_markup,
        )

    async def send_message(self, **values: Any) -> SimpleNamespace:
        self.sent.append(values)
        return SimpleNamespace(message_id=900)

    async def edit_message_reply_markup(self, **values: Any) -> None:
        self.cleared.append(values)


class SeasonGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, _actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "series_seasons":
            return SEVERANCE_SEASONS
        # Yield, so concurrent taps genuinely interleave here rather than each
        # running to completion before the next starts.
        await asyncio.sleep(0)
        return {"status": "monitoring_updated", "request_id": 3}


@pytest.fixture
async def season_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = SeasonButton  # type: ignore[attr-defined]
    telegram.InlineKeyboardMarkup = SeasonMarkup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setattr(plugin, "_season_pickers", {})
    monkeypatch.setattr(plugin, "_claimed_pickers", {})
    monkeypatch.setattr(plugin, "_single_cards", {})
    gateway = SeasonGateway()
    adapter = SeasonAdapter()
    monkeypatch.setattr(plugin, "_client", gateway)
    monkeypatch.setattr(plugin, "_active_adapter", adapter)
    return SimpleNamespace(gateway=gateway, adapter=adapter)


async def _open_picker(season_env: Any) -> tuple[str, list[SeasonButton]]:
    query = SeasonQuery("md:req:s371980")
    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)
    assert season_env.gateway.calls[0] == ("series_seasons", {"tvdb_id": 371980})
    markup = season_env.adapter.sent[0]["reply_markup"]
    buttons = [b for row in markup.inline_keyboard for b in row]
    picker_id = next(iter(plugin._season_pickers))
    return picker_id, buttons


async def test_series_tap_opens_a_season_picker_with_availability(season_env: Any) -> None:
    """Complete seasons read as done; specials come last and are selectable."""

    picker_id, buttons = await _open_picker(season_env)
    labels = [b.text for b in buttons]

    # Numbered seasons first, specials last, then the shortcut and actions.
    assert labels[0].startswith("☑  S1  ·  complete")
    assert labels[1] == "☐  S2  ·  0/10"
    assert labels[2] == "☐  S3  ·  not aired yet"
    assert labels[3] == "☐  Specials  ·  0/21"
    assert labels[4] == "＋ All missing"
    assert labels[5] == "Request (0)"
    assert labels[6] == "Cancel"
    assert buttons[3].callback_data == f"md:{picker_id}:s0"
    # Nothing is pre-ticked, and the series card's spent button is cleared.
    assert plugin._season_pickers[picker_id].selected == set()
    assert season_env.adapter.cleared == [
        {"chat_id": 1001, "message_id": 500, "reply_markup": None}
    ]


async def test_tapping_a_complete_season_only_explains_itself(season_env: Any) -> None:
    picker_id, _ = await _open_picker(season_env)

    query = SeasonQuery(f"md:{picker_id}:s1")
    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)

    assert query.answers == ["Season 1 is already complete"]
    assert query.markups == []
    assert plugin._season_pickers[picker_id].selected == set()
    assert [name for name, _ in season_env.gateway.calls] == ["series_seasons"]


async def test_season_picker_requests_only_the_ticked_seasons(season_env: Any) -> None:
    picker_id, _ = await _open_picker(season_env)

    # Tick season 2 and the specials, then untick the specials again.
    for action in (f"md:{picker_id}:s2", f"md:{picker_id}:s0", f"md:{picker_id}:s0"):
        query = SeasonQuery(action)
        assert await plugin._handle_media_picker_callback(
            SimpleNamespace(callback_query=query), None
        )
    assert plugin._season_pickers[picker_id].selected == {2}

    go = SeasonQuery(f"md:{picker_id}:go")
    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=go), None)

    assert season_env.gateway.calls[-1] == (
        "request_series",
        {"tvdb_id": 371980, "seasons": [2]},
    )
    assert "S2" in go.closed[-1]["text"]
    assert go.closed[-1]["reply_markup"] is None
    # The picker is spent, so a second tap cannot request again.
    assert picker_id not in plugin._season_pickers


async def test_double_tapped_request_runs_once(season_env: Any) -> None:
    """Two go taps in flight together must not request the same seasons twice.

    The submit path awaits the gateway, so both taps would otherwise pass the
    selection check and run Sonarr's update and season search twice.
    """

    picker_id, _ = await _open_picker(season_env)
    tick = SeasonQuery(f"md:{picker_id}:s2")
    await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=tick), None)

    first = SeasonQuery(f"md:{picker_id}:go")
    second = SeasonQuery(f"md:{picker_id}:go")
    await asyncio.gather(
        plugin._handle_media_picker_callback(SimpleNamespace(callback_query=first), None),
        plugin._handle_media_picker_callback(SimpleNamespace(callback_query=second), None),
    )

    requests = [args for name, args in season_env.gateway.calls if name == "request_series"]
    assert requests == [{"tvdb_id": 371980, "seasons": [2]}]
    # The loser is told why nothing happened rather than silently ignored.
    answers = first.answers + second.answers
    assert "Already requested" in answers
    assert picker_id not in plugin._season_pickers


async def test_all_missing_shortcut_skips_complete_seasons(season_env: Any) -> None:
    picker_id, _ = await _open_picker(season_env)

    query = SeasonQuery(f"md:{picker_id}:all")
    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)

    # Season 3 has not aired, so the shortcut leaves it alone.
    assert plugin._season_pickers[picker_id].selected == {0, 2}
    assert query.answers == ["All missing seasons selected"]


async def test_season_picker_refuses_an_empty_request(season_env: Any) -> None:
    picker_id, _ = await _open_picker(season_env)

    query = SeasonQuery(f"md:{picker_id}:go")
    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)

    assert query.answers == ["Pick at least one season first"]
    assert [name for name, _ in season_env.gateway.calls] == ["series_seasons"]
    assert picker_id in plugin._season_pickers


async def test_season_picker_rejects_another_users_tap(season_env: Any) -> None:
    picker_id, _ = await _open_picker(season_env)

    query = SeasonQuery(f"md:{picker_id}:s2")
    query.from_user = SimpleNamespace(id=7777)
    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)

    assert query.answers == ["⛔ This media card belongs to another request."]
    assert plugin._season_pickers[picker_id].selected == set()


async def test_media_card_switches_poster_then_requests_active_movie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClarifyGateway:
        response = ""

        @classmethod
        def resolve_gateway_clarify(cls, _clarify_id: str, response: str) -> bool:
            cls.response = response
            return True

    class Query:
        def __init__(self) -> None:
            self.data = "md:picker-1:v1"
            self.from_user = SimpleNamespace(id=1001)
            self.message = SimpleNamespace(chat_id=1001)
            self.answers: list[str | None] = []
            self.edits: list[dict[str, Any]] = []

        async def answer(self, text: str | None = None) -> None:
            self.answers.append(text)

        async def edit_message_media(self, **values: Any) -> None:
            self.edits.append(values)

        async def edit_message_caption(self, **values: Any) -> None:
            self.edits.append(values)

    class Button:
        def __init__(self, text: str, *, callback_data: str) -> None:
            self.text = text
            self.callback_data = callback_data

    class Markup:
        def __init__(self, rows: list[list[Button]]) -> None:
            self.inline_keyboard = rows

    class Media:
        def __init__(self, **values: Any) -> None:
            self.values = values

    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = Button  # type: ignore[attr-defined]
    telegram.InlineKeyboardMarkup = Markup  # type: ignore[attr-defined]
    telegram.InputMediaPhoto = Media  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setattr(plugin, "_clarify_gateway", lambda: ClarifyGateway())
    pending = plugin._PendingMediaPicker(
        clarify_id="picker-1",
        choices=(
            "House (2004) · Series · TVDB 22",
            "The Last House (2026) · Movie · TMDB 11",
        ),
        candidates=(
            {
                "media_type": "series",
                "tvdb_id": 22,
                "title": "House",
                "year": 2004,
                "poster_url": "https://artworks.thetvdb.com/house.jpg",
            },
            {
                "media_type": "movie",
                "tmdb_id": 11,
                "title": "The Last House",
                "year": 2026,
                "poster_url": "https://image.tmdb.org/last-house.jpg",
            },
        ),
        posters=(
            "https://artworks.thetvdb.com/house.jpg",
            "https://image.tmdb.org/last-house.jpg",
        ),
        actor_user_id=1001,
        actor_chat_id=1001,
        has_photo=True,
    )
    monkeypatch.setattr(plugin, "_pending_pickers", {})
    plugin._set_pending_picker("session-1", pending)
    query = Query()

    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)
    assert pending.active_index == 1
    media = query.edits[-1]["media"]
    assert media.values["media"] == "https://image.tmdb.org/last-house.jpg"
    rows = query.edits[-1]["reply_markup"].inline_keyboard
    assert rows[1][0].text == "●  🎬 The Last House · 2026"
    assert rows[-1][0].text == "+ Request movie"

    class RequestGateway:
        calls: ClassVar[list[tuple[int, str, dict[str, Any]]]] = []

        async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            RequestGateway.calls.append((actor.user_id, name, arguments))
            return {"request_id": 7, "status": "search_started"}

    monkeypatch.setattr(plugin, "_gateway", lambda: RequestGateway())

    query.data = "md:picker-1:select"
    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)
    # The tap performed the request itself; the model only narrates it.
    assert RequestGateway.calls == [(1001, "request_movie", {"tmdb_id": 11})]
    assert ClarifyGateway.response == (
        plugin.MEDIA_PICKER_REQUESTED + "search_started:The Last House (2026) · Movie · TMDB 11"
    )
    assert "Requesting…" in query.answers
    closed = query.edits[-1]
    assert closed["reply_markup"] is None
    assert "Requested ✓" in closed["caption"]


def _media_card_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    """One delivered two-tab card: a series tab open, a movie tab closed."""

    class ClarifyGateway:
        response: str | None = None
        resolvable = True

        @classmethod
        def resolve_gateway_clarify(cls, _clarify_id: str, response: str) -> bool:
            if not cls.resolvable:
                return False
            cls.response = response
            return True

    class Query:
        def __init__(self, *, data: str, user_id: int = 1001, chat_id: int = 1001) -> None:
            self.data = data
            self.from_user = SimpleNamespace(id=user_id)
            self.message = SimpleNamespace(chat_id=chat_id)
            self.answers: list[str | None] = []
            self.edits: list[dict[str, Any]] = []
            self.fail_edit = False

        async def answer(self, text: str | None = None) -> None:
            self.answers.append(text)

        async def edit_message_media(self, **values: Any) -> None:
            if self.fail_edit:
                raise RuntimeError("telegram rejected the media edit")
            self.edits.append(values)

        async def edit_message_caption(self, **values: Any) -> None:
            self.edits.append(values)

    class Button:
        def __init__(self, text: str, *, callback_data: str) -> None:
            self.text = text
            self.callback_data = callback_data

    class Markup:
        def __init__(self, rows: list[list[Button]]) -> None:
            self.inline_keyboard = rows

    class Media:
        def __init__(self, **values: Any) -> None:
            self.values = values

    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = Button  # type: ignore[attr-defined]
    telegram.InlineKeyboardMarkup = Markup  # type: ignore[attr-defined]
    telegram.InputMediaPhoto = Media  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setattr(plugin, "_clarify_gateway", lambda: ClarifyGateway())
    monkeypatch.setattr(plugin, "_pending_pickers", {})
    pending = plugin._PendingMediaPicker(
        clarify_id="picker-1",
        choices=(
            "House (2004) · Series · TVDB 22",
            "The Last House (2026) · Movie · TMDB 11",
        ),
        candidates=(
            {"media_type": "series", "tvdb_id": 22, "title": "House", "year": 2004},
            {"media_type": "movie", "tmdb_id": 11, "title": "The Last House", "year": 2026},
        ),
        posters=("https://artworks.thetvdb.com/house.jpg", "https://image.tmdb.org/last.jpg"),
        actor_user_id=1001,
        actor_chat_id=1001,
        has_photo=True,
    )
    plugin._set_pending_picker("session-1", pending)
    return ClarifyGateway, Query, pending


async def test_media_card_rejects_taps_from_another_telegram_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clarify, query_cls, pending = _media_card_fixture(monkeypatch)
    intruder = query_cls(data="md:picker-1:select", user_id=2002)

    assert await plugin._handle_media_picker_callback(
        SimpleNamespace(callback_query=intruder), None
    )
    assert clarify.response is None
    assert intruder.edits == []
    assert "another request" in (intruder.answers[-1] or "")
    assert pending.active_index == 0


async def test_media_card_cancel_resolves_without_requesting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clarify, query_cls, _pending = _media_card_fixture(monkeypatch)
    interrupted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        plugin,
        "interrupt_running_turn",
        lambda _adapter, session_key, reason: interrupted.append((session_key, reason)) or True,
    )
    query = query_cls(data="md:picker-1:cancel")

    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)
    assert clarify.response == plugin.MEDIA_PICKER_CANCELLED
    assert query.answers[-1] == "Cancelled"
    assert query.edits[-1]["reply_markup"] is None
    assert "cancelled" in query.edits[-1]["caption"].casefold()
    assert interrupted == [("session-1", "Telegram media picker cancelled")]


async def test_failed_tab_switch_keeps_the_displayed_candidate_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clarify, query_cls, pending = _media_card_fixture(monkeypatch)
    query = query_cls(data="md:picker-1:v1")
    query.fail_edit = True

    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)
    assert pending.active_index == 0

    query.data = "md:picker-1:select"
    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)
    # The card still displayed the series tab, so the series must be what
    # resolves - never the movie the user never saw opened.
    assert clarify.response == "House (2004) · Series · TVDB 22"


async def test_failed_gateway_request_closes_card_without_claiming_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clarify, query_cls, pending = _media_card_fixture(monkeypatch)

    class FailingGateway:
        async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            raise GatewayError("radarr is down")

    monkeypatch.setattr(plugin, "_gateway", lambda: FailingGateway())
    plugin._commit_picker_tab(pending, 1, True)
    query = query_cls(data="md:picker-1:select")

    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)
    assert clarify.response == (
        plugin.MEDIA_PICKER_REQUEST_FAILED + "The Last House (2026) · Movie · TMDB 11"
    )
    closed = query.edits[-1]
    assert "Request failed" in closed["caption"]
    assert "Requested ✓" not in closed["caption"]


async def test_performed_request_reports_outcome_and_forbids_second_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choice = "The Last House (2026) · Movie · TMDB 11"

    async def already_requested(*_args: Any, **_kwargs: Any) -> str:
        return plugin.MEDIA_PICKER_REQUESTED + "requested:" + choice

    monkeypatch.setattr(plugin, "_select_search_result", already_requested)
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER, "session-1"):
        result = await plugin._decorate_search_result(
            1001,
            "session-1",
            {
                "query": "last house",
                "results": [
                    {"media_type": "movie", "tmdb_id": 11, "title": "The Last House", "year": 2026},
                    {"media_type": "movie", "tmdb_id": 22, "title": "House", "year": 2024},
                ],
            },
        )
    presentation = result["telegram_presentation"]
    assert presentation["selection_status"] == "requested"
    assert presentation["request_status"] == "requested"
    assert presentation["provider_mutation_performed"] is True
    assert "Never call request_movie" in presentation["instruction"]
    assert result["results"][0]["tmdb_id"] == 11


async def test_failed_request_marker_tells_model_nothing_was_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choice = "The Last House (2026) · Movie · TMDB 11"

    async def failed(*_args: Any, **_kwargs: Any) -> str:
        return plugin.MEDIA_PICKER_REQUEST_FAILED + choice

    monkeypatch.setattr(plugin, "_select_search_result", failed)
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER, "session-1"):
        result = await plugin._decorate_search_result(
            1001,
            "session-1",
            {
                "query": "last house",
                "results": [
                    {"media_type": "movie", "tmdb_id": 11, "title": "The Last House", "year": 2026},
                    {"media_type": "movie", "tmdb_id": 22, "title": "House", "year": 2024},
                ],
            },
        )
    presentation = result["telegram_presentation"]
    assert presentation["selection_status"] == "request_failed"
    assert presentation["provider_mutation_performed"] is False
    assert "nothing was" in presentation["instruction"]


async def test_expired_media_card_tap_is_reported_and_requests_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clarify, query_cls, _pending = _media_card_fixture(monkeypatch)
    clarify.resolvable = False
    query = query_cls(data="md:picker-1:select")

    assert await plugin._handle_media_picker_callback(SimpleNamespace(callback_query=query), None)
    assert clarify.response is None
    assert "expired" in (query.answers[-1] or "")


def test_media_caption_stays_within_the_telegram_limit_after_escaping() -> None:
    caption = plugin._candidate_caption(
        {
            "media_type": "movie",
            "title": "A&B <Title> " * 40,
            "year": 2026,
            "overview": "<&>" * 400,
        }
    )
    assert len(caption) <= 1024
    assert "<b>" in caption


async def test_single_search_result_sends_card_with_action_button(
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
    markup = card["reply_markup"]
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    assert len(buttons) == 1
    assert buttons[0].text == "＋ Request"
    assert buttons[0].callback_data == "md:req:s411959"
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


async def test_new_text_supersedes_picker_and_continues_as_fresh_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClarifyGateway:
        def __init__(self) -> None:
            self.responses: dict[str, str] = {}

        def resolve_gateway_clarify(self, clarify_id: str, response: str) -> bool:
            self.responses[clarify_id] = response
            return True

        def wait_for_response(self, clarify_id: str, _timeout: float) -> str:
            return self.responses[clarify_id]

    gateway = FakeGateway()
    clarify = ClarifyGateway()
    monkeypatch.setattr(plugin, "_client", gateway)
    monkeypatch.setattr(plugin, "_clarify_gateway", lambda: clarify)
    pending = plugin._PendingMediaPicker(
        clarify_id="picker-1",
        choices=(
            "The Last House (2026) · Movie · TMDB 11",
            "The Last House (2024) · Movie · TMDB 22",
        ),
    )
    plugin._set_pending_picker("agent:main:telegram:dm:1001", pending)
    adapter = plugin.MediaTelegramAdapter(SimpleNamespace(extra={}))
    monkeypatch.setattr(plugin, "_active_adapter", adapter)

    incoming = text_event("avengers")
    assert await adapter.handle_message(incoming) is incoming
    assert clarify.responses == {"picker-1": plugin.MEDIA_PICKER_SUPERSEDED}
    assert plugin._get_pending_picker("agent:main:telegram:dm:1001") is None
    assert gateway.calls == []


async def test_number_year_and_exact_title_resolve_picker_without_new_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices = (
        "The Last House (2026) · Movie · TMDB 11",
        "The Last House (2024) · Movie · TMDB 22",
        "Sell Your House (2026) · Movie · TMDB 33",
    )
    assert plugin._picker_choice("2", choices) == choices[1]
    assert plugin._picker_choice("2024", choices) == choices[1]
    assert plugin._picker_choice("Sell Your House", choices) == choices[2]
    assert plugin._picker_choice("2026", choices) is None
    assert plugin._picker_choice("avengers", choices) is None

    class ClarifyGateway:
        def __init__(self) -> None:
            self.response = ""

        def resolve_gateway_clarify(self, _clarify_id: str, response: str) -> bool:
            self.response = response
            return True

        def wait_for_response(self, _clarify_id: str, _timeout: float) -> str:
            return self.response

    gateway = FakeGateway()
    clarify = ClarifyGateway()
    monkeypatch.setattr(plugin, "_client", gateway)
    monkeypatch.setattr(plugin, "_clarify_gateway", lambda: clarify)
    plugin._set_pending_picker(
        "agent:main:telegram:dm:1001",
        plugin._PendingMediaPicker(clarify_id="picker-2", choices=choices),
    )
    adapter = plugin.MediaTelegramAdapter(SimpleNamespace(extra={}))
    monkeypatch.setattr(plugin, "_active_adapter", adapter)

    assert await adapter.handle_message(text_event("2")) is None
    assert clarify.response == choices[1]
    assert plugin._get_pending_picker("agent:main:telegram:dm:1001") is None
    assert gateway.calls == []


def test_media_picker_timeout_is_short_and_bounded() -> None:
    assert plugin._picker_timeout(60) == 60
    assert plugin._picker_timeout(600) == 120
    assert plugin._picker_timeout(0) == 120


async def test_unanswered_picker_expires_without_returning_a_candidate(
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

    class RunningAgent:
        def __init__(self) -> None:
            self.interruptions = 0

        def interrupt(self) -> None:
            self.interruptions += 1

    running_agent = RunningAgent()

    class PresentationAdapter:
        def __init__(self) -> None:
            self._media_delivery_loop = asyncio.get_running_loop()
            self._bot = SimpleNamespace(send_message=self.send_message)
            self.gateway_runner = SimpleNamespace(
                _running_agents={"agent:main:telegram:dm:1001": running_agent}
            )

        @staticmethod
        async def send_message(**_values: Any) -> SimpleNamespace:
            return SimpleNamespace(message_id=42)

    class ClarifyGateway:
        timeout = 0.0

        @staticmethod
        def register(**_values: Any) -> None:
            return None

        @staticmethod
        def resolve_gateway_clarify(_clarify_id: str, _response: str) -> bool:
            return True

        @classmethod
        def wait_for_response(cls, _clarify_id: str, timeout: float) -> None:
            cls.timeout = timeout
            return None

        @staticmethod
        def get_clarify_timeout() -> int:
            return 600

    class Button:
        def __init__(self, text: str, *, callback_data: str) -> None:
            self.text = text
            self.callback_data = callback_data

    class Markup:
        def __init__(self, rows: list[list[Button]]) -> None:
            self.inline_keyboard = rows

    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = Button  # type: ignore[attr-defined]
    telegram.InlineKeyboardMarkup = Markup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setattr(plugin, "_client", SearchGateway())
    monkeypatch.setattr(plugin, "_active_adapter", PresentationAdapter())
    monkeypatch.setattr(plugin, "_clarify_gateway", lambda: ClarifyGateway())
    actor = Actor(user_id=1001, chat_id=1001)
    with actor_scope(actor, Role.USER, "agent:main:telegram:dm:1001"):
        raw = await plugin._handler("search_media")({"query": "House"})

    result = json.loads(raw)
    assert ClarifyGateway.timeout == plugin.MEDIA_PICKER_TIMEOUT_SECONDS
    assert result["results"] == []
    assert result["telegram_presentation"]["selection_status"] == "expired"
    assert "without a user-facing response" in result["telegram_presentation"]["instruction"]
    assert running_agent.interruptions == 1


def test_turn_interrupt_uses_exact_session_without_queuing_a_message() -> None:
    class RunningAgent:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def interrupt(self, *arguments: object) -> None:
            self.calls.append(arguments)

    current = RunningAgent()
    other = RunningAgent()
    adapter = SimpleNamespace(
        gateway_runner=SimpleNamespace(
            _running_agents={
                "agent:main:telegram:dm:1001": current,
                "agent:main:telegram:dm:2002": other,
            }
        )
    )

    assert plugin.interrupt_running_turn(adapter, "agent:main:telegram:dm:1001", "picker expired")
    # An argument would become Hermes' next queued message and restart the turn.
    assert current.calls == [()]
    assert other.calls == []


def test_turn_interrupt_fails_closed_without_the_owning_agent() -> None:
    adapter = SimpleNamespace(gateway_runner=SimpleNamespace(_running_agents={}))
    assert not plugin.interrupt_running_turn(
        adapter, "agent:main:telegram:dm:1001", "picker expired"
    )


def test_search_presentation_keeps_card_numbers_aligned_after_invalid_rows() -> None:
    cards, choices, candidates, posters = plugin._search_presentation(
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
    # posters stay index-aligned with candidates; cards keep only artwork rows,
    # so a tab must never read its poster out of cards.
    assert posters == ["https://image.tmdb.org/house.jpg"]
    assert len(posters) == len(candidates)


def test_search_tool_contract_owns_native_telegram_selection() -> None:
    text = SHARED_SCHEMAS["search_media"]["description"]
    assert "before the tool returns" in text
    assert "call clarify" in text
    assert "Never use MEDIA" in text
    # Availability is the providers' answer, so the contract points at their
    # fields rather than at a Plex lookup.
    assert "plex_url on a lone downloaded movie" in text
    assert "downloaded" in plugin.PLATFORM_HINT
    recommendation_schema = SHARED_SCHEMAS["recommend_media"]
    recommendation = recommendation_schema["description"]
    assert "conversational reply" in recommendation
    assert "instead of calling search_media separately" in recommendation
    assert recommendation_schema["inputSchema"]["properties"]["titles"]["minItems"] == 4


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


def _fake_hermes(monkeypatch: pytest.MonkeyPatch, *, on_registry_get: Any) -> ModuleType:
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
    module("agent.prompt_builder", PLATFORM_HINTS={"telegram": "BUILTIN"})
    module(
        "agent.web_search_registry",
        get_active_search_provider=lambda: SimpleNamespace(name="ddgs", is_available=lambda: True),
        get_active_extract_provider=lambda: None,
    )

    class Registry:
        @staticmethod
        def get(_name: str) -> object:
            on_registry_get(system_prompt)
            return SimpleNamespace(plugin_name="crbl-media")

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

    adapter = type("TelegramAdapter", (), {"_handle_callback_query": lambda self, u, c: None})
    module("plugins.platforms.telegram.adapter", TelegramAdapter=adapter)
    module("plugins.platforms.telegram")
    module("plugins.platforms")
    module("plugins")
    monkeypatch.setattr(
        "hermes_media.compat.importlib.metadata.version",
        lambda _name: compat_module.PINNED_HERMES_PACKAGE_VERSION,
    )
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
        )
