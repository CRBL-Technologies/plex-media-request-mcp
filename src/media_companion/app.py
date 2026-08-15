"""Closed ASGI application boundary for the media companion.

Only Hermes talks to ``/mcp``.  The endpoint is deliberately *not* a generic
MCP server: it accepts one bounded JSON-RPC method (``tools/call``), checks one
actor assertion against the exact received arguments and current Hermes
policy, and dispatches only the frozen tool inventories.  Private routes are
kept separate from the model-facing endpoint for confirmation callbacks,
dashboard operations, and the loopback Plex webhook receiver.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
import importlib
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Final, cast
from urllib.parse import urlsplit

from .auth import (
    ActorAssertionVerifier,
    ActorClaims,
    ConfirmationRecord,
    canonical_argument_hash,
    parse_json,
)
from .operations import (
    CONFIRMATION_AUDIENCE,
    CONFIRMATION_TOOL,
    MCP_AUDIENCE,
    MAX_DASHBOARD_BODY_BYTES,
    MAX_DASHBOARD_RESPONSE_BYTES,
    MAX_OPERATION_ARGUMENT_BYTES,
    MAX_OPERATION_RESPONSE_BYTES,
    DASHBOARD_OPERATION_SET,
    ActorPolicy,
    CompanionRuntime,
    DurableStoreRequiredError,
    OperationBoundaryError,
    OperationDependencyError,
    OperationValidationError,
    _safe_actor_arguments,
    _secret_key,
    _target_identity,
    confirmation_result,
    invoke_handler,
    render_confirmation_preview,
    safe_operation_result,
)
from .plex_ingress import (
    BODY_PARSE_DEADLINE_SECONDS,
    PlexIngressError,
    WebhookCapabilityError,
    WebhookContentTypeError,
    WebhookLimitError,
    WebhookPersistenceError,
    WebhookValidationError,
    parse_plex_webhook,
    validate_capability,
)
from .rate_limit import RateLimitExceeded
from .tool_policy import (
    ADMIN_MUTATING_TOOLS,
    ADMIN_TOOL_SET,
    SHARED_TOOL_SET,
    UPSTREAM_READ_ONLY_TOOLS,
    classify_admin_tool,
)

try:  # Starlette is transitively supplied by the production ASGI image.
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route
except (
    ImportError
):  # pragma: no cover - lets pure unit tests import this module without extras
    Starlette = None  # type: ignore[assignment,misc]
    Request = object  # type: ignore[assignment,misc]
    JSONResponse = None  # type: ignore[assignment,misc]
    Response = object  # type: ignore[assignment,misc]
    Route = None  # type: ignore[assignment,misc]


MAX_MCP_BODY_BYTES: Final[int] = MAX_OPERATION_ARGUMENT_BYTES
MAX_MCP_RESPONSE_BYTES: Final[int] = MAX_OPERATION_RESPONSE_BYTES
MAX_PRIVATE_BODY_BYTES: Final[int] = 64 * 1024
MAX_PRIVATE_RESPONSE_BYTES: Final[int] = 256 * 1024
MAX_PLEX_BODY_BYTES: Final[int] = 10 * 1024 * 1024
MAX_POLICY_USERS: Final[int] = 256
MAX_BLOCKED_CONTACTS: Final[int] = 256
MCP_PATH: Final[str] = "/mcp"
CONFIRM_BIND_PATH: Final[str] = "/private/confirm/bind"
CONFIRM_CALLBACK_PATH: Final[str] = "/private/confirm/callback"
CONFIRM_BIND_TOOL: Final[str] = "confirmation_bind"
DASHBOARD_PREFIX: Final[str] = "/private/dashboard/"
OPERATIONS_PREFIX: Final[str] = "/private/operations/"
PLEX_PREFIX: Final[str] = "/private/plex/"
DASHBOARD_SIGNATURE_VERSION: Final[str] = "dashboard-v1"
DASHBOARD_SERVICE_ACTOR: Final[str] = "dashboard-admin"
# Dashboard requests use one stable service-principal bucket.  These values
# are deliberately outside ordinary Telegram ID ranges and never come from
# browser input or the helper's user records.
DASHBOARD_RATE_USER_ID: Final[int] = (1 << 53) - 1
DASHBOARD_RATE_CHAT_ID: Final[int] = -((1 << 53) - 1)
DASHBOARD_CLOCK_SKEW_SECONDS: Final[int] = 30
DASHBOARD_REQUEST_LIFETIME_SECONDS: Final[int] = 60
DASHBOARD_OPERATIONS: Final[frozenset[str]] = DASHBOARD_OPERATION_SET
DASHBOARD_READ_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "health",
        "users",
        "users.resolve",
        "blocked",
        "subscriptions",
        "deliveries",
        "quarantine",
        "oracle",
        "audit",
    }
)
DASHBOARD_MUTATION_OPERATIONS: Final[frozenset[str]] = (
    DASHBOARD_OPERATIONS - DASHBOARD_READ_OPERATIONS
)
_DASHBOARD_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_DASHBOARD_SIG_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_DASHBOARD_HEADER_NAMES: Final[tuple[str, ...]] = (
    "x-crbl-dashboard-version",
    "x-crbl-dashboard-operation",
    "x-crbl-dashboard-actor",
    "x-crbl-dashboard-timestamp",
    "x-crbl-dashboard-expires",
    "x-crbl-dashboard-nonce",
    "x-crbl-dashboard-body-sha256",
    "x-crbl-dashboard-signature",
)
_DASHBOARD_OPTIONAL_HEADER_NAMES: Final[tuple[str, ...]] = (
    "x-crbl-dashboard-session-digest",
    "x-crbl-dashboard-audit",
)
_PLEX_CAPABILITY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{43}$")


class HTTPBoundaryError(Exception):
    """Internal status-only exception; details never cross the HTTP boundary."""

    def __init__(self, status: int = 400, *, code: str = "invalid_request") -> None:
        super().__init__()
        self.status = status
        self.code = code


def _json_dumps(value: object, *, maximum: int) -> bytes:
    try:
        body = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HTTPBoundaryError(500, code="response_unavailable") from exc
    if len(body) > maximum:
        raise HTTPBoundaryError(500, code="response_too_large")
    return body


def _raw_headers(scope: Mapping[str, object]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    values = scope.get("headers", ())
    if not isinstance(values, Sequence):
        return result
    for pair in values:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            continue
        key, value = pair
        if isinstance(key, bytes):
            key = key.decode("latin-1")
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        if isinstance(key, str) and isinstance(value, str):
            result.append((key, value))
    return result


def _header_values(scope: Mapping[str, object], name: str) -> list[str]:
    wanted = name.casefold()
    return [value for key, value in _raw_headers(scope) if key.casefold() == wanted]


def _single_raw_header(
    scope: Mapping[str, object], name: str, *, required: bool = True
) -> str | None:
    values = _header_values(scope, name)
    if len(values) != 1:
        if not values and not required:
            return None
        raise HTTPBoundaryError(401, code="authentication_required")
    value = values[0].strip()
    if not value or "," in value:
        raise HTTPBoundaryError(401, code="authentication_required")
    return value


def _content_type(scope: Mapping[str, object]) -> str:
    value = _single_raw_header(scope, "content-type", required=False)
    if value is None:
        return ""
    return value.split(";", 1)[0].strip().lower()


def _content_length(scope: Mapping[str, object]) -> int | None:
    raw = _single_raw_header(scope, "content-length", required=False)
    if raw is None:
        return None
    if not re.fullmatch(r"[0-9]+", raw):
        raise HTTPBoundaryError(400, code="invalid_content_length")
    parsed = int(raw)
    if parsed < 0:
        raise HTTPBoundaryError(400, code="invalid_content_length")
    return parsed


async def _read_body(
    request: Request,
    *,
    maximum: int,
    deadline_seconds: float | None = None,
) -> bytes:
    deadline_at = (
        None if deadline_seconds is None else time.monotonic() + deadline_seconds
    )
    if deadline_at is not None:
        # The parser receives the remaining time after buffering, making the
        # body-read plus parse operation one absolute budget rather than two
        # independent waits.
        cast(dict[str, object], request.scope)["_companion_deadline_at"] = deadline_at
    declared = _content_length(request.scope)
    if declared is not None and declared > maximum:
        raise HTTPBoundaryError(413, code="body_too_large")
    chunks: list[bytes] = []
    total = 0
    try:
        import asyncio

        async def consume() -> None:
            nonlocal total
            async for chunk in request.stream():
                if deadline_at is not None and time.monotonic() >= deadline_at:
                    raise HTTPBoundaryError(408, code="body_deadline_exceeded")
                if not isinstance(chunk, bytes):
                    raise HTTPBoundaryError(400, code="invalid_body")
                total += len(chunk)
                if total > maximum:
                    raise HTTPBoundaryError(413, code="body_too_large")
                chunks.append(chunk)

        if deadline_at is None:
            await consume()
        else:
            remaining = max(0.0, deadline_at - time.monotonic())
            try:
                async with asyncio.timeout(remaining):
                    await consume()
            except TimeoutError as exc:
                raise HTTPBoundaryError(408, code="body_deadline_exceeded") from exc
        if declared is not None and total != declared:
            raise HTTPBoundaryError(400, code="invalid_content_length")
    except HTTPBoundaryError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPBoundaryError(400, code="body_read_failed") from exc
    return b"".join(chunks)


def _check_body_deadline(scope: Mapping[str, object]) -> None:
    deadline = scope.get("_companion_deadline_at")
    if isinstance(deadline, (int, float)) and time.monotonic() >= float(deadline):
        raise HTTPBoundaryError(408, code="body_deadline_exceeded")


def _parse_json_object(body: bytes, *, maximum: int) -> dict[str, Any]:
    if len(body) > maximum:
        raise HTTPBoundaryError(413, code="body_too_large")
    try:
        parsed = parse_json(body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPBoundaryError(400, code="invalid_json") from exc
    if not isinstance(parsed, dict):
        raise HTTPBoundaryError(400, code="object_required")
    return cast(dict[str, Any], parsed)


def _is_json_content_type(scope: Mapping[str, object]) -> bool:
    return _content_type(scope) == "application/json"


def _has_query(scope: Mapping[str, object]) -> bool:
    """Return whether an exact private route carried a query string."""

    query = scope.get("query_string", b"")
    return bool(query)


def _is_loopback(request: Request, runtime: CompanionRuntime) -> bool:
    checker = getattr(runtime, "loopback_checker", None)
    if callable(checker):
        try:
            return bool(checker(request))
        except Exception:
            return False
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    trusted_peers = getattr(runtime, "trusted_ingress_peers", ())
    if isinstance(host, str) and host in trusted_peers:
        return True
    # The path capability is not a network boundary.  Production accepts a
    # Docker DNAT peer only when it is explicitly listed in
    # ``trusted_ingress_peers`` (or when an injected checker validates an
    # authenticated proxy).  Keeping this branch absent is intentional: a
    # guessed/obtained capability must never make a bridge peer trusted.
    if host is None:
        # ASGI Unix-socket test clients sometimes omit ``client``.  Production
        # can set ``require_loopback_client`` to retain the fail-closed rule.
        return not bool(getattr(runtime, "require_loopback_client", runtime.production))
    return host in {"127.0.0.1", "::1", "localhost"}


def _error_payload(
    *, request_id: object | None, status: int, code: str
) -> dict[str, object]:
    if request_id is None:
        request_id = None
    rpc_code = -32600 if status == 400 else -32601 if status == 404 else -32001
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": rpc_code, "message": "request denied"},
    }


def _json_response(
    value: object, *, status: int = 200, maximum: int = MAX_PRIVATE_RESPONSE_BYTES
) -> Response:
    body = _json_dumps(value, maximum=maximum)
    if JSONResponse is not None:
        return JSONResponse(
            content=json.loads(body.decode("utf-8")),
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )
    # This branch is only for a local import without Starlette.  The actual
    # ASGI fallback below writes the same wire shape itself.
    return cast(Response, _FallbackResponse(body, status, "application/json"))


async def _maybe_call(function: object, **kwargs: object) -> object:
    if not callable(function):
        raise OperationDependencyError("injected operation is unavailable")
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        result = function(**kwargs)
    else:
        parameters = signature.parameters
        if any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            result = function(**kwargs)
        else:
            positional = []
            keyword: dict[str, object] = {}
            aliases = {
                "args": "arguments",
                "data": "arguments",
                "body": "arguments",
                "value": "arguments",
                "request": "payload",
            }
            for name, parameter in parameters.items():
                if parameter.kind in {
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                }:
                    continue
                source = name if name in kwargs else aliases.get(name, "")
                if source in kwargs:
                    if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                        positional.append(kwargs[source])
                    elif (
                        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                        and source == name
                    ):
                        keyword[name] = kwargs[source]
                    elif parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                        keyword[name] = kwargs[source]
                    else:
                        # An alias (for example ``args`` <- ``arguments``)
                        # has to be positional for a conventional one-argument
                        # fake.
                        positional.append(kwargs[source])
                elif parameter.default is inspect.Parameter.empty:
                    raise OperationDependencyError(
                        "injected operation signature is unsupported"
                    )
            result = function(*positional, **keyword)
    if inspect.isawaitable(result):
        return await cast(Any, result)
    return result


async def _consume_rate_limit(
    runtime: CompanionRuntime,
    operation: str,
    *,
    user_id: int | None = None,
    chat_id: int | None = None,
) -> None:
    """Atomically consume one frozen operation budget before dispatch.

    The injected limiter owns the durable counter and exact ceiling policy.
    This adapter intentionally accepts only the operation-aware methods used
    by ``media_companion.rate_limit``; a generic boolean callback cannot prove
    that user/chat/global scopes were checked and is therefore not a
    production dependency.
    """

    limiter = getattr(runtime, "rate_limiter", None)
    if limiter is None:
        if runtime.production:
            raise OperationDependencyError("rate limiter is unavailable")
        return
    method: object | None = None
    for name in ("enforce", "consume", "try_consume", "allow"):
        candidate = getattr(limiter, name, None)
        if callable(candidate):
            method = candidate
            break
    if method is None:
        raise OperationDependencyError("rate limiter is unavailable")
    try:
        decision = await _maybe_call(
            method,
            operation=operation,
            user_id=user_id,
            chat_id=chat_id,
            actor_user_id=user_id,
            actor_chat_id=chat_id,
        )
    except RateLimitExceeded as exc:
        raise HTTPBoundaryError(429, code="rate_limited") from exc
    except (OperationDependencyError, TypeError, ValueError) as exc:
        raise OperationDependencyError("rate limiter is unavailable") from exc
    allowed = getattr(decision, "allowed", decision)
    if allowed is False or not bool(allowed):
        raise HTTPBoundaryError(429, code="rate_limited")


def _request_id(value: object) -> int | str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise HTTPBoundaryError(400, code="invalid_rpc_id")
    if isinstance(value, int) and abs(value) > (1 << 53) - 1:
        raise HTTPBoundaryError(400, code="invalid_rpc_id")
    if isinstance(value, str) and (not value or len(value.encode("utf-8")) > 128):
        raise HTTPBoundaryError(400, code="invalid_rpc_id")
    return value


def _mcp_fields(document: Mapping[str, Any]) -> tuple[int | str, str, dict[str, Any]]:
    if set(document) - {"jsonrpc", "id", "method", "params"}:
        raise HTTPBoundaryError(400, code="invalid_rpc_request")
    if document.get("jsonrpc") != "2.0" or "id" not in document:
        raise HTTPBoundaryError(400, code="invalid_rpc_request")
    request_id = _request_id(document.get("id"))
    if document.get("method") != "tools/call":
        raise HTTPBoundaryError(404, code="method_not_allowed")
    params = document.get("params")
    if not isinstance(params, Mapping) or set(params) - {"name", "arguments"}:
        raise HTTPBoundaryError(400, code="invalid_tool_call")
    name = params.get("name")
    if not isinstance(name, str) or not name or len(name) > 128:
        raise HTTPBoundaryError(400, code="invalid_tool_call")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise HTTPBoundaryError(400, code="invalid_tool_arguments")
    return request_id, name, cast(dict[str, Any], dict(arguments))


def _assert_exact_tool(tool: str, *, shared: bool | None = None) -> None:
    if shared is True and tool not in SHARED_TOOL_SET:
        raise HTTPBoundaryError(403, code="tool_denied")
    if shared is False and tool not in ADMIN_TOOL_SET:
        raise HTTPBoundaryError(403, code="tool_denied")
    if shared is None and tool not in SHARED_TOOL_SET and tool not in ADMIN_TOOL_SET:
        raise HTTPBoundaryError(403, code="tool_denied")


async def _dispatch_shared(
    runtime: CompanionRuntime,
    tool: str,
    arguments: Mapping[str, Any],
    claims: ActorClaims,
    policy: ActorPolicy,
) -> object:
    handler = runtime.safe_handlers.get(tool)
    if handler is None and runtime.operations is not None:
        handler = getattr(runtime.operations, tool, None)
    if handler is None:
        raise OperationDependencyError("shared operation is unavailable")
    safe_args = (
        _safe_actor_arguments(cast(Mapping[str, Any], arguments), claims)
        if tool in {"request_movie", "request_series"}
        else dict(arguments)
    )
    return await invoke_handler(
        cast(Any, handler),
        cast(Mapping[str, Any], safe_args),
        claims=claims,
        policy=policy,
    )


async def _dispatch_admin_read(
    runtime: CompanionRuntime, tool: str, arguments: Mapping[str, Any]
) -> object:
    if tool not in UPSTREAM_READ_ONLY_TOOLS:
        raise OperationBoundaryError("admin read tool is not allowlisted")
    proxy = runtime.upstream
    if proxy is None:
        raise OperationDependencyError("upstream operation is unavailable")
    method = getattr(proxy, "call_tool", None)
    if not callable(method):
        method = getattr(proxy, "invoke", None)
    if not callable(method):
        raise OperationDependencyError("upstream operation is unavailable")
    result = method(tool, cast(Mapping[str, Any], arguments))
    if inspect.isawaitable(result):
        result = await cast(Any, result)
    return result


async def _persist_confirmation_arguments(
    runtime: CompanionRuntime,
    *,
    token_hash: str,
    tool: str,
    argument_hash: str,
    arguments: Mapping[str, Any],
    expires_at: int,
) -> None:
    """Bind exact admin arguments to a capability in durable state.

    The auth capability record intentionally stores only the argument hash.
    The separate adapter is the only place where the bounded arguments live;
    callback execution retrieves them by token hash and verifies the hash
    again before invoking a mutation handler.  A process-local fallback is
    retained solely for ``for_testing`` runtimes.
    """

    store = getattr(runtime, "confirmation_arguments_store", None)
    if store is not None:
        put = getattr(store, "put", None)
        if not callable(put):
            raise OperationDependencyError(
                "confirmation argument binding is unavailable"
            )
        result = await _maybe_call(
            put,
            token_hash=token_hash,
            tool=tool,
            argument_hash=argument_hash,
            arguments=dict(arguments),
            expires_at=expires_at,
        )
        if result is False:
            raise OperationDependencyError(
                "confirmation argument binding was not stored"
            )
        return
    if runtime.production:
        raise OperationDependencyError(
            "durable confirmation argument binding is unavailable"
        )
    resolver = getattr(runtime, "confirmation_arguments_resolver", None)
    if callable(resolver):
        # A resolver-only test seam is read-oriented and cannot prove that a
        # preview was persisted.  It is therefore deliberately not accepted
        # for issuing a new capability.
        raise OperationDependencyError("confirmation argument store is unavailable")
    bindings = getattr(runtime, "_test_confirmation_arguments", None)
    if not isinstance(bindings, dict):
        bindings = {}
        setattr(runtime, "_test_confirmation_arguments", bindings)
    bindings[token_hash] = {
        "tool": tool,
        "argument_hash": argument_hash,
        "arguments": dict(arguments),
        "expires_at": expires_at,
    }


async def _consume_confirmation_arguments(
    runtime: CompanionRuntime,
    *,
    record: object,
    token_hash: str,
    tool: str,
    argument_hash: str,
) -> dict[str, Any]:
    """Return and atomically consume the exact arguments for a callback."""

    store = getattr(runtime, "confirmation_arguments_store", None)
    value: object | None = None
    if store is not None:
        consume = getattr(store, "consume", None)
        if not callable(consume):
            raise OperationDependencyError(
                "confirmation argument binding is unavailable"
            )
        value = await _maybe_call(
            consume,
            token_hash=token_hash,
            tool=tool,
            argument_hash=argument_hash,
            record=record,
        )
    else:
        resolver = getattr(runtime, "confirmation_arguments_resolver", None)
        if callable(resolver):
            value = await _maybe_call(
                resolver,
                record=record,
                token_hash=token_hash,
                tool=tool,
                argument_hash=argument_hash,
            )
        elif not runtime.production:
            bindings = getattr(runtime, "_test_confirmation_arguments", None)
            if isinstance(bindings, dict):
                value = bindings.pop(token_hash, None)
    if isinstance(value, Mapping) and "arguments" in value:
        # Stores may return a typed envelope containing the verified binding.
        stored_tool = value.get("tool")
        stored_hash = value.get("argument_hash")
        if stored_tool is not None and stored_tool != tool:
            raise OperationBoundaryError("confirmation arguments are not bound")
        if stored_hash is not None and not hmac.compare_digest(
            str(stored_hash), argument_hash
        ):
            raise OperationBoundaryError("confirmation arguments are not bound")
        value = value.get("arguments")
    if not isinstance(value, Mapping):
        raise OperationDependencyError("confirmation arguments are unavailable")
    arguments = dict(value)
    try:
        actual_hash = canonical_argument_hash(arguments)
    except Exception as exc:  # noqa: BLE001
        raise OperationBoundaryError("confirmation arguments are invalid") from exc
    if not hmac.compare_digest(actual_hash, argument_hash):
        raise OperationBoundaryError("confirmation arguments are not bound")
    return arguments


async def _admin_preview(
    runtime: CompanionRuntime,
    tool: str,
    arguments: Mapping[str, Any],
    claims: ActorClaims,
    policy: ActorPolicy,
) -> dict[str, Any]:
    store = runtime.confirmation_store
    if store is None:
        raise OperationDependencyError("confirmation store is unavailable")
    args_hash = canonical_argument_hash(arguments)
    target = _target_identity(tool, cast(Mapping[str, Any], arguments))
    state = runtime.target_state_fingerprint(tool, cast(Mapping[str, Any], arguments))
    policy_version = policy.version or runtime.policy_version
    preview = render_confirmation_preview(
        tool,
        cast(Mapping[str, Any], arguments),
        target_identity=target,
        state_fingerprint=state,
        policy_version=policy_version,
    )
    try:
        token = store.create(
            actor_user_id=claims.user_id,
            actor_chat_id=claims.chat_id,
            tool=tool,
            argument_hash=args_hash,
            target_identity=target,
            state_fingerprint=state,
            preview=preview,
            policy_version=policy_version,
        )
    except Exception as exc:  # noqa: BLE001
        raise OperationDependencyError("confirmation could not be created") from exc
    token_hash = getattr(token, "token_hash", None)
    if not isinstance(token_hash, str) or not _DASHBOARD_SIG_RE.fullmatch(token_hash):
        token_hash = hashlib.sha256(str(token).encode("utf-8", "strict")).hexdigest()
    token_value = getattr(token, "value", token)
    if not isinstance(token_value, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{43}", token_value
    ):
        raise OperationDependencyError("confirmation capability is invalid")
    expires_at = int(getattr(token, "expires_at", int(time.time()) + 300))
    await _persist_confirmation_arguments(
        runtime,
        token_hash=token_hash,
        tool=tool,
        argument_hash=args_hash,
        arguments=arguments,
        expires_at=expires_at,
    )
    # Hermes' native extension is the sole production preview owner: it
    # receives the transient capability, sends the exact server-rendered
    # text, and calls the private bind route.  A companion-owned bridge is
    # available only when explicitly selected (primarily deterministic tests)
    # so a production request cannot send the same preview twice.
    owner = getattr(runtime, "confirmation_delivery_owner", "companion")
    bridge = runtime.confirmation_bridge
    if owner == "companion" and bridge is not None:
        # The plaintext token is passed only to trusted extension code.  It is
        # intentionally never included in the model-visible result below.
        await _maybe_call(
            bridge,
            token=token,
            preview=preview,
            tool=tool,
            claims=claims,
            callback_data="crblc:" + str(token),
        )
    elif owner != "hermes":
        raise OperationDependencyError("confirmation delivery owner is invalid")
    return confirmation_result(
        tool=tool,
        preview=preview,
        expires_at=expires_at,
        token_hash=token_hash,
        token=token_value,
    )


async def _mcp_endpoint(request: Request) -> Response:
    runtime: CompanionRuntime = request.app.state.runtime
    request_id: object | None = None
    try:
        if (
            request.method != "POST"
            or _has_query(request.scope)
            or not _is_json_content_type(request.scope)
        ):
            raise HTTPBoundaryError(400, code="json_post_required")
        body = await _read_body(
            request,
            maximum=MAX_MCP_BODY_BYTES,
            deadline_seconds=BODY_PARSE_DEADLINE_SECONDS,
        )
        _check_body_deadline(request.scope)
        document = _parse_json_object(body, maximum=MAX_MCP_BODY_BYTES)
        request_id, tool, arguments = _mcp_fields(document)
        _assert_exact_tool(tool)
        actor_header = _single_raw_header(request.scope, "X-CRBL-Actor")
        verifier = runtime.actor_verifier
        if verifier is None:
            raise OperationDependencyError("actor verifier is unavailable")
        try:
            claims = verifier.verify_bound(
                actor_header or "",
                expected_audience=MCP_AUDIENCE,
                expected_tool=tool,
                arguments=arguments,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPBoundaryError(403, code="actor_denied") from exc
        is_shared = tool in SHARED_TOOL_SET
        policy = runtime.current_policy(claims, require_admin=not is_shared)
        rate_operation = (
            "safe_request"
            if tool in {"request_movie", "request_series"}
            else "shared_read"
            if is_shared or tool not in ADMIN_MUTATING_TOOLS
            else "admin_preview"
        )
        await _consume_rate_limit(
            runtime,
            rate_operation,
            user_id=claims.user_id,
            chat_id=claims.chat_id,
        )
        if is_shared:
            if tool in {"request_movie", "request_series"}:
                runtime.claim_safe_mutation(claims)
            value = await _dispatch_shared(runtime, tool, arguments, claims, policy)
            result = safe_operation_result(value, tool=tool)
        elif tool in ADMIN_TOOL_SET:
            classification = classify_admin_tool(tool)
            if classification is None:
                raise HTTPBoundaryError(403, code="tool_denied")
            if tool in ADMIN_MUTATING_TOOLS:
                # MCP never accepts a model-supplied confirmation or executes
                # a privileged mutation.  The callback route is the sole
                # execution path after the trusted Telegram click.
                result = await _admin_preview(runtime, tool, arguments, claims, policy)
            else:
                value = await _dispatch_admin_read(runtime, tool, arguments)
                result = safe_operation_result(value, tool=tool)
        else:  # pragma: no cover - exact inventory check above
            raise HTTPBoundaryError(403, code="tool_denied")
        envelope = {"jsonrpc": "2.0", "id": request_id, "result": result}
        return _json_response(envelope, maximum=MAX_MCP_RESPONSE_BYTES)
    except HTTPBoundaryError as exc:
        return _json_response(
            _error_payload(request_id=request_id, status=exc.status, code=exc.code),
            status=exc.status,
            maximum=MAX_MCP_RESPONSE_BYTES,
        )
    except (
        OperationBoundaryError,
        OperationDependencyError,
        OperationValidationError,
        DurableStoreRequiredError,
    ):
        return _json_response(
            _error_payload(request_id=request_id, status=403, code="request_denied"),
            status=403,
            maximum=MAX_MCP_RESPONSE_BYTES,
        )
    except Exception:  # noqa: BLE001
        return _json_response(
            _error_payload(request_id=request_id, status=500, code="request_failed"),
            status=500,
            maximum=MAX_MCP_RESPONSE_BYTES,
        )


async def _helper_authenticated(request: Request, runtime: CompanionRuntime) -> None:
    authorizer = runtime.bind_authorizer
    if authorizer is not None:
        # The callback is itself a private injected dependency.  It may use a
        # richer HMAC helper contract than the simple key path below.
        values = _header_values(request.scope, "X-CRBL-Confirm-Key") + _header_values(
            request.scope, "X-CRBL-Helper-Key"
        )
        if len(values) != 1:
            raise HTTPBoundaryError(401, code="authentication_required")
        try:
            result = authorizer(key=values[0], headers=_raw_headers(request.scope))
        except TypeError:
            result = authorizer(values[0])
        if inspect.isawaitable(result):
            result = await cast(Any, result)
        # Authentication callbacks must have an explicit success contract.
        # In particular, an accidental ``None`` or arbitrary truthy object
        # must never arm a capability merely because the callback returned.
        authenticated = result is True
        if isinstance(result, Mapping):
            authenticated = any(
                result.get(field) is True
                for field in ("ok", "authenticated", "authorized")
            )
        elif result is not True:
            for field in ("ok", "authenticated", "authorized"):
                if getattr(result, field, object()) is True:
                    authenticated = True
                    break
        if not authenticated:
            raise HTTPBoundaryError(403, code="authentication_required")
        return
    values = _header_values(request.scope, "X-CRBL-Confirm-Key") + _header_values(
        request.scope, "X-CRBL-Helper-Key"
    )
    if len(values) != 1 or runtime.helper_key is None:
        raise HTTPBoundaryError(401, code="authentication_required")
    expected = (
        runtime.helper_key.encode("utf-8", "strict")
        if isinstance(runtime.helper_key, str)
        else runtime.helper_key
    )
    if not isinstance(expected, bytes) or not hmac.compare_digest(
        hashlib.sha256(values[0].encode("utf-8", "strict")).digest(),
        hashlib.sha256(expected).digest(),
    ):
        raise HTTPBoundaryError(403, code="authentication_required")


async def _bind_authenticated(
    request: Request,
    runtime: CompanionRuntime,
    payload: Mapping[str, Any],
) -> None:
    """Authenticate Hermes' exact confirmation-bind contract.

    The deployment contract normally supplies the private helper key.  The
    checked-in Hermes client currently sends its exact actor assertion instead
    (``aud=media-companion``, ``tool=confirmation_bind``), so that reviewed
    shape is accepted as a compatibility path.  Both paths are bounded and
    deny a missing or duplicate credential; neither is a generic proxy.
    """

    helper_values = _header_values(
        request.scope, "X-CRBL-Confirm-Key"
    ) + _header_values(request.scope, "X-CRBL-Helper-Key")
    if helper_values:
        await _helper_authenticated(request, runtime)
        return
    actor_header = _single_raw_header(request.scope, "X-CRBL-Actor", required=False)
    if actor_header is None:
        raise HTTPBoundaryError(401, code="authentication_required")
    verifier = runtime.actor_verifier
    if verifier is None:
        raise OperationDependencyError("actor verifier is unavailable")
    try:
        claims = verifier.verify_bound(
            actor_header,
            expected_audience=MCP_AUDIENCE,
            expected_tool=CONFIRM_BIND_TOOL,
            arguments=cast(dict[str, Any], dict(payload)),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPBoundaryError(403, code="authentication_required") from exc
    chat_id = payload.get("chat_id")
    message_id = payload.get("message_id")
    if (
        claims.chat_type != "private"
        or claims.chat_id != chat_id
        or claims.message_id != message_id
    ):
        raise HTTPBoundaryError(403, code="authentication_required")
    # The bind assertion is minted only for an admin preview, but the helper
    # policy is re-read here so a stale/revoked administrator cannot arm it.
    runtime.current_policy(claims, require_admin=True)


async def _confirm_bind_endpoint(request: Request) -> Response:
    runtime: CompanionRuntime = request.app.state.runtime
    try:
        if (
            request.method != "POST"
            or _has_query(request.scope)
            or not _is_json_content_type(request.scope)
        ):
            raise HTTPBoundaryError(400, code="json_post_required")
        # Helper-key authentication completes before buffering.  Hermes' own
        # actor-bound bind contract authenticates after parsing the bounded
        # body because its signature covers the exact payload.
        helper_values = _header_values(
            request.scope, "X-CRBL-Confirm-Key"
        ) + _header_values(request.scope, "X-CRBL-Helper-Key")
        if helper_values:
            await _helper_authenticated(request, runtime)
        body = await _read_body(
            request,
            maximum=MAX_PRIVATE_BODY_BYTES,
            deadline_seconds=BODY_PARSE_DEADLINE_SECONDS,
        )
        _check_body_deadline(request.scope)
        payload = _parse_json_object(body, maximum=MAX_PRIVATE_BODY_BYTES)
        if set(payload) - {"token", "chat_id", "message_id", "preview"}:
            raise HTTPBoundaryError(400, code="invalid_confirmation")
        token = payload.get("token")
        chat_id = payload.get("chat_id")
        message_id = payload.get("message_id")
        preview = payload.get("preview")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise HTTPBoundaryError(400, code="invalid_confirmation")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
            raise HTTPBoundaryError(400, code="invalid_confirmation")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            raise HTTPBoundaryError(400, code="invalid_confirmation")
        if (
            not isinstance(preview, str)
            or len(preview.encode("utf-8", "strict")) > 64 * 1024
        ):
            raise HTTPBoundaryError(400, code="invalid_confirmation")
        if not helper_values:
            await _bind_authenticated(request, runtime, payload)
        store = runtime.confirmation_store
        if store is None:
            raise OperationDependencyError("confirmation store is unavailable")
        record = store.bind(
            token, chat_id=chat_id, message_id=message_id, preview=preview
        )
        bound_hash = getattr(record, "token_hash", None)
        if not isinstance(bound_hash, str) or not _DASHBOARD_SIG_RE.fullmatch(
            bound_hash
        ):
            bound_hash = hashlib.sha256(token.encode("ascii", "strict")).hexdigest()
        return _json_response(
            {"ok": True, "state": "armed", "token_hash": bound_hash},
            maximum=MAX_PRIVATE_RESPONSE_BYTES,
        )
    except HTTPBoundaryError as exc:
        return _json_response(
            {"ok": False, "error": "request denied"},
            status=exc.status,
            maximum=MAX_PRIVATE_RESPONSE_BYTES,
        )
    except Exception:
        return _json_response(
            {"ok": False, "error": "request denied"},
            status=403,
            maximum=MAX_PRIVATE_RESPONSE_BYTES,
        )


def _callback_arguments(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "token": payload.get("token"),
        "callback_query_id": payload.get("callback_query_id"),
        "chat_id": payload.get("chat_id"),
        "message_id": payload.get("message_id"),
    }


async def _confirm_callback_endpoint(request: Request) -> Response:
    runtime: CompanionRuntime = request.app.state.runtime
    try:
        if (
            request.method != "POST"
            or _has_query(request.scope)
            or not _is_json_content_type(request.scope)
        ):
            raise HTTPBoundaryError(400, code="json_post_required")
        body = await _read_body(
            request,
            maximum=MAX_PRIVATE_BODY_BYTES,
            deadline_seconds=BODY_PARSE_DEADLINE_SECONDS,
        )
        _check_body_deadline(request.scope)
        payload = _parse_json_object(body, maximum=MAX_PRIVATE_BODY_BYTES)
        if set(payload) != {"token", "callback_query_id", "chat_id", "message_id"}:
            raise HTTPBoundaryError(400, code="invalid_callback")
        token = payload["token"]
        query_id = payload["callback_query_id"]
        chat_id = payload["chat_id"]
        message_id = payload["message_id"]
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise HTTPBoundaryError(400, code="invalid_callback")
        if not isinstance(query_id, str) or not query_id or len(query_id) > 256:
            raise HTTPBoundaryError(400, code="invalid_callback")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
            raise HTTPBoundaryError(400, code="invalid_callback")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            raise HTTPBoundaryError(400, code="invalid_callback")
        verifier = runtime.actor_verifier
        if verifier is None:
            raise OperationDependencyError("actor verifier is unavailable")
        actor_header = _single_raw_header(request.scope, "X-CRBL-Actor")
        callback_args = _callback_arguments(payload)
        try:
            claims = verifier.verify_bound(
                actor_header or "",
                expected_audience=CONFIRMATION_AUDIENCE,
                expected_tool=CONFIRMATION_TOOL,
                arguments=callback_args,
            )
        except Exception as exc:
            raise HTTPBoundaryError(403, code="callback_denied") from exc
        if (
            claims.chat_type != "private"
            or claims.chat_id != chat_id
            or claims.message_id != message_id
        ):
            raise HTTPBoundaryError(403, code="callback_denied")
        if claims.callback_query_id != query_id:
            raise HTTPBoundaryError(403, code="callback_denied")
        token_hash = hashlib.sha256(token.encode("ascii", "strict")).hexdigest()
        if claims.capability_hash is None or not hmac.compare_digest(
            claims.capability_hash, token_hash
        ):
            raise HTTPBoundaryError(403, code="callback_denied")
        store = runtime.confirmation_store
        if store is None:
            raise OperationDependencyError("confirmation store is unavailable")
        try:
            getter = getattr(store, "get", None)
            if not callable(getter):
                raise OperationDependencyError(
                    "confirmation store cannot inspect capabilities"
                )
            record = getter(token)
        except Exception as exc:
            raise HTTPBoundaryError(403, code="callback_denied") from exc
        if not isinstance(record, ConfirmationRecord):
            # Structural fakes are accepted as long as all binding fields are
            # present; the execution path never forwards the object raw.
            required = (
                "actor_user_id",
                "actor_chat_id",
                "tool",
                "argument_hash",
                "target_identity",
                "state_fingerprint",
                "policy_version",
            )
            if any(not hasattr(record, field) for field in required):
                raise HTTPBoundaryError(403, code="callback_denied")
        bound_tool = getattr(record, "tool", None)
        if not isinstance(bound_tool, str) or bound_tool not in ADMIN_MUTATING_TOOLS:
            raise HTTPBoundaryError(403, code="callback_denied")
        policy = runtime.current_policy(claims, require_admin=True)
        if (
            int(getattr(record, "actor_user_id")) != claims.user_id
            or int(getattr(record, "actor_chat_id")) != claims.chat_id
        ):
            raise HTTPBoundaryError(403, code="callback_denied")
        if claims.target_hash is not None:
            target_hash = hashlib.sha256(
                str(getattr(record, "target_identity")).encode("utf-8")
            ).hexdigest()
            if not hmac.compare_digest(claims.target_hash, target_hash):
                raise HTTPBoundaryError(403, code="callback_denied")
        state_check = getattr(runtime, "revalidate_confirmation", None)
        if callable(state_check):
            fresh = await _maybe_call(
                state_check, record=record, claims=claims, policy=policy
            )
            if fresh is False:
                raise HTTPBoundaryError(403, code="callback_denied")
        await _consume_rate_limit(
            runtime,
            "admin_execution",
            user_id=claims.user_id,
            chat_id=claims.chat_id,
        )
        try:
            consumed = store.consume(
                token,
                actor_user_id=claims.user_id,
                actor_chat_id=claims.chat_id,
                tool=bound_tool,
                argument_hash=str(getattr(record, "argument_hash")),
                target_identity=str(getattr(record, "target_identity")),
                state_fingerprint=str(getattr(record, "state_fingerprint")),
                policy_version=str(getattr(record, "policy_version")),
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception as exc:
            raise HTTPBoundaryError(403, code="callback_denied") from exc
        bound_arguments = await _consume_confirmation_arguments(
            runtime,
            record=consumed,
            token_hash=token_hash,
            tool=bound_tool,
            argument_hash=str(getattr(record, "argument_hash")),
        )
        executor = runtime.confirmation_executor
        if executor is None and runtime.operations is not None:
            executor = getattr(runtime.operations, "execute_confirmation", None)
        if executor is not None:
            value = await _maybe_call(
                executor,
                record=consumed,
                arguments=bound_arguments,
                claims=claims,
                policy=policy,
            )
            result = safe_operation_result(value, tool=bound_tool)
        else:
            result = {
                "ok": True,
                "status": "consumed",
                "tool": bound_tool,
            }
        return _json_response(
            {"ok": True, "result": result}, maximum=MAX_PRIVATE_RESPONSE_BYTES
        )
    except HTTPBoundaryError as exc:
        return _json_response(
            {"ok": False, "error": "request denied"},
            status=exc.status,
            maximum=MAX_PRIVATE_RESPONSE_BYTES,
        )
    except Exception:
        return _json_response(
            {"ok": False, "error": "request denied"},
            status=403,
            maximum=MAX_PRIVATE_RESPONSE_BYTES,
        )


def _dashboard_signature_message(
    *,
    method: str,
    path: str,
    operation: str,
    actor: str,
    body_digest: str,
    timestamp: int,
    expires_at: int,
    nonce: str,
    session_digest: str | None = None,
    audit_context: str | None = None,
) -> bytes:
    fields = [
        DASHBOARD_SIGNATURE_VERSION,
        method,
        path,
        operation,
        actor,
        body_digest,
        str(timestamp),
        str(expires_at),
        nonce,
    ]
    # These fields are optional for compatibility with older dashboard
    # callers, but when present they are covered by the signature exactly as
    # ``media_dashboard.companion.signature_message`` covers them.
    if session_digest is not None:
        fields.append(session_digest)
    if audit_context is not None:
        fields.append(audit_context)
    return "\n".join(fields).encode("utf-8", "strict")


def _verify_dashboard_request(
    request: Request, body: bytes, operation: str, runtime: CompanionRuntime
) -> str:
    key = _secret_key(runtime.dashboard_api_key or b"")
    if key is None:
        raise HTTPBoundaryError(503, code="dashboard_unavailable")
    values: dict[str, str] = {}
    for header in _DASHBOARD_HEADER_NAMES:
        # Header lookup is case-insensitive and duplicate-resistant.
        found = _header_values(request.scope, header)
        if len(found) != 1:
            raise HTTPBoundaryError(401, code="dashboard_denied")
        values[header] = found[0].strip()
    optional: dict[str, str | None] = {}
    for header in _DASHBOARD_OPTIONAL_HEADER_NAMES:
        found = _header_values(request.scope, header)
        if len(found) > 1:
            raise HTTPBoundaryError(401, code="dashboard_denied")
        optional[header] = found[0].strip() if found else None
    if values["x-crbl-dashboard-version"] != DASHBOARD_SIGNATURE_VERSION:
        raise HTTPBoundaryError(401, code="dashboard_denied")
    if values["x-crbl-dashboard-operation"] != operation:
        raise HTTPBoundaryError(401, code="dashboard_denied")
    actor = values["x-crbl-dashboard-actor"]
    if (
        not actor
        or len(actor.encode("utf-8", "strict")) > 128
        or any(character.isspace() or ord(character) < 0x20 for character in actor)
    ):
        raise HTTPBoundaryError(401, code="dashboard_denied")
    nonce = values["x-crbl-dashboard-nonce"]
    if not _DASHBOARD_TOKEN_RE.fullmatch(nonce):
        raise HTTPBoundaryError(401, code="dashboard_denied")
    try:
        timestamp = int(values["x-crbl-dashboard-timestamp"])
        expires_at = int(values["x-crbl-dashboard-expires"])
    except (TypeError, ValueError):
        raise HTTPBoundaryError(401, code="dashboard_denied")
    now = int(time.time())
    if (
        abs(now - timestamp) > DASHBOARD_CLOCK_SKEW_SECONDS
        or expires_at < timestamp
        or expires_at - timestamp > DASHBOARD_REQUEST_LIFETIME_SECONDS
        or expires_at <= now - DASHBOARD_CLOCK_SKEW_SECONDS
    ):
        raise HTTPBoundaryError(401, code="dashboard_denied")
    digest = hashlib.sha256(body).hexdigest()
    if values[
        "x-crbl-dashboard-body-sha256"
    ] != digest or not _DASHBOARD_SIG_RE.fullmatch(
        values["x-crbl-dashboard-body-sha256"]
    ):
        raise HTTPBoundaryError(401, code="dashboard_denied")
    if not _DASHBOARD_SIG_RE.fullmatch(values["x-crbl-dashboard-signature"]):
        raise HTTPBoundaryError(401, code="dashboard_denied")
    session_digest = optional["x-crbl-dashboard-session-digest"]
    if session_digest is not None and not _DASHBOARD_SIG_RE.fullmatch(session_digest):
        raise HTTPBoundaryError(401, code="dashboard_denied")
    audit_context = optional["x-crbl-dashboard-audit"]
    if audit_context is not None and (
        not audit_context
        or len(audit_context.encode("utf-8", "strict")) > 512
        or any(
            ord(character) < 0x20 or character in "\r\n" for character in audit_context
        )
    ):
        raise HTTPBoundaryError(401, code="dashboard_denied")
    message = _dashboard_signature_message(
        method=request.method,
        path=request.url.path,
        operation=operation,
        actor=actor,
        body_digest=digest,
        timestamp=timestamp,
        expires_at=expires_at,
        nonce=nonce,
        session_digest=session_digest,
        audit_context=audit_context,
    )
    expected = hmac.new(key, message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, values["x-crbl-dashboard-signature"]):
        raise HTTPBoundaryError(401, code="dashboard_denied")
    try:
        nonce_store = runtime.nonce_replay_store()
        consume = getattr(nonce_store, "consume", None)
        if not callable(consume):
            raise OperationDependencyError("dashboard replay store is unavailable")
        fresh = consume(
            "dashboard:" + nonce, expires_at + DASHBOARD_CLOCK_SKEW_SECONDS, now=now
        )
    except Exception as exc:
        raise HTTPBoundaryError(503, code="dashboard_unavailable") from exc
    if not fresh:
        raise HTTPBoundaryError(401, code="dashboard_denied")
    return actor


async def _dashboard_endpoint(request: Request) -> Response:
    runtime: CompanionRuntime = request.app.state.runtime
    operation = request.path_params.get("operation")
    if not isinstance(operation, str) or operation not in DASHBOARD_OPERATIONS:
        return _json_response(
            {"ok": False, "error": "request denied"},
            status=404,
            maximum=MAX_DASHBOARD_RESPONSE_BYTES,
        )
    try:
        if (
            request.method != "POST"
            or _has_query(request.scope)
            or not _is_json_content_type(request.scope)
        ):
            raise HTTPBoundaryError(400, code="json_post_required")
        body = await _read_body(
            request,
            maximum=MAX_DASHBOARD_BODY_BYTES,
            deadline_seconds=BODY_PARSE_DEADLINE_SECONDS,
        )
        _check_body_deadline(request.scope)
        payload = _parse_json_object(body, maximum=MAX_DASHBOARD_BODY_BYTES)
        actor = _verify_dashboard_request(request, body, operation, runtime)
        identity = await _resolve_dashboard_identity(runtime, actor)
        policy = await _recheck_dashboard_policy(
            runtime,
            actor=actor,
            identity=identity,
            operation=operation,
            arguments=payload,
        )
        if operation in DASHBOARD_MUTATION_OPERATIONS:
            _validate_dashboard_mutation_shape(operation, payload)
            await _authorize_dashboard_mutation(
                runtime,
                actor=actor,
                identity=identity,
                operation=operation,
                arguments=payload,
                policy=policy,
            )
        else:
            _validate_dashboard_read_shape(operation, payload)
            dashboard_user_id, dashboard_chat_id = _dashboard_rate_identity(
                runtime, identity
            )
            await _consume_rate_limit(
                runtime,
                "shared_read",
                user_id=dashboard_user_id,
                chat_id=dashboard_chat_id,
            )
        handler = runtime.dashboard_handlers.get(operation)
        if handler is None and runtime.operations is not None:
            handler = getattr(runtime.operations, operation.replace(".", "_"), None)
        result: object
        if operation in DASHBOARD_MUTATION_OPERATIONS:
            preview, mutation_arguments, confirmation = _dashboard_preview(
                operation,
                payload,
                identity=identity,
                policy=policy,
            )
            preview_digest = hashlib.sha256(
                preview.encode("utf-8", "strict")
            ).hexdigest()
            supplied_preview_digest = payload.get("preview_digest")
            if supplied_preview_digest is not None and not hmac.compare_digest(
                str(supplied_preview_digest), preview_digest
            ):
                raise HTTPBoundaryError(409, code="dashboard_denied")
            dashboard_user_id, dashboard_chat_id = _dashboard_rate_identity(
                runtime, identity
            )
            await _consume_rate_limit(
                runtime,
                "admin_execution" if confirmation is not None else "admin_preview",
                user_id=dashboard_user_id,
                chat_id=dashboard_chat_id,
            )
            if confirmation is None:
                # A mutation is always preview-only until a trusted, one-time
                # capability guard accepts the exact rendered bytes.  The
                # browser's signed request is not itself a confirmation.
                result = {
                    "confirmation_required": True,
                    "preview": preview,
                    "preview_digest": preview_digest,
                    "operation": operation,
                }
                issuer = runtime.dashboard_confirmation_issuer
                if callable(issuer):
                    capability = await _maybe_call(
                        issuer,
                        actor=actor,
                        identity=identity,
                        operation=operation,
                        arguments=mutation_arguments,
                        payload=mutation_arguments,
                        preview=preview,
                        preview_digest=preview_digest,
                        policy=policy,
                    )
                    capability_value, capability_expires = _dashboard_capability(
                        capability
                    )
                    result["confirmation_capability"] = capability_value
                    if capability_expires is not None:
                        result["expires_at"] = capability_expires
                elif runtime.production:
                    raise OperationDependencyError(
                        "dashboard confirmation issuer is unavailable"
                    )
            else:
                guard = runtime.dashboard_confirmation_guard
                if not callable(guard):
                    raise OperationDependencyError(
                        "dashboard confirmation guard is unavailable"
                    )
                confirmation_record = await _maybe_call(
                    guard,
                    actor=actor,
                    identity=identity,
                    operation=operation,
                    arguments=mutation_arguments,
                    payload=mutation_arguments,
                    preview=preview,
                    confirmation=confirmation,
                    preview_digest=preview_digest,
                )
                if (
                    confirmation_record is False
                    or confirmation_record is None
                    or isinstance(confirmation_record, bool)
                ):
                    raise HTTPBoundaryError(403, code="dashboard_denied")
                if handler is None:
                    raise OperationDependencyError("dashboard operation is unavailable")
                result = await _maybe_call(
                    handler,
                    payload=mutation_arguments,
                    arguments=mutation_arguments,
                    actor=actor,
                    identity=identity,
                    policy=policy,
                    confirmation=confirmation_record,
                )
        else:
            if handler is None:
                raise OperationDependencyError("dashboard operation is unavailable")
            result = await _maybe_call(
                handler,
                payload=payload,
                arguments=payload,
                actor=actor,
                identity=identity,
                policy=policy,
            )
        safe = safe_operation_result(result, tool=None)
        return _json_response(
            {"ok": True, "operation": operation, "data": safe},
            maximum=MAX_DASHBOARD_RESPONSE_BYTES,
        )
    except HTTPBoundaryError as exc:
        return _json_response(
            {"ok": False, "error": "request denied"},
            status=exc.status,
            maximum=MAX_DASHBOARD_RESPONSE_BYTES,
        )
    except Exception:
        return _json_response(
            {"ok": False, "error": "request denied"},
            status=403,
            maximum=MAX_DASHBOARD_RESPONSE_BYTES,
        )


async def _resolve_dashboard_identity(runtime: CompanionRuntime, actor: str) -> object:
    resolver = runtime.dashboard_identity_resolver
    if resolver is None:
        # Test runtimes may use the signed actor label as an opaque display
        # identity.  Production validation requires the typed resolver below.
        if runtime.production:
            raise OperationDependencyError("dashboard identity resolver is unavailable")
        return {"actor": actor, "role": "admin", "user_id": 1, "chat_id": 1}
    identity = await _maybe_call(resolver, actor=actor, dashboard_actor=actor)
    if identity is None or identity is False:
        raise HTTPBoundaryError(403, code="dashboard_denied")
    allowed = _dashboard_field(identity, "allowed")
    if allowed is not None and allowed is not True:
        raise HTTPBoundaryError(403, code="dashboard_denied")
    if runtime.production and allowed is not True:
        raise OperationDependencyError("dashboard identity is not current")
    role = _dashboard_field(identity, "role")
    if role is not None and role != "admin":
        raise HTTPBoundaryError(403, code="dashboard_denied")
    if runtime.production and role != "admin":
        raise HTTPBoundaryError(403, code="dashboard_denied")
    if runtime.production:
        if actor != DASHBOARD_SERVICE_ACTOR:
            raise HTTPBoundaryError(403, code="dashboard_denied")
        principal = _dashboard_field(identity, "actor")
        if principal is None:
            principal = _dashboard_field(identity, "principal")
        # A signed dashboard session is a service principal, not a Telegram
        # administrator session.  Requiring the marker here prevents a
        # resolver that returns only ``user_id``/``chat_id`` from silently
        # borrowing that user's authorization and rate-limit bucket.
        if principal != DASHBOARD_SERVICE_ACTOR:
            raise HTTPBoundaryError(403, code="dashboard_denied")
        for field in ("fingerprint", "version"):
            value = _dashboard_field(identity, field)
            if (
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > 512
            ):
                raise OperationDependencyError("dashboard identity is incomplete")
        # Numeric fields are accepted only as an optional typed adapter detail;
        # production authorization and rate limiting use the fixed service
        # principal above, never a browser-provided Telegram identity.
        for field in ("user_id", "chat_id"):
            value = _dashboard_field(identity, field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value == 0
            ):
                raise OperationDependencyError("dashboard identity is invalid")
    return identity


async def _recheck_dashboard_policy(
    runtime: CompanionRuntime,
    *,
    actor: str,
    identity: object,
    operation: str,
    arguments: Mapping[str, Any],
) -> object:
    checker = runtime.dashboard_policy_recheck
    if checker is None:
        if runtime.production:
            raise OperationDependencyError("dashboard policy recheck is unavailable")
        return identity
    result = await _maybe_call(
        checker,
        actor=actor,
        identity=identity,
        operation=operation,
        arguments=arguments,
        payload=arguments,
    )
    if result is False or result is None:
        raise HTTPBoundaryError(403, code="dashboard_denied")
    allowed = _dashboard_field(result, "allowed")
    if allowed is not None and allowed is not True:
        raise HTTPBoundaryError(403, code="dashboard_denied")
    role = _dashboard_field(result, "role")
    if role is not None and role != "admin":
        raise HTTPBoundaryError(403, code="dashboard_denied")
    if runtime.production and role != "admin":
        raise HTTPBoundaryError(403, code="dashboard_denied")
    if runtime.production:
        principal = _dashboard_field(result, "actor")
        if principal is None:
            principal = _dashboard_field(result, "principal")
        if principal != DASHBOARD_SERVICE_ACTOR:
            raise OperationDependencyError("dashboard policy principal is invalid")
        result_fingerprint = _dashboard_field(result, "fingerprint")
        result_version = _dashboard_field(result, "version")
        identity_fingerprint = _dashboard_field(identity, "fingerprint")
        identity_version = _dashboard_field(identity, "version")
        if (
            not isinstance(result_fingerprint, str)
            or not result_fingerprint
            or not isinstance(result_version, str)
            or not result_version
            or not isinstance(identity_fingerprint, str)
            or not isinstance(identity_version, str)
            or not hmac.compare_digest(result_fingerprint, identity_fingerprint)
            or result_version != identity_version
        ):
            raise OperationDependencyError("dashboard policy is stale")
    return result


async def _authorize_dashboard_mutation(
    runtime: CompanionRuntime,
    *,
    actor: str,
    identity: object,
    operation: str,
    arguments: Mapping[str, Any],
    policy: object,
) -> None:
    # The file fingerprint is a typed compare-and-swap field for allowlist
    # edits.  It is checked before the backend guard, which owns its atomic
    # candidate/admin-removal checks for both user and delivery mutations.
    fingerprint = arguments.get("fingerprint")
    current_fingerprint = _dashboard_field(policy, "fingerprint") or _dashboard_field(
        identity, "fingerprint"
    )
    if fingerprint is not None and current_fingerprint is not None:
        if not isinstance(fingerprint, str) or not hmac.compare_digest(
            fingerprint, str(current_fingerprint)
        ):
            raise HTTPBoundaryError(409, code="dashboard_denied")
    requested_version = arguments.get("version")
    current_version = _dashboard_field(policy, "version") or _dashboard_field(
        identity, "version"
    )
    if requested_version is not None and current_version is not None:
        if isinstance(requested_version, bool) or not isinstance(
            requested_version, int
        ):
            raise HTTPBoundaryError(409, code="dashboard_denied")
        if str(requested_version) != str(current_version):
            raise HTTPBoundaryError(409, code="dashboard_denied")
    guard = runtime.dashboard_mutation_guard
    if guard is None:
        if runtime.production:
            raise OperationDependencyError("dashboard CAS/admin guard is unavailable")
        return
    result = await _maybe_call(
        guard,
        actor=actor,
        identity=identity,
        operation=operation,
        arguments=arguments,
        payload=arguments,
        policy=policy,
    )
    if result is False or result is None:
        raise HTTPBoundaryError(409, code="dashboard_denied")
    allowed = _dashboard_field(result, "allowed")
    if allowed is not None and allowed is not True:
        raise HTTPBoundaryError(409, code="dashboard_denied")


def _dashboard_field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _dashboard_numeric_identity(identity: object, field: str) -> int:
    value = _dashboard_field(identity, field)
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise OperationDependencyError("dashboard identity is incomplete")
    return value


def _dashboard_rate_identity(
    runtime: CompanionRuntime, identity: object
) -> tuple[int, int]:
    """Return the limiter IDs for a typed dashboard principal.

    Telegram numeric IDs are used when an explicitly injected adapter has
    supplied them.  The production dashboard, however, is authenticated as
    the fixed ``dashboard-admin`` service principal; it must not borrow an
    arbitrary Telegram administrator's rate bucket or trust browser fields.
    """

    user_id = _dashboard_field(identity, "user_id")
    chat_id = _dashboard_field(identity, "chat_id")
    principal = _dashboard_field(identity, "actor")
    if principal is None:
        principal = _dashboard_field(identity, "principal")
    if principal == DASHBOARD_SERVICE_ACTOR:
        return DASHBOARD_RATE_USER_ID, DASHBOARD_RATE_CHAT_ID
    if runtime.production:
        raise OperationDependencyError(
            "dashboard service-principal scope is unavailable"
        )
    if (
        isinstance(user_id, int)
        and not isinstance(user_id, bool)
        and user_id > 0
        and isinstance(chat_id, int)
        and not isinstance(chat_id, bool)
        and chat_id != 0
    ):
        return user_id, chat_id
    raise OperationDependencyError("dashboard identity is incomplete")


def _dashboard_capability(value: object) -> tuple[str, int | None]:
    """Extract only the opaque capability fields from an issuer result."""

    capability: object
    if isinstance(value, str):
        capability = value
        expires = None
    else:
        capability = _dashboard_field(value, "confirmation_capability")
        if capability is None:
            capability = _dashboard_field(value, "capability")
        if capability is None:
            capability = _dashboard_field(value, "token")
        expires_value = _dashboard_field(value, "expires_at")
        expires = (
            None
            if expires_value is None
            else expires_value
            if isinstance(expires_value, int) and not isinstance(expires_value, bool)
            else None
        )
        if expires_value is not None and expires is None:
            raise OperationDependencyError(
                "dashboard confirmation capability is invalid"
            )
    if (
        not isinstance(capability, str)
        or not 16 <= len(capability.encode("utf-8", "strict")) <= 256
        or any(character.isspace() or ord(character) < 0x20 for character in capability)
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", capability)
    ):
        raise OperationDependencyError("dashboard confirmation capability is invalid")
    if expires is not None and expires <= int(time.time()):
        raise OperationDependencyError("dashboard confirmation capability is expired")
    return capability, expires


def _dashboard_preview(
    operation: str,
    payload: Mapping[str, Any],
    *,
    identity: object,
    policy: object,
) -> tuple[str, dict[str, Any], str | None]:
    mutation_arguments = dict(payload)
    has_confirmation = "confirmation" in mutation_arguments
    raw_confirmation = mutation_arguments.pop("confirmation", None)
    mutation_arguments.pop("preview_digest", None)
    if has_confirmation and (
        not isinstance(raw_confirmation, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", raw_confirmation)
        or raw_confirmation.casefold()
        in {"yes", "y", "confirm", "confirmed", "true", "1"}
    ):
        # A copied affirmative or arbitrary browser text is an authorization
        # failure, not a malformed transport.  Keeping this at 403 also makes
        # it impossible for a permissive handler to mistake the value for a
        # capability after shape validation.
        raise HTTPBoundaryError(403, code="dashboard_denied")
    fingerprint = _dashboard_field(policy, "fingerprint") or _dashboard_field(
        identity, "fingerprint"
    )
    version = (
        _dashboard_field(policy, "version")
        or _dashboard_field(identity, "version")
        or "1"
    )
    if not isinstance(version, str) or not version:
        raise OperationDependencyError("dashboard policy version is unavailable")
    state = hashlib.sha256(
        _json_dumps(
            {
                "operation": operation,
                "arguments": mutation_arguments,
                "fingerprint": fingerprint,
                "version": version,
            },
            maximum=MAX_DASHBOARD_BODY_BYTES,
        )
    ).hexdigest()
    preview = render_confirmation_preview(
        operation,
        cast(Mapping[str, Any], mutation_arguments),
        target_identity=_target_label(operation, mutation_arguments),
        state_fingerprint=state,
        policy_version=version,
    )
    return preview, mutation_arguments, raw_confirmation


def _validate_dashboard_mutation_shape(
    operation: str, payload: Mapping[str, Any]
) -> None:
    base = (
        {"user_id", "fingerprint", "idempotency_key"}
        if operation in {"users.add", "users.remove"}
        else {"delivery_id", "idempotency_key"}
    )
    optional = {"confirmation", "preview_digest"}
    if operation in {"users.add", "users.remove"}:
        optional |= {"version", "state_fingerprint"}
    payload_keys = set(payload)
    if payload_keys - base - optional:
        raise HTTPBoundaryError(400, code="dashboard_denied")
    if not base.issubset(payload_keys):
        raise HTTPBoundaryError(400, code="dashboard_denied")
    if ("confirmation" in payload_keys) != ("preview_digest" in payload_keys):
        raise HTTPBoundaryError(400, code="dashboard_denied")
    if operation in {"users.add", "users.remove"}:
        user_id = payload.get("user_id")
        if (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or not 0 < user_id <= (1 << 53) - 1
        ):
            raise HTTPBoundaryError(400, code="dashboard_denied")
        fingerprint = payload.get("fingerprint")
        if (
            not isinstance(fingerprint, str)
            or not fingerprint
            or len(fingerprint.encode("utf-8")) > 256
        ):
            raise HTTPBoundaryError(400, code="dashboard_denied")
        version = payload.get("version")
        if version is not None and (
            isinstance(version, bool)
            or not isinstance(version, int)
            or not 0 <= version <= (1 << 53) - 1
        ):
            raise HTTPBoundaryError(400, code="dashboard_denied")
        state_fingerprint = payload.get("state_fingerprint")
        if state_fingerprint is not None and (
            not isinstance(state_fingerprint, str)
            or not state_fingerprint
            or len(state_fingerprint.encode("utf-8")) > 256
            or any(
                character.isspace() or ord(character) < 0x20
                for character in state_fingerprint
            )
        ):
            raise HTTPBoundaryError(400, code="dashboard_denied")
    else:
        delivery_id = payload.get("delivery_id")
        if (
            isinstance(delivery_id, bool)
            or not isinstance(delivery_id, int)
            or not 0 < delivery_id <= (1 << 53) - 1
        ):
            raise HTTPBoundaryError(400, code="dashboard_denied")
    key = payload.get("idempotency_key")
    if (
        not isinstance(key, str)
        or not key
        or len(key.encode("utf-8")) > 512
        or any(character.isspace() or ord(character) < 0x20 for character in key)
    ):
        raise HTTPBoundaryError(400, code="dashboard_denied")
    preview_digest = payload.get("preview_digest")
    if preview_digest is not None and not _DASHBOARD_SIG_RE.fullmatch(
        str(preview_digest)
    ):
        raise HTTPBoundaryError(400, code="dashboard_denied")
    confirmation = payload.get("confirmation")
    if confirmation is not None and (
        not isinstance(confirmation, str)
        or not confirmation
        or len(confirmation.encode("utf-8")) > 256
        or any(
            character.isspace() or ord(character) < 0x20 for character in confirmation
        )
    ):
        raise HTTPBoundaryError(400, code="dashboard_denied")


def _validate_dashboard_read_shape(operation: str, payload: Mapping[str, Any]) -> None:
    if operation in {"health", "oracle"}:
        if payload:
            raise HTTPBoundaryError(400, code="dashboard_denied")
        return
    if operation == "users.resolve":
        allowed = {"user_id", "fingerprint", "version"}
        if set(payload) - allowed:
            raise HTTPBoundaryError(400, code="dashboard_denied")
        user_id = payload.get("user_id")
        if (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or not 0 < user_id <= (1 << 53) - 1
        ):
            raise HTTPBoundaryError(400, code="dashboard_denied")
        fingerprint = payload.get("fingerprint")
        if fingerprint is not None and (
            not isinstance(fingerprint, str)
            or not fingerprint
            or len(fingerprint.encode("utf-8")) > 256
            or any(
                character.isspace() or ord(character) < 0x20
                for character in fingerprint
            )
        ):
            raise HTTPBoundaryError(400, code="dashboard_denied")
        version = payload.get("version")
        if version is not None and (
            isinstance(version, bool)
            or not isinstance(version, int)
            or not 0 <= version <= (1 << 53) - 1
        ):
            raise HTTPBoundaryError(400, code="dashboard_denied")
        return
    allowed = {"limit", "cursor"}
    if operation == "deliveries":
        allowed.add("status")
    if set(payload) - allowed:
        raise HTTPBoundaryError(400, code="dashboard_denied")
    limit = payload.get("limit")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 250
    ):
        raise HTTPBoundaryError(400, code="dashboard_denied")
    cursor = payload.get("cursor")
    if cursor is not None and (
        not isinstance(cursor, str)
        or not 1 <= len(cursor) <= 4096
        or any(character.isspace() or ord(character) < 0x20 for character in cursor)
    ):
        raise HTTPBoundaryError(400, code="dashboard_denied")
    status = payload.get("status")
    if status is not None and (
        not isinstance(status, str)
        or not 1 <= len(status) <= 64
        or any(character.isspace() or ord(character) < 0x20 for character in status)
    ):
        raise HTTPBoundaryError(400, code="dashboard_denied")


def _target_label(operation: str, payload: Mapping[str, Any]) -> str:
    for key in ("user_id", "delivery_id", "record_id", "id"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return f"{operation}:{key}:{value}"
    return f"{operation}:request:{hashlib.sha256(canonical_argument_hash(payload).encode()).hexdigest()}"


async def _plex_endpoint(request: Request) -> Response:
    runtime: CompanionRuntime = request.app.state.runtime
    capability = request.path_params.get("capability")
    if (
        not isinstance(capability, str)
        or _has_query(request.scope)
        or not _PLEX_CAPABILITY_RE.fullmatch(capability)
        or not _is_loopback(request, runtime)
    ):
        return _json_response({"error": "request denied"}, status=404, maximum=1024)
    # Reject a well-shaped but wrong capability before charging the durable
    # limiter or consuming the request body.  This is a constant-time compare
    # and intentionally remains separate from the parser's defense-in-depth
    # validation below.  A path capability is not a substitute for the
    # network-source boundary checked above.
    capability_value = runtime.plex_capability
    if not isinstance(capability_value, (str, bytes)):
        return _json_response({"error": "request denied"}, status=404, maximum=1024)
    try:
        validate_capability(
            PLEX_PREFIX.rstrip("/") + "/" + capability, capability_value
        )
    except WebhookCapabilityError:
        return _json_response({"error": "request denied"}, status=404, maximum=1024)
    try:
        limiter = getattr(runtime, "plex_rate_limiter", None)
        allow = getattr(limiter, "allow", None) if limiter is not None else None
        if not callable(allow):
            allow = getattr(limiter, "consume", None) if limiter is not None else None
        if not callable(allow):
            raise WebhookPersistenceError("rate_limiter_unavailable")
        allowed = allow()
        if inspect.isawaitable(allowed):
            allowed = await cast(Any, allowed)
        if allowed is False:
            return _json_response(
                {"error": "temporarily unavailable"}, status=429, maximum=1024
            )
        body = await _read_body(
            request,
            maximum=MAX_PLEX_BODY_BYTES,
            deadline_seconds=BODY_PARSE_DEADLINE_SECONDS,
        )
        deadline_at = cast(Mapping[str, object], request.scope).get(
            "_companion_deadline_at"
        )
        if not isinstance(deadline_at, (int, float)):
            raise WebhookLimitError("parse_deadline_exceeded")
        remaining = float(deadline_at) - time.monotonic()
        if remaining <= 0:
            raise WebhookLimitError("parse_deadline_exceeded")
        content_type = _single_raw_header(request.scope, "content-type") or ""
        content_encoding = _single_raw_header(
            request.scope, "content-encoding", required=False
        )
        # MIME parsing is CPU-bound and email's parser is synchronous.  Keep
        # it off the ASGI event loop and bound the await with the same
        # absolute deadline used for body buffering.  The parser also checks
        # its own deadline; if a cancelled thread is still unwinding it can
        # never retain an unbounded body because the ingress byte cap was
        # enforced above.
        import asyncio

        parse_kwargs = {
            "request_path": PLEX_PREFIX.rstrip("/") + "/" + capability,
            "capability": cast(Any, capability_value),
            "expected_server_uuid": runtime.expected_server_uuid,
            "allowed_server_uuids": runtime.allowed_server_uuids,
            "allowed_library_ids": runtime.allowed_library_ids,
            "allowed_library_names": runtime.allowed_library_names,
            "content_encoding": content_encoding,
            "deadline_seconds": min(BODY_PARSE_DEADLINE_SECONDS, remaining),
        }
        parse_task = asyncio.to_thread(
            parse_plex_webhook,
            body,
            content_type,
            **parse_kwargs,
        )
        try:
            event = await asyncio.wait_for(parse_task, timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise WebhookLimitError("parse_deadline_exceeded") from exc
        if event is None:
            return (
                Response(status_code=204)
                if Response is not object
                else cast(Response, _FallbackResponse(b"", 204, ""))
            )
        if not runtime.persist_event(event):
            raise WebhookPersistenceError("persistence_failed")
        return (
            Response(status_code=202)
            if Response is not object
            else cast(Response, _FallbackResponse(b"", 202, ""))
        )
    except WebhookCapabilityError:
        return _json_response({"error": "request denied"}, status=404, maximum=1024)
    except WebhookPersistenceError:
        return _json_response(
            {"error": "temporarily unavailable"}, status=503, maximum=1024
        )
    except (
        WebhookLimitError,
        WebhookContentTypeError,
        WebhookValidationError,
        PlexIngressError,
        HTTPBoundaryError,
    ):
        return _json_response({"error": "request denied"}, status=400, maximum=1024)
    except Exception:
        return _json_response(
            {"error": "temporarily unavailable"}, status=503, maximum=1024
        )


async def _health_endpoint(request: Request) -> Response:
    del request
    return _json_response({"status": "ok"}, maximum=1024)


async def _ready_endpoint(request: Request) -> Response:
    runtime: CompanionRuntime = request.app.state.runtime
    ready = runtime.ready()
    return _json_response(
        {"status": "ready" if ready else "not_ready", "ready": ready},
        status=200 if ready else 503,
        maximum=1024,
    )


class StartupConfigurationError(RuntimeError):
    """A production dependency or secret reference is not configured."""


class _PolicyHelperClient:
    """Small production client for Hermes's fixed policy-helper routes.

    This is intentionally not a generic HTTP proxy.  The seven fixed typed
    policy operations mirror Hermes's helper contract: current membership,
    current users, blocked contacts, selected identity resolution, regular
    allowlist mutation, and runtime status.  No caller can supply an
    arbitrary helper path or method.
    """

    _ROUTES: Final[Mapping[str, str]] = {
        # Route names intentionally mirror the native Hermes client.  The
        # shorter aliases are private implementation spellings retained for
        # callers that were written against the first companion boundary;
        # every value still resolves to this fixed, reviewed route set.
        "membership": "/v1/policy/membership",
        "current_users": "/v1/policy/current-users",
        "notify_admin": "/v1/policy/notify-admin",
        "blocked_contacts": "/v1/policy/blocked-contacts",
        "resolve_identity": "/v1/policy/resolve-identity",
        "allowlist_mutate": "/v1/policy/allowlist/mutate",
        "runtime_status": "/v1/policy/status",
        "blocked": "/v1/policy/blocked-contacts",
        "resolve": "/v1/policy/resolve-identity",
        "mutate": "/v1/policy/allowlist/mutate",
        "status": "/v1/policy/status",
    }

    def __init__(
        self, base_url: str, key: bytes, *, timeout: tuple[float, float]
    ) -> None:
        try:
            from .config import normalize_url

            normalized = normalize_url(base_url, field_name="policy_helper_url")
        except Exception as exc:  # noqa: BLE001
            raise StartupConfigurationError(
                "policy helper configuration is invalid"
            ) from exc
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        if hostname is None or any(
            ord(character) < 0x20 or character.isspace() for character in hostname
        ):
            raise StartupConfigurationError("policy helper configuration is invalid")
        # The helper is a private Hermes sidecar.  Hostnames used by the
        # Compose network are accepted explicitly; public IPs are not.
        import ipaddress

        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if hostname not in {"localhost", "hermes-media", "media-hermes"} and (
            address is None or not (address.is_private or address.is_loopback)
        ):
            raise StartupConfigurationError("policy helper must use a private origin")
        if not isinstance(key, bytes) or not key or len(key) > 16 * 1024:
            raise StartupConfigurationError("policy helper key is invalid")
        self.base_url = normalized
        self.key = key
        self.timeout = timeout

    def _request(
        self, operation: str, payload: Mapping[str, object]
    ) -> dict[str, object]:
        route = self._ROUTES.get(operation)
        if route is None:
            raise OperationDependencyError("policy helper operation is unavailable")
        try:
            import requests

            key_text = self.key.decode("utf-8", "strict")
            response = requests.post(
                self.base_url + route,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-CRBL-Policy-Key": key_text,
                },
                data=_json_dumps(payload, maximum=16 * 1024),
                timeout=self.timeout,
                allow_redirects=False,
            )
            if (
                response.is_redirect
                or response.is_permanent_redirect
                or not 200 <= response.status_code < 300
            ):
                raise OperationDependencyError("policy helper denied the request")
            raw = response.content
            if not isinstance(raw, bytes) or len(raw) > 256 * 1024:
                raise OperationDependencyError("policy helper response is unavailable")
            parsed = parse_json(raw)
        except OperationDependencyError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OperationDependencyError("policy helper is unavailable") from exc
        if not isinstance(parsed, Mapping):
            raise OperationDependencyError("policy helper response is invalid")
        data = parsed.get("data", parsed)
        if parsed.get("ok", True) is False or not isinstance(data, Mapping):
            raise OperationDependencyError("policy helper response is invalid")
        return cast(dict[str, object], dict(data))

    @staticmethod
    def _id(value: object, *, allow_negative: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value == 0:
            raise OperationDependencyError("policy helper identity is invalid")
        if not allow_negative and value < 0:
            raise OperationDependencyError("policy helper identity is invalid")
        return value

    @staticmethod
    def _text(value: object, *, field: str, maximum: int = 256) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.encode("utf-8", "strict")) > maximum
        ):
            raise OperationDependencyError(f"policy helper {field} is invalid")
        return value.strip()

    @classmethod
    def _membership_result(
        cls,
        value: Mapping[str, object],
        *,
        user_id: int,
        chat_id: int,
    ) -> dict[str, object]:
        allowed = value.get("allowed")
        role = value.get("role")
        if (
            not isinstance(allowed, bool)
            or not isinstance(role, str)
            or role not in {"user", "admin", "unknown"}
        ):
            raise OperationDependencyError("policy helper membership is invalid")
        result: dict[str, object] = {
            "user_id": cls._id(value.get("user_id", user_id)),
            "chat_id": cls._id(value.get("chat_id", chat_id), allow_negative=True),
            "allowed": allowed,
            "role": role,
            "fingerprint": cls._text(
                value.get("fingerprint"), field="fingerprint", maximum=128
            ),
        }
        if result["user_id"] != user_id or result["chat_id"] != chat_id:
            raise OperationDependencyError("policy helper identity is stale")
        version = value.get("version")
        if version is None:
            raise OperationDependencyError(
                "policy helper membership version is missing"
            )
        result["version"] = cls._text(version, field="version", maximum=128)
        return result

    def membership(self, *, user_id: int, chat_id: int) -> Mapping[str, object]:
        checked_user_id = self._id(user_id)
        checked_chat_id = self._id(chat_id, allow_negative=True)
        return self._membership_result(
            self._request(
                "membership", {"user_id": checked_user_id, "chat_id": checked_chat_id}
            ),
            user_id=checked_user_id,
            chat_id=checked_chat_id,
        )

    def current_users(self) -> Mapping[str, object]:
        """Return only numeric allowlist IDs, roles, fingerprint, and version."""

        value = self._request("current_users", {})
        rows = value.get("users", value.get("items", []))
        if not isinstance(rows, list) or len(rows) > MAX_POLICY_USERS:
            raise OperationDependencyError("policy helper current users are invalid")
        users: list[dict[str, object]] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise OperationDependencyError("policy helper current user is invalid")
            user_id = self._id(row.get("user_id"))
            role = row.get("role")
            if (
                not isinstance(role, str)
                or role not in {"user", "admin"}
                or user_id in seen
            ):
                raise OperationDependencyError("policy helper current user is invalid")
            seen.add(user_id)
            users.append({"user_id": user_id, "role": role})
        return {
            "users": tuple(users),
            "fingerprint": self._text(
                value.get("fingerprint"), field="fingerprint", maximum=128
            ),
            "version": self._text(value.get("version"), field="version", maximum=128),
        }

    # Keep the names used by the Hermes extension and dashboard adapters.
    get_current_users = current_users

    def notify_admin(
        self, *, chat_id: int, text: str, parse_mode: str = ""
    ) -> Mapping[str, object]:
        """Send one bounded admin notification through Hermes' native bot."""

        checked_chat_id = self._id(chat_id, allow_negative=True)
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text.encode("utf-8", "strict")) > 4096
            or any(
                ord(character) < 0x20 and character not in {"\n", "\t"}
                for character in text
            )
        ):
            raise OperationDependencyError("policy helper notification text is invalid")
        if parse_mode not in {"", "HTML"}:
            raise OperationDependencyError(
                "policy helper notification parse mode is invalid"
            )
        value = self._request(
            "notify_admin",
            {"chat_id": checked_chat_id, "text": text, "parse_mode": parse_mode},
        )
        status = value.get("status")
        if status not in {
            "sent",
            "retryable-pretransmission",
            "retryable",
            "ambiguous",
            "permanent",
        }:
            raise OperationDependencyError("policy helper notification was not sent")
        message_id = value.get("message_id")
        if message_id is not None:
            message_id = self._id(message_id)
        elif status == "sent":
            raise OperationDependencyError("policy helper notification was not sent")
        retry_after = value.get("retry_after")
        if retry_after is not None:
            if (
                isinstance(retry_after, bool)
                or not isinstance(retry_after, int)
                or not 0 <= retry_after <= 86_400
            ):
                raise OperationDependencyError(
                    "policy helper notification retry interval is invalid"
                )
        transmitted = value.get("transmitted")
        if transmitted is not None and not isinstance(transmitted, bool):
            raise OperationDependencyError(
                "policy helper notification transmission flag is invalid"
            )
        result: dict[str, object] = {
            "chat_id": self._id(
                value.get("chat_id", checked_chat_id), allow_negative=True
            ),
            "status": status,
        }
        if message_id is not None:
            result["message_id"] = message_id
        if retry_after is not None:
            result["retry_after"] = retry_after
        if transmitted is not None:
            result["transmitted"] = transmitted
        return result

    send_notification = notify_admin
    send_admin_notification = notify_admin
    notify_admins = notify_admin

    def blocked(self, *, limit: int = 50) -> tuple[Mapping[str, object], ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 < limit <= MAX_BLOCKED_CONTACTS
        ):
            raise OperationDependencyError(
                "policy helper blocked-contact limit is invalid"
            )
        value = self._request("blocked_contacts", {"limit": limit})
        rows = value.get("contacts", value.get("items", []))
        if not isinstance(rows, list) or len(rows) > MAX_BLOCKED_CONTACTS:
            raise OperationDependencyError("policy helper blocked contacts are invalid")
        contacts: list[Mapping[str, object]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise OperationDependencyError(
                    "policy helper blocked contact is invalid"
                )
            contact: dict[str, object] = {
                "user_id": self._id(row.get("user_id")),
                "chat_id": self._id(row.get("chat_id"), allow_negative=True),
                "observed_at": self._text(row.get("observed_at"), field="observed_at"),
            }
            source = row.get("source")
            if source is not None:
                contact["source"] = self._text(source, field="source")
            contacts.append(contact)
        return tuple(contacts)

    blocked_contacts = blocked
    get_blocked_contacts = blocked

    def resolve(self, *, user_id: int) -> Mapping[str, object]:
        checked_user_id = self._id(user_id)
        value = self._request("resolve_identity", {"user_id": checked_user_id})
        resolved_user_id = self._id(value.get("user_id", checked_user_id))
        resolved_chat_id = self._id(
            value.get("chat_id", checked_user_id), allow_negative=True
        )
        if resolved_user_id != checked_user_id or resolved_chat_id != checked_user_id:
            raise OperationDependencyError(
                "policy helper identity resolution changed the selected ID"
            )
        result: dict[str, object] = {
            "user_id": resolved_user_id,
            "chat_id": resolved_chat_id,
        }
        for field in ("display_name", "username"):
            item = value.get(field)
            if item is not None:
                result[field] = self._text(item, field=field)
        chat_type = value.get("chat_type", "private")
        if not isinstance(chat_type, str) or chat_type not in {
            "private",
            "group",
            "supergroup",
            "channel",
        }:
            raise OperationDependencyError("policy helper chat type is invalid")
        result["chat_type"] = chat_type
        return result

    resolve_identity = resolve
    resolve_user = resolve

    def mutate(
        self, *, operation: str, user_id: int, expected_fingerprint: str
    ) -> Mapping[str, object]:
        if operation not in {"add", "remove"}:
            raise OperationDependencyError(
                "policy helper allowlist operation is invalid"
            )
        checked_user_id = self._id(user_id)
        fingerprint = self._text(expected_fingerprint, field="fingerprint", maximum=128)
        value = self._request(
            "allowlist_mutate",
            {
                "operation": operation,
                "user_id": checked_user_id,
                "expected_fingerprint": fingerprint,
            },
        )
        result = self._membership_result(
            value, user_id=checked_user_id, chat_id=checked_user_id
        )
        returned_version = value.get("version")
        if returned_version is None:
            raise OperationDependencyError("policy helper mutation version is missing")
        result["version"] = self._text(returned_version, field="version", maximum=128)
        returned_operation = value.get("operation")
        if returned_operation != operation:
            raise OperationDependencyError("policy helper mutation operation is stale")
        result["operation"] = operation
        changed = value.get("changed")
        if not isinstance(changed, bool):
            raise OperationDependencyError("policy helper mutation result is invalid")
        result["changed"] = changed
        status = value.get("status")
        result["status"] = self._text(status, field="status", maximum=64)
        return result

    mutate_allowlist = mutate

    def add_user(
        self, *, user_id: int, expected_fingerprint: str
    ) -> Mapping[str, object]:
        return self.mutate(
            operation="add", user_id=user_id, expected_fingerprint=expected_fingerprint
        )

    def remove_user(
        self, *, user_id: int, expected_fingerprint: str
    ) -> Mapping[str, object]:
        return self.mutate(
            operation="remove",
            user_id=user_id,
            expected_fingerprint=expected_fingerprint,
        )

    def authorize(
        self, *, user_id: int, chat_id: int, require_admin: bool = False
    ) -> Mapping[str, object]:
        result = self.membership(user_id=user_id, chat_id=chat_id)
        if result.get("allowed") is not True or (
            require_admin and result.get("role") != "admin"
        ):
            raise OperationBoundaryError("actor is not allowed")
        return result

    def runtime_status(self) -> Mapping[str, object]:
        result = self._request("runtime_status", {})
        ready = result.get("ready")
        if not isinstance(ready, bool):
            raise OperationDependencyError("policy helper readiness is invalid")
        typed: dict[str, object] = {"ready": ready}
        for field, maximum in (("fingerprint", 128), ("version", 128), ("source", 64)):
            value = result.get(field)
            if field == "source" and value is None:
                continue
            typed[field] = self._text(value, field=field, maximum=maximum)
        return typed

    status = runtime_status

    def ready(self) -> bool:
        return self.runtime_status().get("ready") is True


def _read_secret_reference(
    reference: object, *, selectors: Sequence[str] = ()
) -> bytes:
    """Read one canonical mounted secret without retaining paths in errors."""

    raw_path = getattr(reference, "path", reference)
    if not isinstance(raw_path, (str, os.PathLike)):
        raise StartupConfigurationError("secret reference is unavailable")
    try:
        path = Path(raw_path)
        stat_result = path.stat()
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or stat_result.st_mode & 0o077
        ):
            raise StartupConfigurationError("secret reference is unavailable")
        raw = path.read_bytes()
    except StartupConfigurationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StartupConfigurationError("secret reference is unavailable") from exc
    if not raw or len(raw) > 16 * 1024:
        raise StartupConfigurationError("secret reference is invalid")
    raw = raw.rstrip(b"\r\n")
    if not selectors:
        if not raw:
            raise StartupConfigurationError("secret reference is empty")
        return raw
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise StartupConfigurationError("secret reference is invalid") from exc
    wanted = set(selectors)
    selected: str | None = None
    plain: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            plain.append(stripped)
            continue
        name, value = stripped.split("=", 1)
        if name.strip() in wanted:
            if selected is not None:
                raise StartupConfigurationError(
                    "secret reference contains a duplicate selector"
                )
            selected = value.strip().strip("\"'")
    if selected is None and len(plain) == 1:
        selected = plain[0]
    if selected is None or not selected:
        raise StartupConfigurationError(
            "secret reference is missing its selected value"
        )
    return selected.encode("utf-8", "strict")


