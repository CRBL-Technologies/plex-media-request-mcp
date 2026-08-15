from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import types
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_media_extension.companion_client import (
    PRIVATE_CONFIRM_BIND_ENDPOINT,
    PRIVATE_CONFIRM_CALLBACK_ENDPOINT,
    CompanionClient,
    CompanionToolDenied,
    ConfirmationEnvelope,
    _decode_result,
    policy_membership_scope,
)
from hermes_media_extension.confirmation_callback import ConfirmationCallbackHandler
from hermes_media_extension.plugin import (
    TOOL_INVENTORY,
    MediaPolicyTelegramAdapter,
    ADMIN_TOOLSET,
    SHARED_TOOLSET,
    _install_native_tool_visibility_patch,
    _build_adapter,
    load_frozen_tool_metadata,
    load_frozen_tool_schemas,
    register,
)
from hermes_media_extension.policy_helper_api import PolicyMembership
from hermes_media_extension.policy_helper_api import (
    NotificationDelivery,
    PolicyHelperAPI,
    PolicyHelperServer,
    _policy_admin_ids,
    _policy_selected_values,
    _telegram_bot_from_policy,
    probe_policy_helper,
)
from hermes_media_extension.startup_contract import (
    EXPECTED_NATIVE_ADAPTER_SHA256,
    EXPECTED_PLATFORM_REGISTRY_SHA256,
    ContractInputs,
    check_startup_contract,
)
from hermes_media_extension.trusted_context import (
    TrustedContextError,
    TrustedTelegramContext,
    current_trusted_context,
    trusted_context_from_event,
    trusted_context_scope,
)

from media_companion.auth import ActorAssertionSigner, confirmation_callback_data
from media_companion.clients.telegram import TelegramError, TelegramErrorClass
from media_companion.production import HermesTelegramBridge


def _extracted_hermes_source() -> Path | None:
    configured = os.getenv("HERMES_SOURCE_DIR", "").strip()
    if configured and (candidate := Path(configured)).is_dir():
        return candidate
    candidates = sorted(Path("/tmp").glob("tmp.*/hermes-agent-2026.8.3"))
    return candidates[0] if candidates else None


EXTRACTED_HERMES_SOURCE = _extracted_hermes_source()


def _message(
    *, user_id: int = 42, chat_id: int = 42, message_id: int = 8
) -> SimpleNamespace:
    user = SimpleNamespace(id=user_id, first_name="Ada", username="ada")
    chat = SimpleNamespace(id=chat_id, type="private")
    return SimpleNamespace(
        from_user=user,
        chat=chat,
        message_id=message_id,
        message_thread_id=None,
        text="hello",
    )


def _update(
    *, update_id: int = 7, callback: str | None = None, message_id: int = 8
) -> SimpleNamespace:
    message = _message(message_id=message_id)
    if callback is None:
        return SimpleNamespace(
            update_id=update_id, message=message, callback_query=None
        )
    query = SimpleNamespace(
        id="query-1",
        data=callback,
        from_user=message.from_user,
        message=message,
    )
    return SimpleNamespace(update_id=update_id, message=None, callback_query=query)


def test_trusted_context_requires_native_raw_message_and_contextvar_is_scoped() -> None:
    update = _update()
    context = TrustedTelegramContext.from_update(update)
    assert (context.user_id, context.chat_id, context.update_id) == (42, 42, 7)
    event = SimpleNamespace(
        raw_message=update.message,
        source=SimpleNamespace(user_id="42", chat_id="42"),
        platform_update_id=7,
        message_id="8",
    )
    assert trusted_context_from_event(event) == context
    assert current_trusted_context() is None
    with trusted_context_scope(context):
        assert current_trusted_context() == context
    assert current_trusted_context() is None
    with pytest.raises(TrustedContextError):
        trusted_context_from_event(
            SimpleNamespace(source=event.source, platform_update_id=7)
        )


