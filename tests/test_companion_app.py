"""Adversarial contract tests for the closed companion ASGI boundary."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

import pytest

pytest.importorskip("starlette")

from media_companion.app import (  # noqa: E402
    CONFIRM_CALLBACK_PATH,
    CONFIRM_BIND_PATH,
    DASHBOARD_RATE_CHAT_ID,
    DASHBOARD_RATE_USER_ID,
    DASHBOARD_SERVICE_ACTOR,
    MCP_PATH,
    create_app,
)
from media_companion.auth import (  # noqa: E402
    ActorAssertionSigner,
    ActorAssertionVerifier,
    InMemoryConfirmationTokenStore,
    InMemoryNonceReplayStore,
)
from media_companion.operations import (  # noqa: E402
    CompanionRuntime,
    DurableStoreRequiredError,
)
from media_companion.tool_policy import (  # noqa: E402
    SHARED_TOOL_SET,
    UPSTREAM_TOOL_SET,
)
from hermes_media_extension.companion_client import _decode_result  # noqa: E402


ACTOR_KEY = b"a" * 32
HELPER_KEY = b"h" * 32
DASHBOARD_KEY = b"d" * 32


class _Policy:
    def membership(self, *, user_id: int, chat_id: int) -> Mapping[str, object]:
        return {
            "user_id": user_id,
            "chat_id": chat_id,
            "allowed": True,
            "role": "admin" if user_id == 99 else "user",
            "fingerprint": "policy-fp",
            "version": "1",
        }


class _Upstream:
    registered_tools = tuple(sorted(UPSTREAM_TOOL_SET))

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.calls.append((name, arguments))
        return {"ok": True, "result": {"tool": name}}


class _Dashboard:
    def health(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        return {"ok": True, "healthy": True}


def _runtime(
    *,
    executor: Any = None,
    bridge: Any = None,
) -> tuple[CompanionRuntime, dict[str, Any]]:
    nonces = InMemoryNonceReplayStore()
    verifier = ActorAssertionVerifier(ACTOR_KEY, nonce_store=nonces)
    confirmations = InMemoryConfirmationTokenStore()
    calls: dict[str, Any] = {"executor": [], "bridge": []}

    def _bridge(**kwargs: object) -> None:
        calls["bridge"].append(kwargs)
        if bridge is not None:
            bridge(**kwargs)

    def _executor(**kwargs: object) -> Mapping[str, object]:
        calls["executor"].append(kwargs)
        if executor is not None:
            return executor(**kwargs)
        return {"ok": True, "status": "executed"}

    handlers = {
        name: (lambda arguments, **_kwargs: {"ok": True, "result": dict(arguments)})
        for name in SHARED_TOOL_SET
    }
    runtime = CompanionRuntime.for_testing(
        actor_verifier=verifier,
        confirmation_store=confirmations,
        policy=_Policy(),
        safe_handlers=handlers,
        upstream=_Upstream(),
        confirmation_bridge=_bridge,
        confirmation_executor=_executor,
        helper_key=HELPER_KEY,
        dashboard_api_key=DASHBOARD_KEY,
        dashboard_handlers={"health": _Dashboard().health},
    )
    return runtime, calls


def _actor(
    tool: str,
    arguments: Mapping[str, object],
    *,
    user_id: int = 7,
    chat_id: int = 700,
    role: str | None = None,
    audience: str = "media-companion",
    **kwargs: object,
) -> str:
    signer = ActorAssertionSigner(ACTOR_KEY)
    return signer.issue(
        audience=audience,
        tool=tool,
        arguments=arguments,
        user_id=user_id,
        chat_id=chat_id,
        chat_type="private" if role == "admin" else "group",
        role=role or "user",
        update_id=int(kwargs.pop("update_id", 1)),
        update_type="message",
        message_id=kwargs.pop("message_id", 10),
        allowlist_fingerprint="policy-fp",
        allowlist_version="1",
        **kwargs,
    )


def _call(
    app: Any,
    path: str,
    *,
    body: bytes = b"",
    method: str = "POST",
    headers: Mapping[str, str] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 50000),
) -> tuple[int, dict[str, Any] | None]:
    header_pairs = [
        (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
    ]
    header_pairs.append((b"content-length", str(len(body)).encode()))
    messages: list[dict[str, Any]] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": header_pairs,
        "client": client,
        "server": ("localhost", 8000),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    chunks = [
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ]
    raw = b"".join(chunks)
    try:
        payload = json.loads(raw.decode()) if raw else None
    except json.JSONDecodeError:
        payload = None
    return int(start["status"]), payload


def _mcp_body(request_id: int, tool: str, arguments: Mapping[str, object]) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        separators=(",", ":"),
    ).encode()


def test_missing_duplicate_and_replayed_actor_are_denied() -> None:
    runtime, _calls = _runtime()
    app = create_app(runtime)
    args = {"query": "alien"}
    body = _mcp_body(1, "search_media", args)

    status, _ = _call(
        app, MCP_PATH, body=body, headers={"content-type": "application/json"}
    )
    assert status == 401

    token = _actor("search_media", args)
    status, _ = _call(
        app,
        MCP_PATH,
        body=body,
        headers={
            "content-type": "application/json",
            "x-crbl-actor": token + "," + token,
        },
    )
    assert status == 401

    headers = {"content-type": "application/json", "x-crbl-actor": token}
    assert _call(app, MCP_PATH, body=body, headers=headers)[0] == 200
    assert _call(app, MCP_PATH, body=body, headers=headers)[0] == 403


def test_safe_operation_is_a_typed_mcp_result_decoded_by_hermes() -> None:
    runtime, _calls = _runtime()
    app = create_app(runtime)
    args = {"query": "3 Body Problem", "media_type": "series"}
    body = _mcp_body(41, "search_media", args)
    token = _actor("search_media", args, update_id=41)

    status, payload = _call(
        app,
        MCP_PATH,
        body=body,
        headers={"content-type": "application/json", "x-crbl-actor": token},
    )

    assert status == 200
    assert payload is not None
    result = _decode_result("search_media", payload)
    assert result.is_error is False
    assert result.content == ("search_media completed; use the structured result.",)
    assert result.structured_content == {
        "ok": True,
        "result": {"media_type": "series"},
        "tool": "search_media",
    }


def test_exact_args_and_tool_binding_default_deny() -> None:
    runtime, _calls = _runtime()
    app = create_app(runtime)
    args = {"query": "alien"}
    body = _mcp_body(1, "search_media", args)
    token = _actor("search_media", args)
    mismatch = _actor("search_media", {"query": "other"})
    assert (
        _call(
            app,
            MCP_PATH,
            body=body,
            headers={"content-type": "application/json", "x-crbl-actor": mismatch},
        )[0]
        == 403
    )

    unknown_body = _mcp_body(2, "resources/list", {})
    unknown = _actor("resources/list", {})
    status, payload = _call(
        app,
        MCP_PATH,
        body=unknown_body,
        headers={"content-type": "application/json", "x-crbl-actor": unknown},
    )
    assert status == 403
    assert payload and payload["error"]["message"] == "request denied"

    # Private/MCP paths are exact contracts; a slash-normalizing redirect
    # must not turn a copied URL into an accepted request.
    assert (
        _call(
            app,
            MCP_PATH + "/",
            body=body,
            headers={"content-type": "application/json", "x-crbl-actor": token},
        )[0]
        == 404
    )


def test_wrong_well_shaped_plex_capability_is_rejected_before_limiter_or_body() -> None:
    runtime, _calls = _runtime()

    class _CountingLimiter:
        calls = 0

        def allow(self) -> bool:
            self.calls += 1
            return True

    limiter = _CountingLimiter()
    runtime.plex_capability = "A" * 43
    runtime.plex_rate_limiter = limiter
    app = create_app(runtime)
    body_was_read = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal body_was_read
        body_was_read = True
        raise AssertionError("wrong Plex capability must not consume the body")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    path = "/private/plex/" + ("B" * 43)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"content-type", b"multipart/form-data"),
            (b"content-length", b"3"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 8000),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    assert start["status"] == 404
    assert limiter.calls == 0
    assert body_was_read is False


def test_configured_dnat_peer_is_accepted_but_unlisted_peer_is_denied() -> None:
    runtime, _calls = _runtime()
    runtime.plex_capability = "A" * 43
    runtime.trusted_ingress_peers = ("172.20.0.1",)

    app = create_app(runtime)
    path = "/private/plex/" + ("A" * 43)
    # The configured bridge/gateway peer reaches the parser and is rejected
    # only for the intentionally empty/malformed body.  A peer outside the
    # exact allowlist is rejected at the network boundary first.
    accepted_status, _ = _call(
        app,
        path,
        body=b"not-a-webhook",
        headers={"content-type": "application/octet-stream"},
        client=("172.20.0.1", 50000),
    )
    denied_status, _ = _call(
        app,
        path,
        body=b"not-a-webhook",
        headers={"content-type": "application/octet-stream"},
        client=("172.20.0.2", 50000),
    )
    assert accepted_status == 400
    assert denied_status == 404


def test_non_admin_admin_tool_is_denied_and_mutation_only_previews() -> None:
    runtime, calls = _runtime()
    app = create_app(runtime)
    read_args: dict[str, object] = {}
    read_body = _mcp_body(1, "plex_get_libraries", read_args)
    user = _actor("plex_get_libraries", read_args)
    assert (
        _call(
            app,
            MCP_PATH,
            body=read_body,
            headers={"content-type": "application/json", "x-crbl-actor": user},
        )[0]
        == 403
    )

    mutation_args = {"movie_id": 42}
    mutation_body = _mcp_body(2, "radarr_add_movie", mutation_args)
    admin = _actor(
        "radarr_add_movie", mutation_args, user_id=99, chat_id=990, role="admin"
    )
    status, payload = _call(
        app,
        MCP_PATH,
        body=mutation_body,
        headers={"content-type": "application/json", "x-crbl-actor": admin},
    )
    assert status == 200
    assert payload and payload["result"]["confirmation_required"] is True
    assert calls["executor"] == []


def test_confirmation_bind_and_actor_bound_callback_execute_once() -> None:
    runtime, calls = _runtime()
    app = create_app(runtime)
    args = {"movie_id": 42}
    body = _mcp_body(1, "radarr_add_movie", args)
    admin = _actor(
        "radarr_add_movie", args, user_id=99, chat_id=990, role="admin", update_id=50
    )
    status, _ = _call(
        app,
        MCP_PATH,
        body=body,
        headers={"content-type": "application/json", "x-crbl-actor": admin},
    )
    assert status == 200
    token = str(calls["bridge"][0]["token"])
    preview = str(calls["bridge"][0]["preview"])
    bind_body = json.dumps(
        {"token": token, "chat_id": 990, "message_id": 55, "preview": preview}
    ).encode()
    bind_headers = {
        "content-type": "application/json",
        "x-crbl-confirm-key": HELPER_KEY.decode(),
    }
    assert _call(app, CONFIRM_BIND_PATH, body=bind_body, headers=bind_headers)[0] == 200

    callback_args = {
        "token": token,
        "callback_query_id": "q-1",
        "chat_id": 990,
        "message_id": 55,
    }
    callback_token = _actor(
        "confirmation_callback",
        callback_args,
        user_id=99,
        chat_id=990,
        role="admin",
        audience="confirmation-callback",
        message_id=55,
        callback_query_id="q-1",
        capability_hash=hashlib.sha256(token.encode()).hexdigest(),
        update_id=51,
    )
    callback_body = json.dumps(callback_args).encode()
    callback_headers = {
        "content-type": "application/json",
        "x-crbl-actor": callback_token,
    }
    assert (
        _call(app, CONFIRM_CALLBACK_PATH, body=callback_body, headers=callback_headers)[
            0
        ]
        == 200
    )
    assert len(calls["executor"]) == 1
    assert calls["executor"][0]["arguments"] == args
    assert (
        _call(app, CONFIRM_CALLBACK_PATH, body=callback_body, headers=callback_headers)[
            0
        ]
        == 403
    )


def test_hermes_owns_preview_delivery_and_custom_bind_auth_is_explicit() -> None:
    runtime, calls = _runtime()
    runtime.confirmation_delivery_owner = "hermes"
    runtime.bind_authorizer = lambda **_kwargs: None
    app = create_app(runtime)
    args = {"movie_id": 44}
    token = _actor(
        "radarr_add_movie",
        args,
        user_id=99,
        chat_id=990,
        role="admin",
        update_id=70,
    )
    status, payload = _call(
        app,
        MCP_PATH,
        body=_mcp_body(1, "radarr_add_movie", args),
        headers={"content-type": "application/json", "x-crbl-actor": token},
    )
    assert status == 200
    assert calls["bridge"] == []
    assert payload and len(payload["result"]["confirmation"]["token"]) == 43

    confirmation = payload["result"]["confirmation"]
    bind_body = json.dumps(
        {
            "token": confirmation["token"],
            "chat_id": 990,
            "message_id": 57,
            "preview": payload["result"]["preview"],
        }
    ).encode()
    bind_status, _ = _call(
        app,
        CONFIRM_BIND_PATH,
        body=bind_body,
        headers={
            "content-type": "application/json",
            "x-crbl-confirm-key": HELPER_KEY.decode(),
        },
    )
    assert bind_status == 403


def test_confirmation_bind_accepts_the_checked_in_hermes_actor_contract() -> None:
    runtime, calls = _runtime()
    app = create_app(runtime)
    args = {"movie_id": 43}
    body = _mcp_body(1, "radarr_add_movie", args)
    admin = _actor(
        "radarr_add_movie", args, user_id=99, chat_id=990, role="admin", update_id=60
    )
    assert (
        _call(
            app,
            MCP_PATH,
            body=body,
            headers={"content-type": "application/json", "x-crbl-actor": admin},
        )[0]
        == 200
    )
    token = str(calls["bridge"][0]["token"])
    preview = str(calls["bridge"][0]["preview"])
    bind_args = {"token": token, "chat_id": 990, "message_id": 56, "preview": preview}
    bind_actor = _actor(
        "confirmation_bind",
        bind_args,
        user_id=99,
        chat_id=990,
        role="admin",
        message_id=56,
        update_id=61,
    )
    status, payload = _call(
        app,
        CONFIRM_BIND_PATH,
        body=json.dumps(bind_args, separators=(",", ":")).encode(),
        headers={"content-type": "application/json", "x-crbl-actor": bind_actor},
    )
    assert status == 200
    assert payload and payload["state"] == "armed"


def test_dashboard_requires_signed_request_and_body_limit_is_bounded() -> None:
    runtime, _calls = _runtime()
    app = create_app(runtime)
    body = b"{}"
    assert (
        _call(
            app,
            "/private/dashboard/health",
            body=body,
            headers={"content-type": "application/json"},
        )[0]
        == 401
    )

    timestamp = int(time.time())
    expires = timestamp + 60
    actor = "dashboard-admin"
    nonce = "N" * 16
    digest = hashlib.sha256(body).hexdigest()
    message = "\n".join(
        (
            "dashboard-v1",
            "POST",
            "/private/dashboard/health",
            "health",
            actor,
            digest,
            str(timestamp),
            str(expires),
            nonce,
        )
    ).encode()
    signed = hmac.new(DASHBOARD_KEY, message, hashlib.sha256).hexdigest()
    dashboard_headers = {
        "content-type": "application/json",
        "x-crbl-dashboard-version": "dashboard-v1",
        "x-crbl-dashboard-operation": "health",
        "x-crbl-dashboard-actor": actor,
        "x-crbl-dashboard-timestamp": str(timestamp),
        "x-crbl-dashboard-expires": str(expires),
        "x-crbl-dashboard-nonce": nonce,
        "x-crbl-dashboard-body-sha256": digest,
        "x-crbl-dashboard-signature": signed,
    }
    status, payload = _call(
        app, "/private/dashboard/health", body=body, headers=dashboard_headers
    )
    assert status == 200
    assert payload and payload["data"]["healthy"] is True

    oversized = b"{" + b'"x":' + b'"a"' * 70_000 + b"}"
    token = _actor("search_media", {"query": "x"})
    status, _ = _call(
        app,
        MCP_PATH,
        body=oversized,
        headers={"content-type": "application/json", "x-crbl-actor": token},
    )
    assert status == 413


def test_dashboard_canonical_session_and_audit_fields_are_signature_bound() -> None:
    from media_dashboard.companion import sign_request

    runtime, _calls = _runtime()
    app = create_app(runtime)
    body = b"{}"
    _signature, signed = sign_request(
        DASHBOARD_KEY,
        method="POST",
        path="/private/dashboard/health",
        operation="health",
        actor="session-admin",
        body=body,
        timestamp=int(time.time()),
        nonce="S" * 16,
        session_digest="e" * 64,
        audit_context="dashboard:session:health",
    )
    signed.update({"content-type": "application/json"})
    status, payload = _call(
        app,
        "/private/dashboard/health",
        body=body,
        headers=signed,
    )
    assert status == 200
    assert payload and payload["data"]["healthy"] is True


def test_dashboard_mutation_is_preview_only_and_truthy_text_never_bypasses_guard() -> (
    None
):
    runtime, _calls = _runtime()
    app = create_app(runtime)
    body = json.dumps(
        {"user_id": 7, "fingerprint": "fp", "idempotency_key": "once"},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    def signed(nonce: str, payload: bytes) -> dict[str, str]:
        now = int(time.time())
        expires = now + 60
        digest = hashlib.sha256(payload).hexdigest()
        message = "\n".join(
            (
                "dashboard-v1",
                "POST",
                "/private/dashboard/users.add",
                "users.add",
                "signed-admin",
                digest,
                str(now),
                str(expires),
                nonce,
            )
        ).encode()
        return {
            "content-type": "application/json",
            "x-crbl-dashboard-version": "dashboard-v1",
            "x-crbl-dashboard-operation": "users.add",
            "x-crbl-dashboard-actor": "signed-admin",
            "x-crbl-dashboard-timestamp": str(now),
            "x-crbl-dashboard-expires": str(expires),
            "x-crbl-dashboard-nonce": nonce,
            "x-crbl-dashboard-body-sha256": digest,
            "x-crbl-dashboard-signature": hmac.new(
                DASHBOARD_KEY, message, hashlib.sha256
            ).hexdigest(),
        }

    status, payload = _call(
        app,
        "/private/dashboard/users.add",
        body=body,
        headers=signed("P" * 16, body),
    )
    assert status == 200
    assert payload and payload["data"]["confirmation_required"] is True
    preview_digest = str(payload["data"]["preview_digest"])

    confirmed = json.dumps(
        {
            "user_id": 7,
            "fingerprint": "fp",
            "idempotency_key": "once",
            "preview_digest": preview_digest,
            "confirmation": "copied-text",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    # No injected one-time dashboard guard exists in this test runtime; a
    # merely truthy browser value therefore remains denied and cannot execute.
    assert (
        _call(
            app,
            "/private/dashboard/users.add",
            body=confirmed,
            headers=signed("Q" * 16, confirmed),
        )[0]
        == 403
    )


def test_dashboard_service_principal_uses_dedicated_rate_scope() -> None:
    runtime, _calls = _runtime()
    identity = {
        "actor": DASHBOARD_SERVICE_ACTOR,
        "allowed": True,
        "role": "admin",
        "fingerprint": "helper-fingerprint",
        "version": "helper-version",
    }
    runtime.dashboard_identity_resolver = lambda **_kwargs: identity
    runtime.dashboard_policy_recheck = lambda **_kwargs: identity

    class _RateLimiter:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def enforce(self, operation: str, **kwargs: object) -> bool:
            self.calls.append({"operation": operation, **kwargs})
            return True

    limiter = _RateLimiter()
    runtime.rate_limiter = limiter
    app = create_app(runtime)
    body = b"{}"
    timestamp = int(time.time())
    expires = timestamp + 60
    digest = hashlib.sha256(body).hexdigest()
    nonce = "R" * 16
    message = "\n".join(
        (
            "dashboard-v1",
            "POST",
            "/private/dashboard/health",
            "health",
            DASHBOARD_SERVICE_ACTOR,
            digest,
            str(timestamp),
            str(expires),
            nonce,
        )
    ).encode()
    headers = {
        "content-type": "application/json",
        "x-crbl-dashboard-version": "dashboard-v1",
        "x-crbl-dashboard-operation": "health",
        "x-crbl-dashboard-actor": DASHBOARD_SERVICE_ACTOR,
        "x-crbl-dashboard-timestamp": str(timestamp),
        "x-crbl-dashboard-expires": str(expires),
        "x-crbl-dashboard-nonce": nonce,
        "x-crbl-dashboard-body-sha256": digest,
        "x-crbl-dashboard-signature": hmac.new(
            DASHBOARD_KEY, message, hashlib.sha256
        ).hexdigest(),
    }
    assert _call(app, "/private/dashboard/health", body=body, headers=headers)[0] == 200
    assert limiter.calls == [
        {
            "operation": "shared_read",
            "user_id": DASHBOARD_RATE_USER_ID,
            "chat_id": DASHBOARD_RATE_CHAT_ID,
            "actor_user_id": DASHBOARD_RATE_USER_ID,
            "actor_chat_id": DASHBOARD_RATE_CHAT_ID,
        }
    ]


def test_dashboard_confirmation_returns_raw_capability_and_executes_bound_record() -> (
    None
):
    runtime, _calls = _runtime()
    identity = {
        "actor": DASHBOARD_SERVICE_ACTOR,
        "allowed": True,
        "role": "admin",
        "fingerprint": "helper-fingerprint",
        "version": "1",
    }
    runtime.dashboard_identity_resolver = lambda **_kwargs: identity
    runtime.dashboard_policy_recheck = lambda **_kwargs: identity
    runtime.dashboard_mutation_guard = lambda **_kwargs: {"allowed": True}
    issued: dict[str, object] = {}
    consumed: list[dict[str, object]] = []
    executed: list[dict[str, object]] = []

    def issue(**kwargs: object) -> Mapping[str, object]:
        issued.update(kwargs)
        return {
            "confirmation_capability": "C" * 43,
            "expires_at": int(time.time()) + 300,
        }

    def consume(**kwargs: object) -> Mapping[str, object]:
        consumed.append(kwargs)
        return {
            "operation": kwargs["operation"],
            "arguments": kwargs["arguments"],
            "consumed": True,
        }

    def execute(**kwargs: object) -> Mapping[str, object]:
        executed.append(kwargs)
        return {"status": "executed"}

    runtime.dashboard_confirmation_issuer = issue
    runtime.dashboard_confirmation_guard = consume
    runtime.dashboard_handlers["users.add"] = execute
    app = create_app(runtime)

    def signed(nonce: str, payload: bytes) -> dict[str, str]:
        now = int(time.time())
        expires = now + 60
        digest = hashlib.sha256(payload).hexdigest()
        message = "\n".join(
            (
                "dashboard-v1",
                "POST",
                "/private/dashboard/users.add",
                "users.add",
                DASHBOARD_SERVICE_ACTOR,
                digest,
                str(now),
                str(expires),
                nonce,
            )
        ).encode()
        return {
            "content-type": "application/json",
            "x-crbl-dashboard-version": "dashboard-v1",
            "x-crbl-dashboard-operation": "users.add",
            "x-crbl-dashboard-actor": DASHBOARD_SERVICE_ACTOR,
            "x-crbl-dashboard-timestamp": str(now),
            "x-crbl-dashboard-expires": str(expires),
            "x-crbl-dashboard-nonce": nonce,
            "x-crbl-dashboard-body-sha256": digest,
            "x-crbl-dashboard-signature": hmac.new(
                DASHBOARD_KEY, message, hashlib.sha256
            ).hexdigest(),
        }

    initial = json.dumps(
        {
            "user_id": 7,
            "fingerprint": "helper-fingerprint",
            "version": 1,
            "idempotency_key": "cap-1",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    status, payload = _call(
        app,
        "/private/dashboard/users.add",
        body=initial,
        headers=signed("C" * 16, initial),
    )
    assert status == 200
    assert payload and payload["data"]["confirmation_capability"] == "C" * 43
    assert issued["operation"] == "users.add"
    preview_digest = str(payload["data"]["preview_digest"])
    confirmed = json.dumps(
        {
            "user_id": 7,
            "fingerprint": "helper-fingerprint",
            "version": 1,
            "idempotency_key": "cap-1",
            "preview_digest": preview_digest,
            "confirmation": "C" * 43,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    status, _payload = _call(
        app,
        "/private/dashboard/users.add",
        body=confirmed,
        headers=signed("D" * 16, confirmed),
    )
    assert status == 200
    assert len(consumed) == 1 and len(executed) == 1
    assert consumed[0]["arguments"] == executed[0]["arguments"]


def test_production_runtime_rejects_in_process_auth_state() -> None:
    verifier = ActorAssertionVerifier(ACTOR_KEY, nonce_store=InMemoryNonceReplayStore())
    with pytest.raises(DurableStoreRequiredError):
        CompanionRuntime(
            actor_verifier=verifier, confirmation_store=InMemoryConfirmationTokenStore()
        )


def test_background_worker_task_does_not_block_lifespan_startup() -> None:
    class BackgroundWorker:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.task: asyncio.Task[None] | None = None

        async def run(self) -> None:
            await self.release.wait()

        def start(self) -> asyncio.Task[None]:
            self.task = asyncio.create_task(self.run())
            return self.task

        async def stop(self) -> None:
            self.release.set()
            if self.task is not None:
                await self.task

    async def exercise() -> None:
        runtime, _calls = _runtime()
        worker = BackgroundWorker()
        runtime.worker = worker

        await asyncio.wait_for(runtime.start_worker(), timeout=0.1)

        assert runtime._worker_started is True
        assert runtime._worker_task is worker.task
        assert worker.task is not None and not worker.task.done()
        await runtime.stop_worker()

    asyncio.run(exercise())