_PROVIDER_URL_FIELDS: Final[Mapping[str, str]] = {
    "PLEX_URL": "plex_url",
    "RADARR_URL": "radarr_url",
    "SONARR_URL": "sonarr_url",
    "TMDB_URL": "tmdb_url",
}


def _read_provider_url_overrides(reference: object) -> dict[str, str]:
    """Read only canonical, non-secret URL keys from one mounted dotenv.

    The upstream file is shared with the upstream MCP and therefore contains
    credentials as well as URLs.  This parser deliberately ignores every key
    outside the four reviewed URL names and never returns a credential.  A
    repeated approved key is rejected rather than silently selecting one.
    """

    raw_path = getattr(reference, "path", reference)
    if not isinstance(raw_path, (str, os.PathLike)):
        raise StartupConfigurationError("provider configuration is unavailable")
    try:
        path = Path(raw_path)
        stat_result = path.stat()
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or stat_result.st_mode & 0o077
        ):
            raise StartupConfigurationError("provider configuration is unavailable")
        raw = path.read_bytes()
    except StartupConfigurationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StartupConfigurationError(
            "provider configuration is unavailable"
        ) from exc
    if not raw or len(raw) > 64 * 1024:
        raise StartupConfigurationError("provider configuration is invalid")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise StartupConfigurationError("provider configuration is invalid") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            # Non-assignment lines are not needed by the companion.  Rejecting
            # them would make a shared credential file unnecessarily brittle.
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        field = _PROVIDER_URL_FIELDS.get(key)
        if field is None:
            continue
        if field in values:
            raise StartupConfigurationError(
                "provider configuration contains a duplicate URL"
            )
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not value:
            raise StartupConfigurationError(
                "provider configuration contains an empty URL"
            )
        try:
            from .config import normalize_url

            values[field] = normalize_url(value, field_name=field)
        except Exception as exc:  # noqa: BLE001
            raise StartupConfigurationError(
                "provider configuration contains an invalid URL"
            ) from exc
    return values