def test_companion_wrapper_binds_native_actor_header_and_denies_unknown_tools() -> None:
    class Helper:
        def authorize(self, **_: object) -> PolicyMembership:
            return PolicyMembership(
                user_id=42,
                chat_id=42,
                allowed=True,
                role="user",
                fingerprint="f" * 64,
                version="v1",
            )

    class Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str], bytes]] = []

        def request(self, method: str, url: str, **kwargs: object) -> object:
            self.calls.append((method, kwargs["headers"], kwargs["data"]))  # type: ignore[arg-type]
            return {"result": {"content": [{"type": "text", "text": "ok"}]}}

    transport = Transport()
    client = CompanionClient(
        "http://companion",
        signer=ActorAssertionSigner(b"actor-key", kid="current"),
        policy_helper=Helper(),
        transport=transport,
    )
    context = TrustedTelegramContext.from_update(_update())
    with trusted_context_scope(context):
        result = client.call_tool("search_media", {"query": "Matrix"})
    assert result.text == "ok"
    assert transport.calls[0][1]["X-CRBL-Actor"]
    assert json.loads(transport.calls[0][2])["params"]["name"] == "search_media"
    with pytest.raises(CompanionToolDenied):
        client.call_tool("not-a-reviewed-tool", {})


def test_plugin_registers_last_writer_platform_and_exact_frozen_tools() -> None:
    class Context:
        def __init__(self) -> None:
            self.platform: dict[str, object] | None = None
            self.tools: list[str] = []

        def register_platform(self, **kwargs: object) -> None:
            self.platform = kwargs

        def register_tool(self, **kwargs: object) -> None:
            self.tools.append(str(kwargs["name"]))

    ctx = Context()
    register(ctx)
    assert ctx.platform is not None
    assert ctx.platform["name"] == "telegram"
    assert ctx.platform["plugin_name"] == "media-policy"
    assert ctx.platform["adapter_factory"] is _build_adapter
    assert tuple(ctx.tools) == TOOL_INVENTORY
    assert MediaPolicyTelegramAdapter.media_policy_override is True


def test_registered_tools_use_frozen_closed_schemas_and_metadata() -> None:
    registrations: list[dict[str, object]] = []

    class Context:
        def register_platform(self, **_: object) -> None:
            return None

        def register_tool(self, **kwargs: object) -> None:
            registrations.append(kwargs)

    register(Context())
    schemas = load_frozen_tool_schemas()
    metadata = load_frozen_tool_metadata()
    assert set(schemas) == set(TOOL_INVENTORY)
    assert set(metadata) == set(TOOL_INVENTORY)
    assert len(registrations) == len(TOOL_INVENTORY)
    for registration in registrations:
        name = registration["name"]
        assert isinstance(name, str)
        schema = registration["schema"]
        assert schema == schemas[name]
        assert schema["type"] == "object"  # type: ignore[index]
        assert schema["additionalProperties"] is False  # type: ignore[index]
        assert registration["description"] == metadata[name]["description"]


def test_registered_tool_handler_accepts_hermes_runtime_context(monkeypatch) -> None:
    registrations: list[dict[str, object]] = []
    calls: list[tuple[str, dict[str, object]]] = []

    class Context:
        def register_platform(self, **_: object) -> None:
            return None

        def register_tool(self, **kwargs: object) -> None:
            registrations.append(kwargs)

    class Client:
        async def call_tool_async(
            self, tool: str, arguments: Mapping[str, object]
        ) -> object:
            calls.append((tool, dict(arguments)))
            return SimpleNamespace(
                confirmation=None,
                to_dict=lambda: {"ok": True},
            )

    monkeypatch.setattr(
        "hermes_media_extension.plugin._runtime_for",
        lambda *_args, **_kwargs: SimpleNamespace(client=Client()),
    )
    register(Context())
    registration = next(
        item for item in registrations if item["name"] == "search_media"
    )
    handler = registration["handler"]
    assert callable(handler)
    result = asyncio.run(
        handler(
            {"query": "Matrix"},
            task_id="task-1",
            session_id="session-1",
            future_dispatch_metadata="ignored",
        )
    )
    assert result == {"ok": True}
    assert calls == [("search_media", {"query": "Matrix"})]


