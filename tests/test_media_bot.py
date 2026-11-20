from __future__ import annotations

import asyncio
import base64
import datetime
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from media_bot import bot as bot_module
from media_bot import egress, search
from media_bot import model as model_module
from media_bot.bot import (
    CUT_OFF,
    FRESH_START,
    GREETING,
    MEDIA_DOWN,
    MODEL_DOWN,
    REFUSED,
    TOOL_RESULT_CHARS,
    WEB_SEARCH_CAP,
    Bot,
    Settings,
    actor_from_message,
    split_message,
    to_html,
)
from media_bot.client import GatewayClient, GatewayError
from media_bot.history import ConversationStore
from media_bot.managed_app import project_bot_files
from media_bot.model import Model, ModelBusy, ModelCutOff, ModelError, ModelRefused
from media_bot.prompt import SOUL_MD
from media_bot.telegram import PHOTO_MAX_BYTES, Telegram, TelegramError
from media_gateway.constants import SHARED_TOOLS
from media_gateway.types import Actor, Role

USER_TOOLS = [
    {"name": "search_media", "description": "Search", "inputSchema": {"type": "object"}},
    {"name": "request_movie", "description": "Request", "inputSchema": {"type": "object"}},
]
HELD = {
    "title": "Dune <Part Two>",
    "year": 2024,
    "media_type": "movie",
    "tmdb_id": 693134,
    "overview": "Paul & Chani",
    "poster_url": "https://image.tmdb.org/t/p/poster.jpg",
    "plex_url": "https://watch.plex.tv/movie/dune-part-two",
}


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.cards: list[dict[str, Any]] = []
        self.reject_html = False
        self.photo: bytes | Exception = b"jpeg-bytes"
        self.downloads: list[str] = []

    async def download_photo(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        if isinstance(self.photo, Exception):
            raise self.photo
        return self.photo

    async def send_message(
        self, chat_id: int, text: str, *, html: bool = False, reply_to: int | None = None
    ) -> None:
        if html and self.reject_html:
            raise TelegramError("bad entities", status=400)
        self.messages.append({"chat_id": chat_id, "text": text, "html": html, "reply_to": reply_to})

    async def send_card(self, chat_id: int, **card: Any) -> None:
        self.cards.append({"chat_id": chat_id, **card})

    async def typing(self, chat_id: int) -> None:
        return None


class FakeModel:
    """Replays scripted assistant messages and records what each round was shown."""

    def __init__(self, *replies: list[dict[str, Any]] | Exception) -> None:
        self.replies = list(replies)
        self.systems: list[str] = []
        self.seen: list[list[dict[str, Any]]] = []
        self.tools: list[list[str]] = []

    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        self.systems.append(system)
        self.seen.append([dict(message) for message in messages])
        self.tools.append([tool["name"] for tool in tools])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeGateway:
    def __init__(self, role: Role = Role.USER) -> None:
        self.role = role
        self.calls: list[tuple[Actor, str, dict[str, Any]]] = []
        self.results: dict[str, Any] = {}

    async def observe(self, actor: Actor) -> Role:
        return self.role

    async def tools(self, actor: Actor) -> list[dict[str, Any]]:
        return USER_TOOLS

    async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((actor, name, arguments))
        value = self.results.get(name, {"ok": True})
        if isinstance(value, Exception):
            raise value
        return dict(value)


def tool_call(name: str, arguments: Any, call_id: str = "c1") -> list[dict[str, Any]]:
    return [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}]