def _apply_provider_url_overrides(config: object, values: Mapping[str, str]) -> object:
    reference = _env_text(
        values, "MEDIA_COMPANION_PROVIDER_ENV_FILE", "COMPANION_PROVIDER_ENV_FILE"
    )
    if reference is None:
        return config
    overrides = _read_provider_url_overrides(reference)
    if not overrides:
        raise StartupConfigurationError("provider configuration has no approved URLs")
    try:
        changes: dict[str, object] = {}
        for field, value in overrides.items():
            current = getattr(config, field, None)
            # ``load_config`` supplies the public TMDB API origin when its
            # credential selector is present.  That default is a fallback,
            # not an operator-selected URL, so the canonical upstream.env
            # value may replace it without creating an ambiguous duplicate.
            if field == "tmdb_url" and current == "https://api.themoviedb.org/3":
                current = None
            if current not in {None, "", value}:
                raise StartupConfigurationError(
                    "provider URL configuration is ambiguous"
                )
            changes[field] = value
        return replace(cast(Any, config), **changes)
    except StartupConfigurationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StartupConfigurationError(
            "provider URL configuration is invalid"
        ) from exc


def _env_text(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = values.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _env_port(values: Mapping[str, str], default: int, *names: str) -> int:
    value = _env_text(values, *names)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise StartupConfigurationError("HTTP port configuration is invalid") from exc
    if not 1 <= parsed <= 65_535:
        raise StartupConfigurationError("HTTP port configuration is invalid")
    return parsed


def _env_trusted_ingress_peers(values: Mapping[str, str]) -> tuple[str, ...]:
    """Parse the explicit Docker/proxy peer allowlist for Plex ingress.

    A capability in the URL authenticates the webhook path; it does not
    authenticate the network source.  Production therefore needs an exact
    peer list for the host-loopback DNAT gateway, or a custom checker supplied
    by the runtime factory.  IP literals are required so DNS cannot turn the
    trust boundary into a moving target.
    """

    raw = _env_text(
        values,
        "MEDIA_COMPANION_PLEX_TRUSTED_PEERS",
        "MEDIA_COMPANION_TRUSTED_INGRESS_PEERS",
    )
    if raw is None:
        return ()
    import ipaddress

    peers: list[str] = []
    for token in raw.split(","):
        peer = token.strip()
        if not peer:
            raise StartupConfigurationError(
                "Plex ingress peer configuration is invalid"
            )
        try:
            canonical = str(ipaddress.ip_address(peer))
        except ValueError as exc:
            raise StartupConfigurationError(
                "Plex ingress peer configuration is invalid"
            ) from exc
        if canonical not in peers:
            peers.append(canonical)
    if not peers or len(peers) > 16:
        raise StartupConfigurationError("Plex ingress peer configuration is invalid")
    return tuple(peers)


def _env_identifier_list(values: Mapping[str, str], *names: str) -> tuple[str, ...]:
    """Read a bounded comma-separated allowlist without accepting globs."""

    raw = _env_text(values, *names)
    if raw is None:
        return ()
    result: list[str] = []
    for token in raw.split(","):
        value = token.strip()
        if (
            not value
            or len(value) > 256
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value)
        ):
            raise StartupConfigurationError(
                "identifier allowlist configuration is invalid"
            )
        if value not in result:
            result.append(value)
    if not result or len(result) > 64:
        raise StartupConfigurationError("identifier allowlist configuration is invalid")
    return tuple(result)


