from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from hermes_media import compat as compat_module
from hermes_media import plugin
from hermes_media.soul import SOUL_MD, install_soul
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
    # The hint constrains what may be claimed, never which tool to reach for.
    assert "only source of truth for this library" in platform_hint
    assert "never report something as requested unless a tool said so" in platform_hint
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
    with actor_scope(actor, Role.USER, "agent:main:telegram:dm:1001"):
        raw = await plugin._handler("recommend_media")(arguments)

    result = json.loads(raw)
    assert result["telegram_presentation"]["selection_status"] == "conversational"
    assert "conversational" in result["telegram_presentation"]["instruction"].lower()
    assert len(result["results"]) == 4
    assert adapter.photos_sent == []
    assert gateway.calls == [(1001, "recommend_media", arguments)]


async def test_one_resolved_recommendation_sends_its_poster_and_plex_link(
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
                        "tmdb_id": 329865,
                        "title": "Arrival",
                        "year": 2016,
                        "overview": "A linguist tries to understand visitors from another world.",
                        "poster_url": "https://image.tmdb.org/arrival.jpg",
                        "plex_url": "https://watch.plex.tv/movie/arrival",
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
    button = card["reply_markup"].inline_keyboard[0][0]
    assert button.text == "▶ Open in Plex"
    assert button.url == "https://watch.plex.tv/movie/arrival"


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

    for name in set(SHARED_SCHEMAS) | {"repair_blocked_imports"}:
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

    with actor_scope(actor, Role.USER, "agent:main:telegram:dm:1001"):
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
    with actor_scope(actor, Role.USER, "agent:main:telegram:dm:1001"):
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