def answer(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def message(text: str | None = "hello", *, chat_type: str = "private", **extra: Any) -> dict:
    chat_id = 42 if chat_type == "private" else -100
    value: dict[str, Any] = {
        "message_id": 7,
        "from": {"id": 42, "is_bot": False, "first_name": "Ada"},
        "chat": {"id": chat_id, "type": chat_type},
        **extra,
    }
    if text is not None:
        value["text"] = text
    return value


def make_bot(
    tmp_path: Path, model: FakeModel, gateway: FakeGateway | None = None
) -> tuple[Bot, FakeTelegram, FakeGateway]:
    telegram = FakeTelegram()
    gateway = gateway or FakeGateway()
    store = ConversationStore(tmp_path / "conversations.sqlite3", idle_seconds=7200)
    bot = Bot(telegram, model, gateway, tmp_path / "heartbeat", store)  # type: ignore[arg-type]
    bot.username, bot.bot_id = "crbl_media_bot", 999
    return bot, telegram, gateway


def tool_result(model: FakeModel, round_index: int) -> dict[str, Any]:
    (block,) = model.seen[round_index][-1]["content"]
    assert block["type"] == "tool_result"
    return dict(json.loads(block["content"]))


async def test_single_result_sends_one_card_and_tells_the_model(tmp_path: Path) -> None:
    model = FakeModel(
        tool_call("search_media", {"query": "Dune"}, "a"),
        tool_call("search_media", {"query": "Dune 2"}, "b"),
        answer("It's on **Plex**."),
    )
    bot, telegram, gateway = make_bot(tmp_path, model)
    gateway.results["search_media"] = {"results": [HELD, {"title": "No ID"}]}

    await bot.handle(message("Do we have Dune?"))

    assert [name for _, name, _ in gateway.calls] == ["search_media", "search_media"]
    assert gateway.calls[0][0] == Actor(user_id=42, chat_id=42, first_name="Ada")
    assert len(telegram.cards) == 1
    card = telegram.cards[0]
    assert card["poster_url"] == HELD["poster_url"]
    assert card["button_url"] == HELD["plex_url"]
    assert "Dune &lt;Part Two&gt; (2024)" in card["caption"]
    assert "Paul &amp; Chani" in card["caption"]
    first, second = tool_result(model, 1), tool_result(model, 2)
    assert first["results"] == [HELD]
    assert first["telegram_presentation"]["poster_cards_delivered"] is True
    assert second["telegram_presentation"]["poster_cards_delivered"] is False
    # Only the lookup that posted a poster tells the model not to repeat it.
    assert "do not repeat the synopsis" in first["telegram_presentation"]["instruction"]
    assert "synopsis" not in second["telegram_presentation"]["instruction"]
    assert telegram.messages == [
        {"chat_id": 42, "text": "It's on <b>Plex</b>.", "html": True, "reply_to": None}
    ]


async def test_a_repeat_lookup_of_the_same_title_sends_no_second_poster(tmp_path: Path) -> None:
    other = {**HELD, "title": "Arrival", "tmdb_id": 329865}
    lookup = tool_call("search_media", {"query": "x"})
    model = FakeModel(
        lookup,
        answer("We don't have it. Should I get it?"),
        lookup,
        tool_call("request_movie", {"tmdb_id": 693134}),
        answer("Done."),
        lookup,
        answer("Arrival is on Plex."),
        lookup,
        answer("Still not here."),
        lookup,
        answer("Not yet."),
    )
    bot, telegram, gateway = make_bot(tmp_path, model)

    gateway.results["search_media"] = {"results": [HELD]}
    await bot.handle(message("Do we have Dune?"))
    await bot.handle(message("yes"))
    assert len(telegram.cards) == 1
    # The model is not told a poster went out with the second lookup.
    assert tool_result(model, 3)["telegram_presentation"]["poster_cards_delivered"] is False

    # Another title in between makes the first one news again.
    gateway.results["search_media"] = {"results": [other]}
    await bot.handle(message("And Arrival?"))
    gateway.results["search_media"] = {"results": [HELD]}
    await bot.handle(message("Is Dune here yet?"))
    assert ["Arrival" in card["caption"] for card in telegram.cards] == [False, True, False]

    # A fresh conversation forgets the last poster.
    await bot.handle(message("/new"))
    await bot.handle(message("Dune?"))
    assert len(telegram.cards) == 4


async def test_several_matches_are_left_to_the_conversation(tmp_path: Path) -> None:
    model = FakeModel(tool_call("search_media", {"query": "Dune"}), answer("Which one?"))
    bot, telegram, gateway = make_bot(tmp_path, model)
    gateway.results["search_media"] = {"results": [HELD, {**HELD, "tmdb_id": 438631}]}

    await bot.handle(message("Dune"))

    assert telegram.cards == []
    presentation = tool_result(model, 1)["telegram_presentation"]
    assert presentation["selection_status"] == "conversational"
    assert "numbered list" in presentation["instruction"]


async def test_blocked_users_reach_neither_the_model_nor_the_chat(tmp_path: Path) -> None:
    model = FakeModel()
    bot, telegram, _ = make_bot(tmp_path, model, FakeGateway(Role.BLOCKED))

    await bot.handle(message("Add Dune"))

    assert model.seen == []
    assert telegram.messages == []


async def test_only_the_tools_granted_to_the_role_can_run(tmp_path: Path) -> None:
    model = FakeModel(tool_call("radarr_delete_movie", {"id": 1}), answer("I can't do that."))
    bot, _, gateway = make_bot(tmp_path, model)

    await bot.handle(message("Delete Dune"))

    assert model.tools[0] == ["search_media", "request_movie", "web_search"]
    assert gateway.calls == []
    assert tool_result(model, 1) == {"error": "unknown tool"}


async def test_gateway_errors_and_bad_arguments_return_to_the_model(tmp_path: Path) -> None:
    broken = tool_call("request_movie", "not an object")
    model = FakeModel(broken, tool_call("request_movie", {"tmdb_id": 1}), answer("No luck."))
    bot, telegram, gateway = make_bot(tmp_path, model)
    gateway.results["request_movie"] = GatewayError("movie is already requested")

    await bot.handle(message("Add it"))

    assert tool_result(model, 1) == {"error": "tool arguments must be a JSON object"}
    assert tool_result(model, 2) == {"error": "movie is already requested"}
    assert telegram.messages[-1]["text"] == "No luck."


async def test_web_search_is_capped_per_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    searches: list[str] = []

    async def fake_search(arguments: dict[str, Any]) -> dict[str, Any]:
        searches.append(arguments["query"])
        return {"results": []}

    monkeypatch.setattr(bot_module.search, "web_search", fake_search)
    calls = [tool_call("web_search", {"query": f"q{index}"}) for index in range(WEB_SEARCH_CAP + 1)]
    model = FakeModel(*calls, answer("Done."))
    bot, _, _ = make_bot(tmp_path, model)

    await bot.handle(message("Research everything"))

    assert len(searches) == WEB_SEARCH_CAP
    assert "limit" in tool_result(model, WEB_SEARCH_CAP + 1)["error"]


async def test_history_is_kept_per_user_and_reset_on_request(tmp_path: Path) -> None:
    model = FakeModel(answer("First."), answer("Second."), answer("Third."))
    bot, telegram, _ = make_bot(tmp_path, model)

    await bot.handle(message("one"))
    await bot.handle(message("two"))
    await bot.handle(message("/new"))
    await bot.handle(message("three"))

    assert [item["content"] for item in model.seen[1]] == ["one", "First.", "two"]
    assert [item["content"] for item in model.seen[2]] == ["three"]
    assert telegram.messages[2]["text"] == FRESH_START


async def test_model_failure_is_reported_and_not_remembered(tmp_path: Path) -> None:
    model = FakeModel(ModelError("down"), answer("Back."))
    bot, telegram, _ = make_bot(tmp_path, model)

    await bot.handle(message("one"))
    await bot.handle(message("two"))

    assert telegram.messages[0]["text"] == MODEL_DOWN
    assert [item["content"] for item in model.seen[1]] == ["two"]


async def test_a_cut_off_reply_and_a_refusal_say_what_happened(tmp_path: Path) -> None:
    model = FakeModel(ModelCutOff("cut"), ModelRefused("declined"), answer("Back."))
    bot, telegram, _ = make_bot(tmp_path, model)

    await bot.handle(message("list everything"))
    await bot.handle(message("something refused"))
    await bot.handle(message("three"))

    assert [item["text"] for item in telegram.messages] == [CUT_OFF, REFUSED, "Back."]
    # Neither failed turn is remembered.
    assert [item["content"] for item in model.seen[2]] == ["three"]


async def test_groups_need_a_mention_or_a_reply_to_the_bot(tmp_path: Path) -> None:
    model = FakeModel(answer("Yes."), answer("Also yes."))
    bot, telegram, _ = make_bot(tmp_path, model)

    await bot.handle(message("Anyone seen Dune?", chat_type="supergroup"))
    await bot.handle(message("@CRBL_media_bot do we have Dune?", chat_type="supergroup"))
    reply = {"message_id": 3, "from": {"id": 999, "is_bot": True}}
    await bot.handle(message("and Arrival?", chat_type="group", reply_to_message=reply))

    assert len(model.seen) == 2
    assert model.seen[0][-1]["content"] == "do we have Dune?"
    assert [item["reply_to"] for item in telegram.messages] == [7, 7]
    assert {item["chat_id"] for item in telegram.messages} == {-100}


async def test_non_text_messages_get_a_short_answer_in_private_only(tmp_path: Path) -> None:
    model = FakeModel()
    bot, telegram, _ = make_bot(tmp_path, model)

    await bot.handle(message(None, photo=[{}]))
    await bot.handle(message(None, chat_type="group", photo=[{}]))

    assert [item["text"] for item in telegram.messages] == [
        "I can only read text messages and photos."
    ]
    assert model.seen == []


async def test_unrenderable_markup_falls_back_to_plain_text(tmp_path: Path) -> None:
    model = FakeModel(answer("**half bold"))
    bot, telegram, _ = make_bot(tmp_path, model)
    telegram.reject_html = True

    await bot.handle(message("hi"))

    assert telegram.messages == [
        {"chat_id": 42, "text": "**half bold", "html": False, "reply_to": None}
    ]


async def test_model_is_told_the_date_and_oversized_results_are_cut(tmp_path: Path) -> None:
    model = FakeModel(tool_call("request_movie", {"tmdb_id": 1}), answer("Done."))
    bot, _, gateway = make_bot(tmp_path, model)
    gateway.results["request_movie"] = {"blob": "x" * (TOOL_RESULT_CHARS + 100)}

    await bot.handle(message("Add it"))

    assert f"Today is {datetime.date.today().isoformat()}." in model.systems[0]
    content = model.seen[1][-1]["content"][0]["content"]
    assert len(content) < TOOL_RESULT_CHARS + 100
    assert content.endswith("[result truncated; ask for less]")


async def test_gateway_timeout_becomes_a_gateway_error(
    tmp_path: Path,
) -> None:
    class DeadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

    token = tmp_path / "gateway.key"
    token.write_text("k" * 40)
    real_client = httpx.AsyncClient

    def dead_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=DeadTransport(), **kwargs)

    actor = Actor(user_id=42, chat_id=42)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx, "AsyncClient", dead_client)
        with pytest.raises(GatewayError, match="did not confirm"):
            await GatewayClient("http://gateway", token).call(actor, "request_movie", {})