def _load_runtime_factory(values: Mapping[str, str]) -> Callable[..., object]:
    reference = _env_text(
        values, "MEDIA_COMPANION_RUNTIME_FACTORY", "COMPANION_RUNTIME_FACTORY"
    )
    if reference is None:
        # The checked-in production composition is the only implicit factory
        # permitted.  It is imported by a fixed module path (never an
        # environment-selected test double); if that module is absent or
        # incomplete startup still fails closed.
        reference = "media_companion.production:build_runtime"
    if reference.count(":") != 1:
        raise StartupConfigurationError("production runtime factory is not configured")
    module_name, attribute = reference.split(":", 1)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module_name) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", attribute
    ):
        raise StartupConfigurationError("production runtime factory is invalid")
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
    except Exception as exc:  # noqa: BLE001
        raise StartupConfigurationError(
            "production runtime factory is unavailable"
        ) from exc
    if not callable(factory):
        raise StartupConfigurationError("production runtime factory is unavailable")
    return cast(Callable[..., object], factory)


def _call_runtime_factory(
    factory: Callable[..., object], dependencies: Mapping[str, object]
) -> object:
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory(**dependencies)
    parameters = signature.parameters
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return factory(**dependencies)
    keyword: dict[str, object] = {}
    for name, parameter in parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        if name in dependencies:
            keyword[name] = dependencies[name]
        elif parameter.default is inspect.Parameter.empty:
            raise StartupConfigurationError("production runtime factory is incomplete")
    return factory(**keyword)