def test_crbl_callback_is_consumed_without_delegating_to_native_catch_all() -> None:
    token = "A" * 43
    seen: list[dict[str, object]] = []

    class Client:
        def callback(self, **kwargs: object) -> object:
            seen.append(kwargs)
            return SimpleNamespace(text="done")

    class Query:
        def __init__(self) -> None:
            self.id = "query-1"
            self.data = confirmation_callback_data(token)
            self.from_user = _message().from_user
            self.message = _message()
            self.answers: list[str] = []

        async def answer(self, text: str = "", **_: object) -> None:
            self.answers.append(text)

        async def edit_message_reply_markup(self, **_: object) -> None:
            return None

    query = Query()
    update = SimpleNamespace(update_id=7, message=None, callback_query=query)
    handler = ConfirmationCallbackHandler(Client())
    outcome = asyncio.run(handler.handle_update(update))
    assert outcome.accepted is True
    assert seen[0]["token"] == token
    assert query.answers == ["done"]


def test_confirmation_envelope_is_typed_and_redacted_from_model_result() -> None:
    token = "A" * 43
    result = _decode_result(
        "radarr_add_movie",
        {
            "result": {
                "content": [{"type": "text", "text": "mutation pending"}],
                "confirmation": {
                    "token": token,
                    "preview": "<b>Approve exact server preview</b>",
                    "parse_mode": "HTML",
                    "expires_at": 100,
                },
            }
        },
    )
    assert result.confirmation == ConfirmationEnvelope(
        token=token,
        preview="<b>Approve exact server preview</b>",
        expires_at=100,
    )
    safe = result.to_dict()
    assert token not in json.dumps(safe)
    assert "Approve exact server preview" not in json.dumps(safe)
    assert safe["structuredContent"] == {"confirmation_required": True}


def test_private_confirmation_routes_are_distinct_from_model_mcp_route() -> None:
    class Helper:
        def authorize(self, **_: object) -> PolicyMembership:
            return PolicyMembership(
                user_id=42,
                chat_id=42,
                allowed=True,
                role="admin",
                fingerprint="f" * 64,
                version="v1",
            )

    class Transport:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def request(self, method: str, url: str, **_: object) -> object:
            self.urls.append(url)
            return {"result": {"content": [{"type": "text", "text": "ok"}]}}

    transport = Transport()
    client = CompanionClient(
        "http://companion",
        signer=ActorAssertionSigner(b"actor-key", kid="current"),
        policy_helper=Helper(),
        transport=transport,
    )
    context = TrustedTelegramContext.from_update(_update(message_id=9))
    token = "A" * 43
    with trusted_context_scope(context):
        client.bind_confirmation(
            token=token, preview="Preview", chat_id=42, message_id=9
        )
        client.callback(
            token=token,
            callback_query_id="query-1",
            chat_id=42,
            message_id=9,
        )
    assert transport.urls == [
        "http://companion" + PRIVATE_CONFIRM_BIND_ENDPOINT,
        "http://companion" + PRIVATE_CONFIRM_CALLBACK_ENDPOINT,
    ]
    assert all(not url.endswith("/mcp") for url in transport.urls)


def test_native_confirmation_delivery_sends_exact_preview_then_binds_message() -> None:
    preview = "<b>Exact server preview — do not re-render</b>"
    token = "A" * 43
    sent: list[dict[str, object]] = []
    bound: list[dict[str, object]] = []

    class Bot:
        async def send_message(self, **kwargs: object) -> object:
            sent.append(kwargs)
            return SimpleNamespace(message_id=99)

        async def delete_message(self, **_: object) -> None:
            return None

    class Client:
        def bind_confirmation(self, **kwargs: object) -> object:
            bound.append(kwargs)
            return SimpleNamespace(text="bound")

    adapter = MediaPolicyTelegramAdapter(
        SimpleNamespace(token="configured"),
        companion=Client(),  # type: ignore[arg-type]
        callback_handler=ConfirmationCallbackHandler(Client()),
    )
    adapter._bot = Bot()  # type: ignore[attr-defined]
    context = TrustedTelegramContext.from_update(_update())
    with trusted_context_scope(context):
        message_id = asyncio.run(
            adapter.deliver_confirmation_preview(
                ConfirmationEnvelope(token=token, preview=preview)
            )
        )
    assert message_id == 99
    assert sent[0]["text"] == preview
    markup = sent[0]["reply_markup"]
    if isinstance(markup, Mapping):
        callback = markup["inline_keyboard"][0][0]["callback_data"]  # type: ignore[index]
    else:
        callback = markup.inline_keyboard[0][0].callback_data  # type: ignore[attr-defined]
    assert callback == "crblc:" + token
    assert bound == [
        {
            "token": token,
            "preview": preview,
            "chat_id": 42,
            "message_id": 99,
        }
    ]