async def test_every_part_of_a_long_group_reply_stays_on_the_message(tmp_path: Path) -> None:
    model = FakeModel(answer(("word " * 200 + "\n\n") * 10))
    bot, telegram, _ = make_bot(tmp_path, model)

    await bot.handle(message("@crbl_media_bot tell me everything", chat_type="group"))

    assert len(telegram.messages) > 1
    assert {item["reply_to"] for item in telegram.messages} == {7}


async def test_one_persons_messages_are_answered_in_order(tmp_path: Path) -> None:
    delays = [0.05, 0.0]

    class SlowFirstGateway(FakeGateway):
        async def observe(self, actor: Actor) -> Role:
            await asyncio.sleep(delays.pop(0))
            return self.role

    model = FakeModel(answer("First."), answer("Second."))
    bot, _, _ = make_bot(tmp_path, model, SlowFirstGateway())

    await asyncio.gather(bot.handle(message("find Dune")), bot.handle(message("the 2021 one")))

    assert [item["content"] for item in model.seen[1]] == [
        "find Dune",
        "First.",
        "the 2021 one",
    ]


async def test_gateway_outage_is_explained_in_private_only(tmp_path: Path) -> None:
    class DownGateway(FakeGateway):
        async def observe(self, actor: Actor) -> Role:
            raise GatewayError("down")

    model = FakeModel()
    bot, telegram, _ = make_bot(tmp_path, model, DownGateway())

    await bot.handle(message("hi"))
    await bot.handle(message("@crbl_media_bot hi", chat_type="group"))

    assert [item["text"] for item in telegram.messages] == [MEDIA_DOWN]
    assert model.seen == []