def build_production_runtime(
    *,
    runtime_factory: Callable[..., object] | None = None,
    env: Mapping[str, str] | None = None,
    config: object | None = None,
) -> CompanionRuntime:
    """Build the live runtime from validated, durable dependencies only.

    Workflow handlers, confirmation delivery/execution, dashboard views, and
    the singleton worker are supplied by the checked-in
    ``media_companion.production:build_runtime`` composition (or by an
    explicitly configured factory for a reviewed deployment variant).  This
    module provides the shared durable substrate and refuses to invent test
    fakes when that composition is absent or incomplete.
    """

    values: Mapping[str, str] = os.environ if env is None else env
    if config is None:
        try:
            from .config import load_config

            config = load_config(values)
        except Exception as exc:  # noqa: BLE001
            raise StartupConfigurationError(
                "companion configuration is invalid"
            ) from exc
    config = _apply_provider_url_overrides(config, values)
    try:
        from .db import Database
        from .clients.upstream_mcp import UpstreamMCPClient
        from .operations import SQLiteRateLimiter

        database = Database(getattr(config, "database_path"))
        database.migrate()
        rate_limiter = SQLiteRateLimiter(database)
        nonce_store = database.nonce_replay_store()
        confirmation_store = database.confirmation_store()
        actor_ref = getattr(config, "actor_signing_key_file", None)
        dashboard_ref = getattr(config, "dashboard_api_key_file", None)
        capability_ref = getattr(config, "plex_webhook_capability_file", None)
        if actor_ref is None or dashboard_ref is None or capability_ref is None:
            raise StartupConfigurationError(
                "required production secret references are missing"
            )
        actor_key = _read_secret_reference(actor_ref)
        dashboard_key = _read_secret_reference(dashboard_ref)
        capability = _read_secret_reference(
            capability_ref,
            selectors=(
                "PLEX_WEBHOOK_CAPABILITY",
                "PLEX_WEBHOOK_SECRET",
                "PLEX_CAPABILITY",
            ),
        ).decode("utf-8", "strict")
        verifier = ActorAssertionVerifier(actor_key, nonce_store=nonce_store)
        upstream = UpstreamMCPClient(
            getattr(config, "upstream_url"),
            token_file=getattr(config, "upstream_token_file", None),
            connect_timeout=getattr(getattr(config, "timeouts"), "connect_seconds"),
            total_timeout=getattr(getattr(config, "timeouts"), "total_seconds"),
        )
        helper_url = _env_text(values, "CRBL_POLICY_HELPER_URL", "POLICY_HELPER_URL")
        helper_key_ref = _env_text(
            values, "CRBL_POLICY_HELPER_KEY_FILE", "POLICY_HELPER_KEY_FILE"
        )
        if helper_url is None or helper_key_ref is None:
            raise StartupConfigurationError("policy helper is not configured")
        policy = _PolicyHelperClient(
            helper_url,
            _read_secret_reference(helper_key_ref),
            timeout=(
                getattr(getattr(config, "timeouts"), "connect_seconds"),
                getattr(getattr(config, "timeouts"), "total_seconds"),
            ),
        )
    except StartupConfigurationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StartupConfigurationError(
            "production dependencies could not be constructed"
        ) from exc

    factory = runtime_factory or _load_runtime_factory(values)
    dependencies: dict[str, object] = {
        "config": config,
        "database": database,
        "rate_limiter": rate_limiter,
        "nonce_store": nonce_store,
        "confirmation_store": confirmation_store,
        "actor_verifier": verifier,
        "verifier": verifier,
        "policy": policy,
        "policy_helper": policy,
        "upstream": upstream,
        "upstream_broker": upstream,
        "dashboard_api_key": dashboard_key,
        "helper_key": actor_key,
        "confirmation_key": actor_key,
        "plex_capability": capability,
        "plex_webhook_capability": capability,
        "expected_server_uuid": getattr(config, "plex_server_uuid", None),
        "allowed_server_uuids": _env_identifier_list(
            values,
            "MEDIA_COMPANION_PLEX_SERVER_UUIDS",
            "MEDIA_COMPANION_PLEX_ALLOWED_SERVER_UUIDS",
        )
        or tuple(
            value for value in (getattr(config, "plex_server_uuid", None),) if value
        ),
        "allowed_library_ids": _env_identifier_list(
            values,
            "MEDIA_COMPANION_PLEX_LIBRARY_IDS",
            "MEDIA_COMPANION_PLEX_ALLOWED_LIBRARY_IDS",
        ),
        "allowed_library_names": tuple(getattr(config, "plex_library_names", ()) or ())
        or _env_identifier_list(values, "MEDIA_COMPANION_PLEX_LIBRARY_NAMES"),
        "trusted_ingress_peers": _env_trusted_ingress_peers(values),
        "readiness": policy.ready,
    }
    try:
        built = _call_runtime_factory(factory, dependencies)
    except StartupConfigurationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StartupConfigurationError("production runtime factory failed") from exc
    if isinstance(built, CompanionRuntime):
        runtime = built
        if not runtime.production:
            raise StartupConfigurationError("production runtime cannot use test mode")
    elif isinstance(built, Mapping):
        options = dict(dependencies)
        options.update(dict(built))
        options["production"] = True
        options.pop("config", None)
        accepted = set(inspect.signature(CompanionRuntime).parameters)
        options = {key: value for key, value in options.items() if key in accepted}
        try:
            runtime = cast(Any, CompanionRuntime)(**options)
        except Exception as exc:  # noqa: BLE001
            raise StartupConfigurationError("production runtime is incomplete") from exc
    else:
        raise StartupConfigurationError(
            "production runtime factory returned an invalid value"
        )
    try:
        runtime.validate_production()
    except Exception as exc:  # noqa: BLE001
        raise StartupConfigurationError("production runtime is not ready") from exc
    return runtime


