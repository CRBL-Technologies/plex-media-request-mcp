from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import NoReturn, cast
import unittest

import requests

from media_dashboard.app import (
    DashboardApp,
    DashboardConfig,
    DashboardConfigurationError,
    SlidingWindowRateLimiter,
)
from media_dashboard.auth import hash_password
from media_dashboard.companion import (
    ALLOWED_OPERATIONS,
    CompanionConfigurationError,
    CompanionResponse,
    CompanionProtocolError,
    CompanionUnavailable,
    DashboardCompanionClient,
    _sanitize_operation_response,
    _resolve_private_service,
    _validate_base_url,
    load_api_key_file,
    operation_allowlist_fingerprint,
    validate_operation_allowlist,
)
from media_companion.operations import DASHBOARD_OPERATION_SET


PASSWORD_HASH = hash_password("dashboard password", salt=b"0123456789abcdef")


class _FakeCompanion:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.healthy = True

    def call(self, operation: str, payload: dict[str, object]) -> CompanionResponse:
        self.calls.append((operation, dict(payload)))
        if operation == "health":
            return CompanionResponse(operation, {"ok": True, "healthy": self.healthy})
        if operation == "users":
            return CompanionResponse(operation, {"ok": True, "users": []})
        if operation == "users.add" and "confirmation" not in payload:
            return CompanionResponse(
                operation,
                {
                    "ok": True,
                    "confirmation_required": True,
                    "confirmation_capability": "C" * 32,
                    "preview_digest": "a" * 64,
                    "preview": f"Confirmation required\\nTool: users.add\\nTarget: user_id:{payload['user_id']}",
                },
            )
        return CompanionResponse(operation, {"ok": True, "changed": True})


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.companion = _FakeCompanion()
        self.config = DashboardConfig(
            password_hash=PASSWORD_HASH,
            allowed_origins=("http://dashboard.test",),
            companion_api_key=b"k" * 32,
            port=0,
            read_limit=10,
        )
        self.app = DashboardApp(self.config, companion=self.companion)
        self.server = self.app.make_server(host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: str | bytes | None = None,
        cookies: list[str] | None = None,
        accept: str = "application/json",
        content_type: str = "application/json",
        origin: str | None = "http://dashboard.test",
        referer: str | None = None,
        host: str = "dashboard.test",
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        headers = {"Host": host, "Accept": accept}
        if origin is not None:
            headers["Origin"] = origin
        if referer is not None:
            headers["Referer"] = referer
        if cookies:
            headers["Cookie"] = "; ".join(cookie.split(";", 1)[0] for cookie in cookies)
        payload: bytes | None
        if body is None:
            payload = None
        else:
            payload = body if isinstance(body, bytes) else body.encode()
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(len(payload))
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
            body_bytes = response.read()
            return response.status, response.getheaders(), body_bytes
        finally:
            connection.close()

    @staticmethod
    def _cookies(headers: list[tuple[str, str]]) -> list[str]:
        return [value for key, value in headers if key.lower() == "set-cookie"]

    @staticmethod
    def _csrf(cookies: list[str]) -> str:
        for cookie in cookies:
            if cookie.startswith("dashboard_csrf="):
                return cookie.split("=", 1)[1].split(";", 1)[0]
        raise AssertionError("missing csrf cookie")

    def test_server_rendered_preview_execute_requires_exact_capability(self) -> None:
        status, headers, _ = self._request(
            "POST",
            "/login",
            body="password=dashboard+password",
            accept="text/html",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(status, 303)
        cookies = self._cookies(headers)
        csrf = self._csrf(cookies)
        status, _headers, body = self._request(
            "GET", "/", cookies=cookies, accept="text/html"
        )
        self.assertEqual(status, 200)
        self.assertIn(b'name="csrf_token"', body)
        form = f"csrf_token={csrf}&user_id=42&fingerprint=fp-1&version=1&idempotency_key=edit-1"
        status, _headers, body = self._request(
            "POST",
            "/users/add",
            body=form,
            cookies=cookies,
            accept="text/html",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(status, 200)
        self.assertIn(b"Execute exact preview", body)
        self.assertIn(b"C" * 32, body)
        execute = (
            f"csrf_token={csrf}&user_id=42&fingerprint=fp-1&version=1&idempotency_key=edit-1"
            f"&confirmation={'C' * 32}&preview_digest={'a' * 64}"
        )
        status, _headers, body = self._request(
            "POST",
            "/users/add",
            body=execute,
            cookies=cookies,
            accept="text/html",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(status, 200)
        self.assertIn(b"changed", body)
        self.assertEqual(self.companion.calls[-1][1]["confirmation"], "C" * 32)

        arbitrary = f"{form}&confirmation=confirm&preview_digest={'a' * 64}"
        status, _headers, _body = self._request(
            "POST",
            "/users/add",
            body=arbitrary,
            cookies=cookies,
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(status, 400)
        bad_key = form.replace("idempotency_key=edit-1", "idempotency_key=bad%2Fkey")
        status, _headers, _body = self._request(
            "POST",
            "/users/add",
            body=bad_key,
            cookies=cookies,
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(status, 400)

    def test_root_redirects_unauthenticated_html_to_login(self) -> None:
        status, headers, body = self._request("GET", "/", accept="text/html")
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["Location"], "/login")
        self.assertEqual(body, b"")

        status, _headers, body = self._request(
            "GET", "/", cookies=["dashboard_session=expired"], accept="text/html"
        )
        self.assertEqual(status, 303)
        self.assertEqual(body, b"")

        status, _headers, body = self._request("GET", "/")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "request_failed"})

        status, _headers, body = self._request("GET", "/login", accept="text/html")
        self.assertEqual(status, 200)
        self.assertIn(b'type="password"', body)

    def test_login_accepts_exact_same_origin_referer_fallback(self) -> None:
        status, headers, _ = self._request(
            "POST",
            "/login",
            body="password=dashboard+password",
            accept="text/html",
            content_type="application/x-www-form-urlencoded",
            origin=None,
            referer="http://dashboard.test/login",
        )
        self.assertEqual(status, 303)
        self.assertGreaterEqual(len(self._cookies(headers)), 2)

        # Safari and privacy-focused clients may omit both headers on a
        # same-origin form submission. The password boundary must still be
        # reachable; Host validation and login rate limiting remain active.
        status, headers, _ = self._request(
            "POST",
            "/login",
            body="password=dashboard+password",
            accept="text/html",
            content_type="application/x-www-form-urlencoded",
            origin=None,
            referer=None,
        )
        self.assertEqual(status, 303)
        self.assertGreaterEqual(len(self._cookies(headers)), 2)

        for origin, referer in (
            (None, "http://evil.example/login"),
            ("http://evil.example", None),
        ):
            status, _headers, _ = self._request(
                "POST",
                "/login",
                body="password=dashboard+password",
                accept="text/html",
                content_type="application/x-www-form-urlencoded",
                origin=origin,
                referer=referer,
            )
            self.assertEqual(status, 400)

    def test_health_status_readiness_and_path_framing(self) -> None:
        self.assertEqual(self._request("GET", "/healthz", host="evil")[0], 200)
        self.assertEqual(self._request("GET", "/readyz", host="evil")[0], 400)
        self.assertEqual(self._request("GET", "/readyz")[0], 401)
        self.assertEqual(self._request("GET", "/x%2fhealthz")[0], 400)
        self.assertEqual(self._request("GET", "/./healthz")[0], 400)
        self.assertEqual(self._request("GET", "/healthz#fragment")[0], 400)
        # A non-zero body on a GET is rejected before routing, avoiding
        # request-smuggling ambiguity.
        self.assertEqual(
            self._request("GET", "/healthz", body=b"x", content_type="text/plain")[0],
            400,
        )
        self.assertEqual(
            self._request("GET", "/healthz", body=b"", content_type="text/plain")[0],
            200,
        )

        status, headers, _ = self._request(
            "POST",
            "/login",
            body="password=dashboard+password",
            accept="application/json",
            content_type="application/x-www-form-urlencoded",
        )
        cookies = self._cookies(headers)
        self.companion.healthy = False
        status, _headers, payload = self._request("GET", "/api/health", cookies=cookies)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(payload)["data"]["healthy"], False)

        class NotReadyCompanion(_FakeCompanion):
            def ready(self) -> bool:
                return False

        object.__setattr__(self.app, "companion", NotReadyCompanion())
        self.assertEqual(self._request("GET", "/readyz", cookies=cookies)[0], 503)

    def test_csrf_origin_and_read_rate_limit(self) -> None:
        status, headers, _ = self._request(
            "POST",
            "/login",
            body="password=dashboard+password",
            content_type="application/x-www-form-urlencoded",
        )
        cookies = self._cookies(headers)
        csrf = self._csrf(cookies)
        object.__setattr__(self.config, "read_limit", 1)
        # Header-only mutation without the cookie-bound token fails closed.
        status, _headers, _body = self._request(
            "POST",
            "/users/add",
            body=json.dumps(
                {"user_id": 1, "fingerprint": "f", "version": 1, "idempotency_key": "i"}
            ),
            cookies=cookies,
            origin="http://evil.example",
        )
        self.assertEqual(status, 400)
        status, _headers, _body = self._request("GET", "/api/users", cookies=cookies)
        self.assertEqual(status, 200)
        status, _headers, _body = self._request("GET", "/api/users", cookies=cookies)
        self.assertEqual(status, 429)
        # Keep csrf used so a test refactor cannot accidentally remove the
        # authenticated setup.
        self.assertTrue(csrf)


class DashboardPrimitiveTests(unittest.TestCase):
    def test_dashboard_operation_inventory_is_frozen(self) -> None:
        self.assertEqual(
            operation_allowlist_fingerprint(ALLOWED_OPERATIONS),
            operation_allowlist_fingerprint(set(ALLOWED_OPERATIONS)),
        )
        validate_operation_allowlist(ALLOWED_OPERATIONS)
        with self.assertRaises(CompanionConfigurationError):
            validate_operation_allowlist(set(ALLOWED_OPERATIONS) | {"mcp.raw"})
        # The dashboard and companion must fail CI together when either side's
        # typed operation inventory changes.
        self.assertEqual(DASHBOARD_OPERATION_SET, ALLOWED_OPERATIONS)

    def test_startup_hash_origin_secret_and_response_taint(self) -> None:
        with self.assertRaises(DashboardConfigurationError):
            DashboardConfig(
                password_hash="scrypt$garbage",
                allowed_origins=("https://dashboard.test",),
                companion_api_key=b"k" * 32,
            )
        with self.assertRaises(DashboardConfigurationError):
            DashboardConfig(
                password_hash=PASSWORD_HASH,
                allowed_origins=("https://dashboard.test",),
                companion_api_key=b"k" * 32,
                cookie_secure=False,
            )
        with self.assertRaises(DashboardConfigurationError):
            DashboardConfig(
                password_hash=PASSWORD_HASH,
                allowed_origins=("http://dashboard.test",),
                companion_api_key=b"short",
            )
        with self.assertRaises(CompanionConfigurationError):
            _validate_base_url("http://169.254.169.254:18080")
        with self.assertRaises(CompanionConfigurationError):
            _validate_base_url("http://public.example:18080")
        with self.assertRaises(CompanionConfigurationError):
            _resolve_private_service(
                "media-companion",
                resolver=lambda *_args, **_kwargs: [
                    (None, None, None, None, ("203.0.113.10", 18080))
                ],
            )
        with self.assertRaises(CompanionProtocolError):
            _sanitize_operation_response(
                "health", {"healthy": True, "environment": "secret"}
            )
        preview = "Confirmation required\nTool: users.add\nTarget: users.add:user_id:1"
        sanitized = _sanitize_operation_response(
            "users.add",
            {
                "confirmation_required": True,
                "preview": preview,
                "preview_digest": "a" * 64,
            },
        )
        self.assertEqual(sanitized["preview"], preview)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key"
            path.write_bytes(b"k" * 32)
            os.chmod(path, 0o600)
            self.assertEqual(load_api_key_file(path), b"k" * 32)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(CompanionConfigurationError):
                load_api_key_file(link)

    def test_limiter_purges_long_login_window(self) -> None:
        now = [0.0]
        limiter = SlidingWindowRateLimiter(clock=lambda: now[0])
        self.assertTrue(limiter.allow("login", "a", limit=1, window_seconds=900))
        now[0] = 601.0
        self.assertEqual(limiter.purge(), 0)
        now[0] = 901.0
        self.assertEqual(limiter.purge(), 1)

    def test_limiter_reclaims_expired_capacity_and_reserves_duplicates(self) -> None:
        now = [0.0]
        limiter = SlidingWindowRateLimiter(max_buckets=1, clock=lambda: now[0])
        self.assertTrue(limiter.allow("read", "first", limit=1, window_seconds=10))
        self.assertFalse(limiter.allow("read", "second", limit=1, window_seconds=10))
        now[0] = 11.0
        self.assertTrue(limiter.allow("read", "second", limit=1, window_seconds=10))
        now[0] = 12.0
        self.assertFalse(
            limiter.allow_many(
                (
                    ("read", "second", 1, 10),
                    ("read", "second", 1, 10),
                )
            )
        )


class CompanionClientTests(unittest.TestCase):
    class _Response:
        status_code = 200
        is_redirect = False
        is_permanent_redirect = False
        headers = {"Content-Type": "application/json"}
        content = json.dumps(
            {"ok": True, "operation": "health", "data": {"healthy": True}}
        ).encode()

        def close(self) -> None:
            return

    class _Session:
        trust_env = True

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.urls: list[str] = []

        def post(self, _url: str, **kwargs: object) -> "CompanionClientTests._Response":
            self.calls.append(kwargs)
            self.urls.append(_url)
            return CompanionClientTests._Response()

    @staticmethod
    def _resolver(_host: str, port: int, **_kwargs: object) -> list[object]:
        return [(None, None, None, None, ("172.18.0.8", port))]

    def test_private_origin_and_signed_session_metadata(self) -> None:
        session = self._Session()
        client = DashboardCompanionClient(
            "http://media-companion:18080",
            b"k" * 32,
            session=cast(requests.Session, session),
            resolver=self._resolver,
            nonce_factory=lambda: "N" * 16,
        )
        result = client.call(
            "health",
            session_actor="dashboard-admin",
            session_digest="a" * 64,
            audit_context="dashboard:a:health",
        )
        result_data = result.data.get("data")
        self.assertIsInstance(result_data, dict)
        assert isinstance(result_data, dict)
        self.assertEqual(result_data["healthy"], True)
        timeout = cast(tuple[float, float], session.calls[0]["timeout"])
        self.assertEqual(timeout[0], 3.0)
        self.assertLessEqual(timeout[1], 15.0)
        self.assertGreater(timeout[1], 0.0)
        headers = cast(dict[str, str], session.calls[0]["headers"])
        self.assertEqual(headers["X-CRBL-Dashboard-Actor"], "dashboard-admin")
        self.assertEqual(headers["X-CRBL-Dashboard-Session-Digest"], "a" * 64)
        self.assertEqual(
            session.urls[0], "http://media-companion:18080/private/dashboard/health"
        )

    def test_breaker_opens_after_five_transport_failures(self) -> None:
        class FailingSession(CompanionClientTests._Session):
            def post(self, _url: str, **_kwargs: object) -> NoReturn:
                raise requests.ConnectionError("offline")

        session = FailingSession()
        client = DashboardCompanionClient(
            "http://media-companion:18080",
            b"k" * 32,
            session=cast(requests.Session, session),
            resolver=self._resolver,
        )
        for _ in range(5):
            with self.assertRaises(CompanionUnavailable):
                client.call("health")
        with self.assertRaises(CompanionUnavailable):
            client.call("health")
        self.assertEqual(len(session.calls), 0)


if __name__ == "__main__":
    unittest.main()