def test_role_aware_toolset_patch_seals_regular_and_admin_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes = types.ModuleType("hermes_cli")
    hermes.__path__ = []  # type: ignore[attr-defined]
    tools_config = types.ModuleType("hermes_cli.tools_config")

    def original(config: dict[str, object], platform: str) -> set[str]:
        return {"hermes-telegram"} if platform == "telegram" else {"other"}

    tools_config._get_platform_tools = original  # type: ignore[attr-defined]
    hermes.tools_config = tools_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes)
    monkeypatch.setitem(sys.modules, "hermes_cli.tools_config", tools_config)
    assert _install_native_tool_visibility_patch() is True
    wrapped = tools_config._get_platform_tools  # type: ignore[attr-defined]
    regular = PolicyMembership(
        user_id=42,
        chat_id=42,
        allowed=True,
        role="user",
        fingerprint="f" * 64,
    )
    admin = PolicyMembership(
        user_id=42,
        chat_id=42,
        allowed=True,
        role="admin",
        fingerprint="f" * 64,
    )
    with policy_membership_scope(regular):
        assert wrapped({}, "telegram") == {SHARED_TOOLSET}
    with policy_membership_scope(admin):
        assert wrapped({}, "telegram") == {SHARED_TOOLSET, ADMIN_TOOLSET}