def _serve_production(app: Any, values: Mapping[str, str]) -> None:
    """Serve one ASGI app on one listener unless a split bind is explicit."""

    try:
        import asyncio
        import uvicorn
    except Exception as exc:  # noqa: BLE001
        raise StartupConfigurationError(
            "ASGI server dependency is unavailable"
        ) from exc
    host = _env_text(values, "MEDIA_COMPANION_HOST") or "0.0.0.0"
    configured_webhook_host = _env_text(values, "MEDIA_COMPANION_WEBHOOK_HOST")
    webhook_host = configured_webhook_host or host
    for value in (host, webhook_host):
        if any(ord(character) < 0x20 or character.isspace() for character in value):
            raise StartupConfigurationError("HTTP host configuration is invalid")
    mcp_port = _env_port(
        values, 18_080, "MEDIA_COMPANION_MCP_PORT", "MEDIA_COMPANION_PORT"
    )
    configured_webhook_port = _env_text(values, "MEDIA_COMPANION_WEBHOOK_PORT")
    # Compose publishes host 18081 to the single container listener on 18080.
    # Do not bind an unadvertised second socket by default; an explicitly
    # configured webhook host/port remains available for deployments that
    # intentionally split listeners.
    webhook_port = (
        mcp_port
        if configured_webhook_port is None
        else _env_port(values, mcp_port, "MEDIA_COMPANION_WEBHOOK_PORT")
    )
    if (configured_webhook_port is None and configured_webhook_host is None) or (
        mcp_port == webhook_port and webhook_host == host
    ):
        servers = [
            uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=host,
                    port=mcp_port,
                    workers=1,
                    lifespan="on",
                    access_log=False,
                )
            )
        ]
    else:
        servers = [
            uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=host,
                    port=mcp_port,
                    workers=1,
                    lifespan="on",
                    access_log=False,
                )
            ),
            uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=webhook_host,
                    port=webhook_port,
                    workers=1,
                    lifespan="on",
                    access_log=False,
                )
            ),
        ]

    async def run() -> None:
        tasks = [asyncio.create_task(server.serve()) for server in servers]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for server in servers:
                server.should_exit = True
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def main() -> None:
    """Production module entrypoint used by the companion container."""

    values: Mapping[str, str] = os.environ
    try:
        runtime = build_production_runtime(env=values)
        app = create_app(runtime, production=True)
        _serve_production(app, values)
    except KeyboardInterrupt:
        return
    except Exception as exc:  # noqa: BLE001
        # Keep startup failures deliberately generic.  The process exits
        # non-zero so the orchestrator does not advertise a false healthy app.
        del exc
        raise SystemExit(78) from None