async def test_mention_must_be_the_whole_username(tmp_path: Path) -> None:
    model = FakeModel()
    bot, telegram, _ = make_bot(tmp_path, model)

    await bot.handle(message("@crbl_media_bot_2 hi", chat_type="group"))
    assert telegram.messages == []

    await bot.handle(message("@crbl_media_bot", chat_type="group"))
    assert [item["text"] for item in telegram.messages] == [GREETING]


async def test_empty_model_reply_is_reported_as_a_model_failure(tmp_path: Path) -> None:
    model = FakeModel(answer("   "))
    bot, telegram, _ = make_bot(tmp_path, model)

    await bot.handle(message("hi"))

    assert telegram.messages[0]["text"] == MODEL_DOWN


def test_markdown_subset_renders_as_escaped_telegram_html() -> None:
    rendered = to_html(
        "## Tonight\n**Dune** & `x<y` [site](https://example.com/a) "
        "[bad](javascript:alert(1)) <script>"
    )
    assert rendered == (
        "<b>Tonight</b>\n<b>Dune</b> &amp; <code>x&lt;y</code> "
        '<a href="https://example.com/a">site</a> [bad](javascript:alert(1)) &lt;script&gt;'
    )


def test_long_replies_split_on_paragraphs() -> None:
    text = ("a" * 30 + "\n\n") * 10
    parts = split_message(text, limit=100)
    assert [part.count("a") for part in parts] == [90, 90, 90, 30]
    assert "\n\n".join(parts) == text.strip()