@pytest.mark.skipif(
    EXTRACTED_HERMES_SOURCE is None,
    reason="pinned Hermes source is supplied by the deployment integration job",
)
def test_pinned_extracted_hermes_tool_assembly_is_role_aware() -> None:
    assert EXTRACTED_HERMES_SOURCE is not None
    script = """
from hermes_cli import tools_config
from toolsets import resolve_toolset
from hermes_media_extension.companion_client import policy_membership_scope
from hermes_media_extension.companion_client import ADMIN_TOOLS, SHARED_TOOLS
from hermes_media_extension.plugin import ADMIN_TOOLSET, SHARED_TOOLSET, register
from hermes_media_extension.policy_helper_api import PolicyMembership
from tools.registry import registry

class Context:
    def register_platform(self, **kwargs):
        self.platform = kwargs
    def register_tool(self, **kwargs):
        registry.register(**kwargs)

context = Context()
register(context)
resolver = tools_config._get_platform_tools
regular = PolicyMembership(user_id=42, chat_id=42, allowed=True, role="user", fingerprint="f" * 64)
admin = PolicyMembership(user_id=42, chat_id=42, allowed=True, role="admin", fingerprint="f" * 64)
with policy_membership_scope(regular):
    regular_tools = resolver({}, "telegram")
with policy_membership_scope(admin):
    admin_tools = resolver({}, "telegram")
assert regular_tools == {SHARED_TOOLSET}
assert admin_tools == {SHARED_TOOLSET, ADMIN_TOOLSET}
assert admin_tools - regular_tools == {ADMIN_TOOLSET}
def assembled(toolsets):
    tools = set()
    for name in toolsets:
        tools.update(resolve_toolset(name))
    return tools
assert assembled(regular_tools) == set(SHARED_TOOLS)
assert assembled(admin_tools) == set(SHARED_TOOLS + ADMIN_TOOLS)
"""
    environment = os.environ.copy()
    source = str(EXTRACTED_HERMES_SOURCE)
    repository = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(
        (source, str(Path(repository) / "src"), repository)
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(
    EXTRACTED_HERMES_SOURCE is None,
    reason="pinned Hermes source is supplied by the deployment integration job",
)
def test_pinned_extracted_hermes_loads_media_policy_on_telegram_lookup() -> None:
    """The CRBL loader must win the real Hermes deferred Telegram collision."""

    assert EXTRACTED_HERMES_SOURCE is not None
    repository = str(Path(__file__).resolve().parents[1])
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(EXTRACTED_HERMES_SOURCE), str(Path(repository) / "src"), repository)
    )
    with tempfile.TemporaryDirectory() as hermes_home:
        environment["HERMES_HOME"] = hermes_home
        # Recreate the relevant part of the pinned image's plugin tree.  The
        # native ``telegram`` directory is intentionally present so this test
        # catches the deferred-loader ordering bug instead of testing the CRBL
        # shim in isolation.  The image installs the CRBL directory with a
        # lexically-last basename while keeping its manifest name
        # ``telegram-platform``.
        bundled_plugins = Path(hermes_home) / "bundled-plugins"
        (bundled_plugins / "platforms").mkdir(parents=True)
        shutil.copytree(
            Path(EXTRACTED_HERMES_SOURCE) / "plugins" / "platforms" / "telegram",
            bundled_plugins / "platforms" / "telegram",
        )
        shutil.copytree(
            Path(repository)
            / "deployment"
            / "hermes"
            / "plugins"
            / "platforms"
            / "media-policy",
            bundled_plugins / "platforms" / "zzzz-media-policy",
        )
        environment["HERMES_BUNDLED_PLUGINS"] = str(bundled_plugins)
        script = """
from gateway.platform_registry import platform_registry
from hermes_cli import tools_config
from hermes_cli.plugins import PluginManager
from tools.registry import registry

manager = PluginManager()
manager.discover_and_load()
assert "telegram" in platform_registry._deferred
entry = platform_registry.get("telegram")
assert entry is not None
assert entry.plugin_name == "media-policy"
assert entry.source == "plugin"
assert entry.adapter_factory.__module__ == "hermes_media_extension.plugin"
assert getattr(tools_config._get_platform_tools, "__media_policy_visibility__", False)
assert set(registry.get_tool_names_for_toolset("media-policy-shared")) == {
    "search_media", "request_movie", "request_series", "request_status",
    "download_status", "browse_library", "media_status",
}
assert len(registry.get_tool_names_for_toolset("media-policy-admin")) == 65
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
    assert completed.returncode == 0, completed.stderr


def test_narrow_policy_helper_server_authenticates_and_uses_whitelist_helpers(
    tmp_path: Path,
) -> None:
    policy = tmp_path / ".env"
    policy.write_text("TELEGRAM_ALLOWED_USERS=42,99\n", encoding="utf-8")
    log = tmp_path / "gateway.log"
    log.write_text(
        "gateway: Blocked unauthorized user 77 in chat 42\n", encoding="utf-8"
    )

    class Bot:
        def get_chat(self, chat_id: int) -> dict[str, object]:
            return {
                "id": chat_id,
                "type": "private",
                "username": "ada",
                "full_name": "Ada Lovelace",
            }

        def send_message(
            self, chat_id: int, text: str, *, parse_mode: str
        ) -> SimpleNamespace:
            assert chat_id == 42
            assert text == "admin alert"
            assert parse_mode == "HTML"
            return SimpleNamespace(message_id=123)

    recycles: list[str] = []
    recycle_done = threading.Event()

    def record_recycle() -> None:
        recycles.append("gateway-default")
        recycle_done.set()

    server = PolicyHelperServer(
        policy_path=policy,
        key=b"helper-key",
        log_paths=(log,),
        bot=Bot(),
        admin_ids=(42,),
        recycle_callback=record_recycle,
    )
    host, port = server.start()
    try:
        api = PolicyHelperAPI(f"http://{host}:{port}", key=b"helper-key")
        key_file = tmp_path / "helper.key"
        key_file.write_bytes(b"helper-key")
        os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
        assert (
            probe_policy_helper(host="127.0.0.1", port=port, key_file=key_file) is True
        )
        membership = api.membership(user_id=42, chat_id=42)
        assert membership.is_admin is True
        assert membership.fingerprint
        current = api.current_users()
        assert tuple(user.user_id for user in current.users) == (42, 99)
        assert tuple(user.role for user in current.users) == ("admin", "user")
        assert current.fingerprint == membership.fingerprint
        assert current.version
        assert PolicyHelperAPI.ROUTES["current_users"] == "/v1/policy/current-users"
        sent = api.notify_admin(chat_id=42, text="admin alert", parse_mode="HTML")
        assert sent.status == "sent"
        assert sent.message_id == 123
        with pytest.raises(Exception):
            api.notify_admin(chat_id=99, text="not an admin")
        blocked = api.blocked_contacts(limit=5)
        assert blocked[0].user_id == 77
        identity = api.resolve_identity(user_id=42)
        assert identity.display_name == "Ada Lovelace"
        updated = api.mutate_allowlist(
            operation="add",
            user_id=77,
            expected_fingerprint=membership.fingerprint,
        )
        assert updated.allowed is True
        assert recycle_done.wait(1.0)
        assert recycles == ["gateway-default"]
        status = api.runtime_status()
        assert status.ready is True
        wrong = PolicyHelperAPI(f"http://{host}:{port}", key=b"wrong-key")
        with pytest.raises(Exception):
            wrong.runtime_status()
    finally:
        server.stop()


def test_policy_helper_uses_canonical_dotenv_token_admins_and_selected_get_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / ".env"
    policy.write_text(
        "TELEGRAM_ALLOWED_USERS=42,99\n"
        "TELEGRAM_ADMIN_USERS=42\n"
        "TELEGRAM_BOT_TOKEN=canonical-token\n"
        "UNRELATED_SECRET=must-not-be-selected\n",
        encoding="utf-8",
    )
    os.chmod(policy, stat.S_IRUSR | stat.S_IWUSR)

    class Bot:
        def __init__(self, *, token: str) -> None:
            self.token = token

        def get_chat(self, chat_id: int) -> dict[str, object]:
            return {"id": chat_id, "type": "private", "username": "ada"}

    telegram_module = types.ModuleType("media_companion.clients.telegram")
    telegram_module.TelegramClient = Bot  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "media_companion.clients.telegram", telegram_module
    )

    selected = _policy_selected_values(policy)
    assert selected == {
        "TELEGRAM_ADMIN_USERS": "42",
        "TELEGRAM_BOT_TOKEN": "canonical-token",
    }
    assert selected["TELEGRAM_BOT_TOKEN"] == "canonical-token"
    assert "UNRELATED_SECRET" not in selected
    bot = _telegram_bot_from_policy(selected)
    assert isinstance(bot, Bot)
    assert bot.token == "canonical-token"

    server = PolicyHelperServer(
        policy_path=policy,
        key=b"helper-key",
        bot=bot,
        admin_ids=None,
    )
    host, port = server.start()
    try:
        api = PolicyHelperAPI(f"http://{host}:{port}", key=b"helper-key")
        assert api.current_users().users[0].role == "admin"
        assert api.resolve_identity(user_id=42).username == "ada"
    finally:
        server.stop()


def test_notification_route_has_typed_delivery_states_and_canonical_worker_name(
    tmp_path: Path,
) -> None:
    class RetryError(Exception):
        error_class = "retryable"
        pre_transmission = True
        transmitted = False
        retry_after = 9

    class RetryBot:
        def send_message(self, *_args: object, **_kwargs: object) -> object:
            raise RetryError()

    policy = tmp_path / ".env"
    policy.write_text("TELEGRAM_ALLOWED_USERS=42\n", encoding="utf-8")
    os.chmod(policy, stat.S_IRUSR | stat.S_IWUSR)
    server = PolicyHelperServer(
        policy_path=policy,
        key=b"helper-key",
        bot=RetryBot(),
        admin_ids=(42,),
    )
    try:
        retry = server.handle_request(
            "/v1/policy/notify-admin",
            {"chat_id": 42, "text": "retry", "parse_mode": ""},
        )
        assert retry == {
            "chat_id": 42,
            "status": "retryable-pretransmission",
            "transmitted": False,
            "retry_after": 9,
        }
    finally:
        server.stop()

    assert NotificationDelivery(
        chat_id=42,
        status="retryable-pretransmission",
        retry_after=9,
        transmitted=False,
    ).pre_transmission_retry

    class Helper:
        def send_notification(self, **_: object) -> NotificationDelivery:
            return NotificationDelivery(
                chat_id=42,
                status="retryable-pretransmission",
                retry_after=9,
                transmitted=False,
            )

    with pytest.raises(TelegramError) as raised:
        HermesTelegramBridge(Helper()).send_message(42, "retry")
    assert raised.value.error_class is TelegramErrorClass.RETRYABLE
    assert raised.value.pre_transmission is True


def test_policy_admin_selectors_must_have_equal_sets(tmp_path: Path) -> None:
    policy = tmp_path / ".env"
    policy.write_text(
        "TELEGRAM_ALLOWED_USERS=42,99\n"
        "TELEGRAM_ADMIN_USERS=42\n"
        "TELEGRAM_ADMIN_IDS=99\n",
        encoding="utf-8",
    )
    os.chmod(policy, stat.S_IRUSR | stat.S_IWUSR)
    with pytest.raises(ValueError, match="conflict"):
        _policy_admin_ids(policy)


def test_external_startup_contract_fails_closed_and_accepts_complete_fake_evidence() -> (
    None
):
    NativeTelegram = type(
        "TelegramAdapter", (), {"__module__": "plugins.platforms.telegram.adapter"}
    )
    native = types.SimpleNamespace(
        TelegramAdapter=NativeTelegram,
        __hermes_source_sha256__=EXPECTED_NATIVE_ADAPTER_SHA256,
    )
    registry_module = types.SimpleNamespace(
        __hermes_source_sha256__=EXPECTED_PLATFORM_REGISTRY_SHA256
    )
    entry = types.SimpleNamespace(
        plugin_name="media-policy",
        source="plugin",
        adapter_factory=_build_adapter,
    )
    registry = types.SimpleNamespace(
        get=lambda name: entry if name == "telegram" else None
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        plugin_dir = root / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(
            "name: telegram-platform\nkind: platform\n", encoding="utf-8"
        )
        (plugin_dir / "__init__.py").write_text(
            "from hermes_media_extension.plugin import register\n", encoding="utf-8"
        )
        key = root / "actor-signing.key"
        key.write_bytes(b"actor-key")
        os.chmod(key, stat.S_IRUSR | stat.S_IWUSR)
        report = check_startup_contract(
            ContractInputs(
                registry=registry,
                native_module=native,
                platform_registry_module=registry_module,
                hermes_module=types.SimpleNamespace(__release_date__="2026.8.3"),
                tool_names=TOOL_INVENTORY,
                env={
                    "TELEGRAM_BOT_TOKEN": "configured",
                    "CRBL_ACTOR_SIGNING_KEY_FILE": str(key),
                    "CRBL_POLICY_HELPER_KEY_FILE": str(key),
                    "CRBL_COMPANION_URL": "http://companion",
                    "CRBL_POLICY_HELPER_URL": "http://127.0.0.1:8787",
                    "CRBL_POLICY_HELPER_HOST": "0.0.0.0",
                    "CRBL_POLICY_HELPER_PORT": "8787",
                    "CRBL_POLICY_FILE": "/opt/data/.env",
                    "TOOL_PROFILE": "full",
                    "TOOL_INCLUDE": "",
                },
                plugin_dir=plugin_dir,
                visibility_patch=True,
            )
        )
        assert report.ok, report.errors
        blocked = check_startup_contract(
            ContractInputs(
                registry=registry,
                native_module=native,
                platform_registry_module=registry_module,
                hermes_module=types.SimpleNamespace(__release_date__="2026.8.3"),
                tool_names=TOOL_INVENTORY + ("future_tool",),
                env={"TELEGRAM_WEBHOOK_URL": "https://public.invalid/hook"},
                plugin_dir=plugin_dir,
            )
        )
        assert not blocked.ok
        assert "telegram_polling_only" in blocked.checks
        assert "tool_inventory" in blocked.checks