class _FallbackResponse:
    def __init__(self, body: bytes, status: int, content_type: str) -> None:
        self.body = body
        self.status_code = status
        self.content_type = content_type


class _FallbackASGI:
    """Minimal fallback only used when Starlette is absent in a unit-test env."""

    def __init__(self, runtime: CompanionRuntime) -> None:
        self.state = type("State", (), {})()
        self.state.runtime = runtime

    async def __call__(
        self, scope: Mapping[str, object], receive: object, send: object
    ) -> None:
        del receive
        path = scope.get("path")
        if path == "/healthz":
            response = {"status": "ok"}
        elif path in {"/readyz", "/healthz/ready"}:
            ready = self.state.runtime.ready()
            response = {"status": "ready" if ready else "not_ready", "ready": ready}
        else:
            response = {"error": "request denied"}
        body = _json_dumps(response, maximum=1024)
        sender = cast(Any, send)
        await sender(
            {
                "type": "http.response.start",
                "status": 200
                if path == "/healthz"
                else 503
                if path in {"/readyz", "/healthz/ready"}
                else 404,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await sender({"type": "http.response.body", "body": body})


def create_app(runtime: CompanionRuntime, *, production: bool | None = None) -> Any:
    """Create the ASGI app with explicit dependency injection.

    ``production`` may only make checks stricter.  Passing ``False`` is the
    supported way for tests to use the in-memory auth stores; production
    startup never infers test mode from an environment variable.
    """

    if not isinstance(runtime, CompanionRuntime):
        raise TypeError("runtime must be CompanionRuntime")
    if production is True and not runtime.production:
        runtime.production = True
    if production is False and runtime.production:
        # Do not silently downgrade a secure runtime supplied by a caller.
        raise DurableStoreRequiredError("production runtime cannot be downgraded")
    if runtime.production:
        runtime.validate_production()
    if Starlette is None or Route is None:
        if runtime.production:
            raise OperationDependencyError("Starlette ASGI runtime is unavailable")
        return _FallbackASGI(runtime)
    routes = [
        Route("/mcp", _mcp_endpoint, methods=["POST"]),
        Route("/healthz", _health_endpoint, methods=["GET"]),
        Route("/readyz", _ready_endpoint, methods=["GET"]),
        Route("/healthz/ready", _ready_endpoint, methods=["GET"]),
        Route(CONFIRM_BIND_PATH, _confirm_bind_endpoint, methods=["POST"]),
        Route(CONFIRM_CALLBACK_PATH, _confirm_callback_endpoint, methods=["POST"]),
        Route("/private/dashboard/{operation}", _dashboard_endpoint, methods=["POST"]),
        Route("/private/operations/{operation}", _dashboard_endpoint, methods=["POST"]),
        Route("/private/plex/{capability:path}", _plex_endpoint, methods=["POST"]),
    ]

    @asynccontextmanager
    async def lifespan(_app: Any):
        if runtime.production:
            await runtime.start_worker()
        try:
            yield
        finally:
            if runtime.production:
                await runtime.stop_worker()

    app = Starlette(routes=routes, lifespan=lifespan)
    # The companion contract has exact private paths.  Starlette's default
    # slash redirect would otherwise turn ``/private/plex/<cap>/`` (or a
    # similarly copied confirmation/dashboard URL) into an accepted route.
    app.router.redirect_slashes = False
    app.state.runtime = runtime
    return app


build_app = create_app
application = create_app


if __name__ == "__main__":  # pragma: no cover - exercised by the container
    main()


__all__ = [
    "CONFIRM_BIND_PATH",
    "CONFIRM_BIND_TOOL",
    "CONFIRM_CALLBACK_PATH",
    "DASHBOARD_SERVICE_ACTOR",
    "DASHBOARD_OPERATIONS",
    "MAX_MCP_BODY_BYTES",
    "MAX_MCP_RESPONSE_BYTES",
    "MCP_PATH",
    "OPERATIONS_PREFIX",
    "PLEX_PREFIX",
    "CompanionRuntime",
    "HTTPBoundaryError",
    "StartupConfigurationError",
    "build_app",
    "build_production_runtime",
    "create_app",
    "main",
]