def test_bots_and_malformed_senders_are_not_actors() -> None:
    assert actor_from_message(message("hi")) == Actor(user_id=42, chat_id=42, first_name="Ada")
    assert actor_from_message({**message("hi"), "from": {"id": 5, "is_bot": True}}) is None
    assert actor_from_message({**message("hi"), "from": {"id": "5"}}) is None
    assert actor_from_message({"text": "hi", "chat": {"id": 1}}) is None


def test_settings_require_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_BOT_MODEL", raising=False)
    with pytest.raises(ValueError, match="MEDIA_BOT_MODEL"):
        Settings.from_env()
    monkeypatch.setenv("MEDIA_BOT_MODEL", "claude-example")
    assert Settings.from_env().model == "claude-example"


def test_managed_projection_writes_one_private_secrets_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDIA_BOT_MODEL", "claude-example")
    source = {
        "TELEGRAM_BOT_TOKEN": "123:abc$#=",
        "ANTHROPIC_API_KEY": 'sk-ant-"quoted"',
        "CRBL_MEDIA_GATEWAY_URL": "http://media-gateway:18082",
        "OP_SERVICE_ACCOUNT_TOKEN": "bootstrap",
        "MEDIA_BOT_MODEL": "claude-example",
    }
    secrets = tmp_path / "bot.env"

    environment = project_bot_files(source, secrets)

    assert environment == {
        "CRBL_MEDIA_GATEWAY_URL": "http://media-gateway:18082",
        "MEDIA_BOT_MODEL": "claude-example",
        "MEDIA_BOT_SECRETS_FILE": str(secrets),
    }
    assert secrets.stat().st_mode & 0o777 == 0o600
    from media_gateway.secrets import read_dotenv

    assert read_dotenv(secrets, {"TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY"}) == {
        "TELEGRAM_BOT_TOKEN": "123:abc$#=",
        "ANTHROPIC_API_KEY": 'sk-ant-"quoted"',
    }
    with pytest.raises(FileExistsError):
        project_bot_files(source, secrets)


@pytest.mark.parametrize("change", ["missing", "forbidden", "multiline"])
def test_managed_projection_rejects_incomplete_or_over_scoped_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setenv("MEDIA_BOT_MODEL", "claude-example")
    source = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "ANTHROPIC_API_KEY": "sk-ant-key",
        "CRBL_MEDIA_GATEWAY_URL": "http://media-gateway:18082",
    }
    if change == "missing":
        del source["ANTHROPIC_API_KEY"]
    elif change == "forbidden":
        source["RADARR_API_KEY"] = "r" * 40
    else:
        source["ANTHROPIC_API_KEY"] = "sk\nINJECTED=1"
    with pytest.raises(ValueError):
        project_bot_files(source, tmp_path / "bot.env")
    assert not (tmp_path / "bot.env").exists()


