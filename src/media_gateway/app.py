"""ASGI application for the gateway, dashboard, and Plex webhook."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from urllib.parse import quote

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from .auth import Sessions
from .config import Config, load_config_file
from .dashboard import CSS, dashboard_page, login_page
from .notifications import Notifications
from .password import verify_password
from .policy import Policy
from .secrets import read_secret
from .store import Store
from .tools import ToolError, ToolService
from .types import Actor, Role
from .upstream import Upstream, UpstreamError

COOKIE = "crbl_media_session"
LOGGER = logging.getLogger(__name__)
MAX_BODY_BYTES = 4 * 1024 * 1024


class Runtime:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.db_path)
        self.policy = Policy(config.policy_file)
        self.upstream = Upstream(config.upstream_url, config.upstream_token_file)
        self.tools = ToolService(config, self.store, self.upstream)
        self.notifications = Notifications(config, self.store, self.policy, self.upstream)
        self.sessions = Sessions(
            read_secret(config.dashboard_session_key_file, minimum=32).encode()
        )
        self.password_hash = read_secret(config.dashboard_password_hash_file)
        self.gateway_token = read_secret(config.gateway_token_file, minimum=32)
        self.plex_token = read_secret(config.plex_webhook_token_file, minimum=32)


async def _reconcile_loop(
    runtime: Runtime,
    stop: asyncio.Event,
    startup_intents: list[dict[str, Any]],
) -> None:
    try:
        report = await runtime.tools.reconcile_request_intents(startup_intents)
        if report["unresolved"]:
            LOGGER.warning(
                "request reconciliation left %d startup intent(s) unresolved",
                report["unresolved"],
            )
    except Exception:
        LOGGER.exception("startup request reconciliation failed")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
            continue
        except TimeoutError:
            pass
        try:
            report = await runtime.tools.reconcile_pending_requests(
                updated_before=int(time.time()) - 300
            )
            if report["unresolved"]:
                LOGGER.warning(
                    "request reconciliation left %d stale intent(s) unresolved",
                    report["unresolved"],
                )
        except Exception:
            LOGGER.exception("request reconciliation failed")


def _runtime(request: Request) -> Runtime:
    runtime = request.app.state.runtime
    if not isinstance(runtime, Runtime):
        raise RuntimeError("gateway runtime is unavailable")
    return runtime


def _session(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    return token if _runtime(request).sessions.valid(token) else None


def _dashboard_auth(request: Request) -> str | Response:
    token = _session(request)
    if token:
        return token
    return RedirectResponse("/login", status_code=303)


async def _form_with_csrf(request: Request, token: str) -> dict[str, str]:
    form = await request.form()
    values = {key: str(value) for key, value in form.multi_items() if isinstance(key, str)}
    if not _runtime(request).sessions.valid_csrf(token, values.get("csrf")):
        raise ToolError("invalid form token")
    return values


def _trusted(request: Request) -> bool:
    authorization = request.headers.get("authorization", "")
    expected = f"Bearer {_runtime(request).gateway_token}"
    return hmac.compare_digest(authorization, expected)


def _page_number(request: Request, name: str) -> int:
    raw = request.query_params.get(name, "1")
    if len(raw) > 5 or not raw.isascii() or not raw.isdigit():
        return 1
    value = int(raw)
    return min(value, 10_000) if value > 0 else 1


async def health(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def ready(request: Request) -> Response:
    runtime = _runtime(request)
    try:
        runtime.policy.snapshot()
        runtime.store.recent_activity(1)
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return JSONResponse({"status": "ready"})


async def css(_request: Request) -> Response:
    return PlainTextResponse(CSS, media_type="text/css")


async def login(request: Request) -> Response:
    if request.method == "GET":
        if _session(request):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(login_page())
    form = await request.form()
    password = form.get("password")
    if not isinstance(password, str) or not verify_password(
        password, _runtime(request).password_hash
    ):
        return HTMLResponse(login_page(error="Incorrect password."), status_code=401)
    token = _runtime(request).sessions.issue()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE,
        token,
        max_age=_runtime(request).sessions.ttl_seconds,
        httponly=True,
        secure=_runtime(request).config.secure_cookies,
        samesite="strict",
        path="/",
    )
    return response


async def logout(request: Request) -> Response:
    token = _session(request)
    if not token:
        return RedirectResponse("/login", status_code=303)
    await _form_with_csrf(request, token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


async def dashboard(request: Request) -> Response:
    auth = _dashboard_auth(request)
    if isinstance(auth, Response):
        return auth
    runtime = _runtime(request)
    snapshot = runtime.policy.snapshot()
    roles = {user_id: Role.USER for user_id in snapshot.allowed}
    roles.update({user_id: Role.ADMIN for user_id in snapshot.admins})
    request_page = runtime.store.request_page(_page_number(request, "request_page"))
    activity_page = runtime.store.activity_page(_page_number(request, "activity_page"))
    return HTMLResponse(
        dashboard_page(
            users=runtime.store.users(roles),
            activity=activity_page,
            requests=request_page,
            csrf=runtime.sessions.csrf(auth),
            notice=request.query_params.get("notice"),
        )
    )


async def change_user(request: Request) -> Response:
    token = _session(request)
    if not token:
        return RedirectResponse("/login", status_code=303)
    try:
        form = await _form_with_csrf(request, token)
        raw = form.get("user_id", "")
        if not raw.isdigit() or int(raw) <= 0:
            raise ValueError("Telegram user ID must be a positive integer")
        allowed = request.url.path.endswith("/add")
        _runtime(request).policy.set_allowed(int(raw), allowed=allowed)
        if allowed and _runtime(request).config.telegram_identity_sync:
            with suppress(Exception):
                await _runtime(request).notifications.sync_user(int(raw))
        action = "allowed" if allowed else "removed"
        _runtime(request).store.record_activity("policy", f"User {raw} {action}", int(raw))
        notice = f"User {raw} was {action}."
    except (ValueError, ToolError) as exc:
        notice = str(exc)
    return RedirectResponse(f"/?notice={quote(notice)}", status_code=303)


async def actors(request: Request) -> Response:
    if not _trusted(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"actor", "blocked"}:
            raise ValueError("invalid request")
        actor = Actor.from_json(body.get("actor"))
        blocked = body.get("blocked", False)
        if not isinstance(blocked, bool):
            raise ValueError("blocked must be a boolean")
        role = _runtime(request).policy.snapshot().role(actor.user_id)
        _runtime(request).store.observe_actor(actor, blocked=blocked or role is Role.BLOCKED)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "role": role.value})


async def tool_schema(request: Request) -> Response:
    if not _trusted(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return JSONResponse({"tools": await _runtime(request).tools.all_schemas()})
    except UpstreamError:
        LOGGER.warning("upstream schema discovery failed")
        return JSONResponse({"error": "media service is temporarily unavailable"}, status_code=502)


async def list_tools(request: Request) -> Response:
    if not _trusted(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        actor = Actor.from_json(await request.json())
        role = _runtime(request).policy.snapshot().role(actor.user_id)
        tools = await _runtime(request).tools.tools_for(role)
        return JSONResponse({"role": role.value, "tools": tools})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except UpstreamError:
        LOGGER.warning("upstream tool discovery failed")
        return JSONResponse({"error": "media service is temporarily unavailable"}, status_code=502)


async def call_tool(request: Request) -> Response:
    if not _trusted(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"actor", "name", "arguments"}:
            raise ToolError("request must contain actor, name, and arguments")
        actor = Actor.from_json(body["actor"])
        name = body["name"]
        if not isinstance(name, str):
            raise ToolError("name must be a string")
        role = _runtime(request).policy.snapshot().role(actor.user_id)
        result = await _runtime(request).tools.call(name, body["arguments"], actor, role)
        return JSONResponse({"ok": True, "result": result})
    except (ToolError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except UpstreamError:
        LOGGER.warning("upstream tool call failed")
        return JSONResponse(
            {"ok": False, "error": "media service is temporarily unavailable"},
            status_code=502,
        )


async def plex_webhook(request: Request) -> Response:
    query_token = request.query_params.get("token", "")
    path_token = request.path_params.get("token", "")
    if query_token and path_token and not hmac.compare_digest(query_token, path_token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    supplied = path_token or query_token
    if not hmac.compare_digest(supplied, _runtime(request).plex_token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            raw = form.get("payload")
            if not isinstance(raw, str):
                raise ValueError("missing Plex payload")
            payload: Any = json.loads(raw)
        else:
            payload = await request.json()
        added = await _runtime(request).notifications.observe_plex(payload)
        return JSONResponse({"accepted": added})
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "invalid Plex payload"}, status_code=400)


class SecurityHeaders:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        async def secured(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (
                            b"content-security-policy",
                            b"default-src 'self'; form-action 'self'; frame-ancestors 'none'",
                        ),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (b"cache-control", b"no-store"),
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secured)


class BodySizeGuard:
    def __init__(self, app: Any, maximum: int = MAX_BODY_BYTES):
        self.app = app
        self.maximum = maximum

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        raw = headers.get(b"content-length")
        try:
            declared = int(raw) if raw is not None else None
            if declared is not None and declared < 0:
                raise ValueError
        except ValueError:
            response = PlainTextResponse("Invalid Content-Length", status_code=400)
            await response(scope, receive, send)
            return
        if declared is not None and declared > self.maximum:
            response = PlainTextResponse("Request body too large", status_code=413)
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message: dict[str, Any] = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    received += len(body)
                if received > self.maximum:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            response = PlainTextResponse("Request body too large", status_code=413)
            await response(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


def create_app(config: Config | None = None) -> Starlette:
    configured = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        runtime = Runtime(configured)
        runtime.store.prune()
        startup_intents = runtime.store.pending_request_intents()
        app.state.runtime = runtime
        worker = asyncio.create_task(runtime.notifications.run())
        reconcile_stop = asyncio.Event()
        reconcile_worker = asyncio.create_task(
            _reconcile_loop(runtime, reconcile_stop, startup_intents)
        )
        try:
            yield
        finally:
            runtime.notifications.stop()
            reconcile_stop.set()
            await worker
            reconcile_worker.cancel()
            with suppress(asyncio.CancelledError):
                await reconcile_worker

    app = Starlette(
        routes=[
            Route("/healthz", health),
            Route("/readyz", ready),
            Route("/assets/app.css", css),
            Route("/login", login, methods=["GET", "POST"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/", dashboard),
            Route("/users/add", change_user, methods=["POST"]),
            Route("/users/remove", change_user, methods=["POST"]),
            Route("/api/actors", actors, methods=["POST"]),
            Route("/api/schema", tool_schema, methods=["GET"]),
            Route("/api/tools", list_tools, methods=["POST"]),
            Route("/api/tools/call", call_tool, methods=["POST"]),
            Route("/plex", plex_webhook, methods=["POST"]),
            # Preserve the already-configured Plex webhook URL during the
            # cutover from the discarded companion implementation.
            Route("/private/plex/{token:path}", plex_webhook, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(SecurityHeaders)
    app.add_middleware(BodySizeGuard)
    return app


def main() -> None:
    load_config_file()
    config = Config.from_env()
    # Plex cannot attach a custom Authorization header, so the webhook uses a
    # URL credential. Disable access logs so it is never copied into logs.
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