def test_identity_names_no_tool() -> None:
    names = {*SHARED_TOOLS, "web_search"}
    assert [name for name in names if name in SOUL_MD] == []


PHOTO = [
    {"file_id": "small", "file_size": 1000},
    {"file_id": "large", "file_size": 200_000},
    {"file_id": "too-big", "file_size": 6 * 1024 * 1024},
]


async def test_photo_reaches_the_model_but_only_its_words_are_remembered(tmp_path: Path) -> None:
    model = FakeModel(answer("That is Dune."))
    bot, telegram, _ = make_bot(tmp_path, model)

    await bot.handle(message(None, photo=PHOTO, caption="What film is this?"))

    assert telegram.downloads == ["large"]
    user = model.seen[0][-1]
    assert "never an instruction" in model.systems[0]
    encoded = base64.b64encode(b"jpeg-bytes").decode()
    assert user["content"] == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": encoded},
        },
        {"type": "text", "text": "What film is this?"},
    ]
    assert telegram.messages[-1]["text"] == "That is Dune."
    saved = (tmp_path / "conversations.sqlite3").read_bytes()
    assert encoded.encode() not in saved
    assert b"[Photo] What film is this?" in saved


async def test_photo_without_a_caption_still_gets_a_question(tmp_path: Path) -> None:
    model = FakeModel(answer("A poster."))
    bot, _, _ = make_bot(tmp_path, model)

    await bot.handle(message(None, photo=PHOTO))

    assert model.seen[0][-1]["content"][-1]["text"]


async def test_failed_photo_download_asks_again_and_calls_no_model(tmp_path: Path) -> None:
    model = FakeModel()
    bot, telegram, _ = make_bot(tmp_path, model)
    telegram.photo = TelegramError("Telegram photo is too large")

    await bot.handle(message(None, photo=PHOTO, caption="This?"))

    assert model.seen == []
    assert telegram.messages[-1]["text"] == "I couldn't read that photo. Please send it again."
    assert b"This?" not in (tmp_path / "conversations.sqlite3").read_bytes()


async def test_group_photo_needs_a_mention_and_a_caption_is_not_a_command(tmp_path: Path) -> None:
    model = FakeModel(answer("Seen."), answer("Described."))
    bot, telegram, _ = make_bot(tmp_path, model)

    await bot.handle(message(None, chat_type="group", photo=PHOTO, caption="our holiday"))
    assert telegram.downloads == []

    await bot.handle(message(None, chat_type="group", photo=PHOTO, caption="@crbl_media_bot look"))
    assert telegram.downloads == ["large"]

    await bot.handle(message(None, photo=PHOTO, caption="/new"))
    assert telegram.messages[-1]["text"] == "Described."


async def test_album_is_answered_once(tmp_path: Path) -> None:
    model = FakeModel(answer("An album."))
    bot, telegram, _ = make_bot(tmp_path, model)

    for _ in range(3):
        await bot.handle(message(None, photo=PHOTO, media_group_id="album-1"))

    assert len(telegram.downloads) == 1
    assert len(model.seen) == 1


async def test_conversation_survives_a_restart_until_it_expires(tmp_path: Path) -> None:
    model = FakeModel(answer("Noted."), answer("Dune, yes."), answer("Which?"))
    bot, _, gateway = make_bot(tmp_path, model)
    await bot.handle(message("I like Dune"))

    restarted, _, _ = make_bot(tmp_path, model, gateway)
    await restarted.handle(message("What did I like?"))
    assert {"role": "user", "content": "I like Dune"} in model.seen[1]

    later = time.time() + 7201
    with patch("media_bot.bot.time.time", return_value=later):
        expired, _, _ = make_bot(tmp_path, model, gateway)
        await expired.handle(message("And now?"))
    assert {"role": "user", "content": "I like Dune"} not in model.seen[2]
    assert b"I like Dune" not in (tmp_path / "conversations.sqlite3").read_bytes()


async def test_reset_and_blocking_remove_the_saved_conversation(tmp_path: Path) -> None:
    model = FakeModel(answer("Noted."), answer("Noted again."))
    bot, _, gateway = make_bot(tmp_path, model)
    store = bot.store
    key = (message()["chat"]["id"], message()["from"]["id"])

    await bot.handle(message("first"))
    assert store.load(key, time.time())[0]
    await bot.handle(message("/new"))
    assert store.load(key, time.time())[0] == []
    assert b"first" not in (tmp_path / "conversations.sqlite3").read_bytes()

    await bot.handle(message("second"))
    assert store.load(key, time.time())[0]
    gateway.role = Role.BLOCKED
    await bot.handle(message("third"))
    assert store.load(key, time.time())[0] == []


def test_saved_conversations_are_private_to_the_bot_user(tmp_path: Path) -> None:
    path = tmp_path / "conversations.sqlite3"
    ConversationStore(path, idle_seconds=60)

    assert path.stat().st_mode & 0o777 == 0o600


async def test_photo_download_stops_at_the_limit_and_never_exposes_the_token() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/1.jpg"}})
        assert request.url.path == "/file/botSECRET/photos/1.jpg"
        return httpx.Response(200, content=b"x" * (PHOTO_MAX_BYTES + 1))

    telegram = Telegram("SECRET")
    await telegram._http.aclose()
    telegram._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with pytest.raises(TelegramError) as caught:
        await telegram.download_photo("id")
    assert "too large" in str(caught.value)
    assert "SECRET" not in str(caught.value)
    await telegram.close()


async def test_photo_download_failures_do_not_expose_the_token() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/1.jpg"}})
        raise httpx.ConnectError("boom", request=request)

    telegram = Telegram("SECRET")
    await telegram._http.aclose()
    telegram._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with pytest.raises(TelegramError) as caught:
        await telegram.download_photo("id")
    assert "SECRET" not in str(caught.value)
    await telegram.close()


async def scripted_model(
    *replies: httpx.Response | Exception,
) -> tuple[Model, list[httpx.Request]]:
    """A model whose HTTP calls are answered from a script, and the requests it sent."""

    pending = list(replies)
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        reply = pending.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    model = Model("SECRET", "claude-example")
    await model._http.aclose()
    model._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return model, sent


def api_error(status: int, kind: str, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"type": "error", "error": {"type": kind}}, headers=headers)


OK = {"content": answer("Hi."), "stop_reason": "end_turn"}
MESSAGES = [{"role": "user", "content": "hello"}]


async def test_model_request_marks_the_cache_points_and_reports_failures() -> None:
    model, sent = await scripted_model(
        httpx.Response(200, json=OK),
        httpx.Response(200, json={"content": answer("Cut"), "stop_reason": "max_tokens"}),
        httpx.Response(200, json={"content": [], "stop_reason": "refusal"}),
        api_error(400, "invalid_request_error"),
    )

    assert await model.complete("Be brief.", MESSAGES, [search.SCHEMA]) == answer("Hi.")
    body = json.loads(sent[0].content)
    assert str(sent[0].url) == "https://api.anthropic.com/v1/messages"
    assert sent[0].headers["x-api-key"] == "SECRET"
    assert sent[0].headers["anthropic-version"] == "2026-10-21"
    assert body == {
        "model": "claude-example",
        "max_tokens": 8192,
        "tools": [search.SCHEMA],
        "system": [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}],
        "messages": MESSAGES,
        "cache_control": {"type": "ephemeral"},
    }
    with pytest.raises(ModelCutOff, match="token limit"):
        await model.complete("Be brief.", MESSAGES, [search.SCHEMA])
    with pytest.raises(ModelRefused):
        await model.complete("Be brief.", MESSAGES, [search.SCHEMA])
    # A request the API rejects is not sent again.
    with pytest.raises(ModelError, match=r"HTTP 400 \(invalid_request_error\)") as caught:
        await model.complete("Be brief.", MESSAGES, [search.SCHEMA])
    assert not isinstance(caught.value, ModelBusy)
    assert "SECRET" not in str(caught.value)
    assert len(sent) == 4
    await model.close()


async def test_model_request_is_tried_once_more_after_a_passing_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_module, "RETRY_DELAY_SECONDS", 0)
    model, sent = await scripted_model(
        api_error(529, "overloaded_error"),
        httpx.Response(200, json=OK),
        httpx.ConnectError("refused"),
        httpx.Response(200, json=OK),
        api_error(429, "rate_limit_error", **{"retry-after": "0"}),
        api_error(500, "api_error"),
        httpx.ReadTimeout("slow"),
    )

    assert await model.complete("Be brief.", MESSAGES, []) == answer("Hi.")
    assert await model.complete("Be brief.", MESSAGES, []) == answer("Hi.")
    assert len(sent) == 4
    # The second failure is final: one more try, not a loop.
    with pytest.raises(ModelError, match=r"HTTP 500 \(api_error\)"):
        await model.complete("Be brief.", MESSAGES, [])
    assert len(sent) == 6
    # A timeout already waited its full time, so it is not tried again.
    with pytest.raises(ModelError, match="ReadTimeout"):
        await model.complete("Be brief.", MESSAGES, [])
    assert len(sent) == 7
    await model.close()


def test_retry_waits_as_the_api_asks_and_gives_up_when_a_retry_cannot_help() -> None:
    delay = model_module._retry_delay

    assert delay(api_error(529, "overloaded_error"), {}) == 2
    assert delay(api_error(429, "rate_limit_error", **{"retry-after": "3"}), {}) == 3
    assert delay(api_error(429, "rate_limit_error", **{"retry-after": "soon"}), {}) == 2
    # Too long to hold a chat for.
    assert delay(api_error(429, "rate_limit_error", **{"retry-after": "60"}), {}) is None
    # The monthly limit is spent: the next try fails the same way.
    spent = {"details": {"error_code": "enforced_spend_limit_reached"}}
    assert delay(api_error(429, "rate_limit_error"), spent) is None
    assert delay(api_error(400, "invalid_request_error"), {}) is None
    assert delay(api_error(401, "authentication_error"), {}) is None
    # The API's own advice wins in both directions.
    assert delay(api_error(500, "api_error", **{"x-should-retry": "false"}), {}) is None
    assert delay(api_error(400, "invalid_request_error", **{"x-should-retry": "true"}), {}) == 2


def test_egress_opens_only_named_hosts_on_port_443() -> None:
    assert egress.target(b"CONNECT api.telegram.org:443 HTTP/1.1\r\n\r\n") == "api.telegram.org"
    assert egress.target(b"CONNECT API.Anthropic.com:443 HTTP/1.1\r\n\r\n") == "api.anthropic.com"
    for refused in (
        b"CONNECT example.com:443 HTTP/1.1\r\n\r\n",
        b"CONNECT api.telegram.org:80 HTTP/1.1\r\n\r\n",
        b"CONNECT api.telegram.org.example.com:443 HTTP/1.1\r\n\r\n",
        b"CONNECT 192.168.1.1:443 HTTP/1.1\r\n\r\n",
        b"CONNECT media-gateway:443 HTTP/1.1\r\n\r\n",
        b"GET http://api.telegram.org:443/ HTTP/1.1\r\n\r\n",
        b"CONNECT api.telegram.org:443\r\n\r\n",
        b"\xff\xfe\r\n\r\n",
    ):
        assert egress.target(refused) is None


def test_egress_never_connects_to_an_inside_address() -> None:
    inside = ["192.168.1.10", "10.0.0.5", "172.30.40.2", "127.0.0.1", "169.254.1.1", "::1"]
    assert egress.public_addresses([*inside, "fd00::1", "149.154.167.220"]) == ["149.154.167.220"]


def test_web_search_names_only_engines_the_egress_proxy_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: dict[str, Any] = {}

    class Engines:
        def __init__(self, **_options: Any) -> None:
            pass

        def text(self, _query: str, **options: Any) -> list[dict[str, str]]:
            asked.update(options)
            return []

    monkeypatch.setattr(search, "DDGS", Engines)
    search._search("dune")
    assert {egress.SEARCH_ENGINES[name] for name in asked["backend"].split(",")} <= (
        egress.ALLOWED_HOSTS
    )
