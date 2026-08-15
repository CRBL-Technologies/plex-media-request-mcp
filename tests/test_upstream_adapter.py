from __future__ import annotations

import asyncio
import hashlib
import json
import math
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, cast

import requests

from media_companion import tool_policy
from media_companion.config import SecretFileRef
from media_companion.clients.upstream_mcp import (
    CircuitBreaker,
    CircuitOpenError,
    ConcurrencyLimiter,
    DeadlineExceededError,
    MCPToolResult,
    RadarrQueueAdapter,
    ResponseSizeError,
    ResponseValidationError,
    ToolNotAllowedError,
    UpstreamContractError,
    UpstreamMCPClient,
    UpstreamTransportError,
    MCP_PROTOCOL_VERSION,
    _load_token,
    _queue_page_wire_size,
)
from media_companion.redaction import redact_json, redact_text
from media_companion.upstream_contract import (
    FROZEN_UPSTREAM_TOOLS,
    UPSTREAM_TOOL_CONTRACT_SHA256,
    canonical_tool_digest,
)


class FakeResponse:
    def __init__(
        self,
        payload: object,
        *,
        status_code: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")


class RecordingTransport:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[
            tuple[str, str, dict[str, str], bytes, tuple[float, float]]
        ] = []
        self.redirects: list[bool] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: bytes,
        timeout: tuple[float, float],
        allow_redirects: bool,
    ) -> object:
        self.calls.append((method, url, headers, data, timeout))
        self.redirects.append(allow_redirects)
        return self.response


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain(child) for child in value]
    if isinstance(value, list):
        return [_plain(child) for child in value]
    return value


class ContractTransport:
    def __init__(self, tools: list[dict[str, object]] | None = None) -> None:
        frozen = FROZEN_UPSTREAM_TOOLS if tools is None else tools
        plain_tools = _plain(frozen)
        assert isinstance(plain_tools, list)
        self.tools = cast(list[dict[str, object]], plain_tools)
        self.calls: list[
            tuple[str, str, dict[str, str], bytes, tuple[float, float]]
        ] = []
        self.redirects: list[bool] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: bytes,
        timeout: tuple[float, float],
        allow_redirects: bool,
    ) -> object:
        self.calls.append((method, url, headers, data, timeout))
        self.redirects.append(allow_redirects)
        request = json.loads(data)
        if request["method"] == "initialize":
            payload: dict[str, object] = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "serverInfo": {"name": "pinned-upstream", "version": "2.3.0"},
            }
        else:
            payload = {"tools": self.tools}
        return FakeResponse({"jsonrpc": "2.0", "id": request["id"], "result": payload})


class FailingTransport:
    def request(self, *args: object, **kwargs: object) -> object:
        raise OSError("secret path must not escape")


class BlockingSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.trust_env = True
        self.proxies: dict[str, str] = {}

    def request(self, *args: object, **kwargs: object) -> FakeResponse:
        self.entered.set()
        if not self.release.wait(2):
            raise TimeoutError("test session was not released")
        self.finished.set()
        return self.response


class UpstreamAdapterTests(unittest.TestCase):
    def _result(self, *, request_id: int = 1) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": 'Movie /run/secrets/key {"apiKey":"do-not-return"}',
                    }
                ],
                "structuredContent": {
                    "title": "Example",
                    "apiKey": "do-not-return",
                    "rootFolder": "/media/movies",
                    "providerUrl": "https://provider.invalid/item?token=secret",
                    "nested": [{"authorization": "Bearer abcdefghijkl"}],
                },
            },
        }

    def test_exact_pinned_surface_and_future_default_deny(self) -> None:
        self.assertEqual(len(tool_policy.UPSTREAM_TOOLS), 102)
        inventory_digest = hashlib.sha256(
            json.dumps(tool_policy.UPSTREAM_TOOLS, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(
            inventory_digest,
            "02390ae11d07dae8920276460e83503ddd0d115d4ea7f76f19a1f48648f46b24",
        )
        client = UpstreamMCPClient(
            "http://media-server:3000", transport=RecordingTransport({})
        )
        self.assertEqual(client.tool_names, tool_policy.UPSTREAM_TOOLS)
        self.assertTrue(
            tool_policy.SHARED_TOOL_SET.isdisjoint(tool_policy.UPSTREAM_TOOL_SET)
        )
        for mutating_name in (
            "plex_refresh_library",
            "radarr_add_movie",
            "radarr_search_movie_releases",
            "radarr_delete_queue_item",
            "sonarr_add_series",
        ):
            self.assertEqual(
                tool_policy.classify_upstream_tool(mutating_name),
                tool_policy.ToolClassification.MUTATE,
            )
        with self.assertRaises(ToolNotAllowedError):
            client.call_tool("radarr_get_queue")
        with self.assertRaises(ToolNotAllowedError):
            client.call_tool("request_movie")
        with self.assertRaises(ToolNotAllowedError):
            client.call_tool("future_tool")

    def test_frozen_contract_artifact_has_exact_digest_and_fields(self) -> None:
        self.assertEqual(len(FROZEN_UPSTREAM_TOOLS), 102)
        self.assertEqual(
            canonical_tool_digest(FROZEN_UPSTREAM_TOOLS), UPSTREAM_TOOL_CONTRACT_SHA256
        )
        self.assertEqual(
            set(FROZEN_UPSTREAM_TOOLS[0]),
            {"name", "title", "description", "inputSchema", "annotations"},
        )
        self.assertEqual(
            {entry["name"] for entry in FROZEN_UPSTREAM_TOOLS},
            tool_policy.UPSTREAM_TOOL_SET,
        )

    def test_live_contract_is_verified_once_and_never_registers_dynamic_tools(
        self,
    ) -> None:
        transport = ContractTransport()
        client = UpstreamMCPClient("http://media-server:3000", transport=transport)
        self.assertEqual(client.verify_contract(), tool_policy.UPSTREAM_TOOLS)
        self.assertEqual(client.list_tools(), tool_policy.UPSTREAM_TOOLS)
        self.assertEqual(client.registered_tools, tool_policy.UPSTREAM_TOOLS)
        self.assertEqual(len(transport.calls), 2)
        methods = [json.loads(call[3])["method"] for call in transport.calls]
        self.assertEqual(methods, ["initialize", "tools/list"])
        self.assertEqual(transport.redirects, [False, False])
        self.assertTrue(all(call[4][0] <= 3.0 for call in transport.calls))
        self.assertTrue(all(0.0 < call[4][1] <= 15.0 for call in transport.calls))
        self.assertNotIn("Mcp-Name", transport.calls[0][2])
        self.assertNotIn("Mcp-Name", transport.calls[1][2])

    def test_live_contract_denies_missing_extra_and_schema_drift(self) -> None:
        cases: list[list[dict[str, object]]] = []
        missing = cast(list[dict[str, object]], _plain(FROZEN_UPSTREAM_TOOLS))
        missing.pop()
        cases.append(missing)
        extra = cast(list[dict[str, object]], _plain(FROZEN_UPSTREAM_TOOLS))
        extra.append(dict(extra[0]))
        extra[-1]["name"] = "future_unreviewed_tool"
        cases.append(extra)
        unknown_field = cast(list[dict[str, object]], _plain(FROZEN_UPSTREAM_TOOLS))
        unknown_field[0]["futureField"] = True
        cases.append(unknown_field)
        drifted = cast(list[dict[str, object]], _plain(FROZEN_UPSTREAM_TOOLS))
        schema = cast(dict[str, object], drifted[0]["inputSchema"])
        properties = cast(dict[str, object], schema["properties"])
        term = cast(dict[str, object], properties["term"])
        term["type"] = "integer"
        cases.append(drifted)
        for tools in cases:
            transport = ContractTransport(tools)
            client = UpstreamMCPClient("http://media-server:3000", transport=transport)
            with self.assertRaises(UpstreamContractError) as context:
                client.verify_contract()
            self.assertIsNone(context.exception.__cause__)
            self.assertIsNone(context.exception.__context__)
            self.assertIsNone(context.exception.__traceback__)
            self.assertEqual(len(transport.calls), 2)

    def test_live_contract_pagination_is_bounded(self) -> None:
        class EndlessTransport(ContractTransport):
            def request(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                data: bytes,
                timeout: tuple[float, float],
                allow_redirects: bool,
            ) -> object:
                self.calls.append((method, url, headers, data, timeout))
                self.redirects.append(allow_redirects)
                request = json.loads(data)
                if request["method"] == "initialize":
                    result: dict[str, object] = {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                    }
                else:
                    result = {"tools": [], "nextCursor": "again"}
                return FakeResponse(
                    {"jsonrpc": "2.0", "id": request["id"], "result": result}
                )

        transport = EndlessTransport()
        client = UpstreamMCPClient("http://media-server:3000", transport=transport)
        with self.assertRaises(UpstreamContractError):
            client.verify_contract()
        self.assertEqual(len(transport.calls), 3)  # initialize plus two repeated pages

    def test_async_live_contract_uses_same_bounded_transport_contract(self) -> None:
        class AsyncContractTransport(ContractTransport):
            async def request(
                self,
                method: str,
                url: str,
                *,
                headers: dict[str, str],
                data: bytes,
                timeout: tuple[float, float],
                allow_redirects: bool,
            ) -> object:
                return super().request(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    timeout=timeout,
                    allow_redirects=allow_redirects,
                )

        transport = AsyncContractTransport()

        async def run() -> tuple[str, ...]:
            client = UpstreamMCPClient("http://media-server:3000", transport=transport)
            return await client.verify_contract_async()

        self.assertEqual(asyncio.run(run()), tool_policy.UPSTREAM_TOOLS)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.redirects, [False, False])

    def test_call_is_typed_bounded_and_redacted(self) -> None:
        transport = RecordingTransport(FakeResponse(self._result()))
        client = UpstreamMCPClient(
            "http://media-server:3000",
            token="secret-token",
            transport=transport,
        )
        result = client.call_tool("plex_get_libraries", {"include": "all"})
        self.assertIsInstance(result, MCPToolResult)
        self.assertIn("<redacted-path>", result.text)
        assert result.structured_content is not None
        self.assertNotIn("apiKey", result.structured_content)
        self.assertNotIn("rootFolder", result.structured_content)
        self.assertNotIn("providerUrl", result.structured_content)
        self.assertNotIn("secret", json.dumps(result.to_dict()))
        self.assertNotIn("media-server", repr(client))
        self.assertNotIn("secret-token", repr(client))
        method, _, headers, payload, timeout = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertEqual(timeout[0], 3.0)
        self.assertLessEqual(timeout[1], 15.0)
        self.assertGreater(timeout[1], 0.0)
        self.assertEqual(transport.redirects, [False])
        request = json.loads(payload)
        self.assertEqual(request["method"], "tools/call")
        self.assertEqual(headers["MCP-Protocol-Version"], "2025-11-25")
        # The pinned v2.3.0 image's 2025 compatibility path accepts the
        # ordinary tools/call params only.  A modelcontext _meta envelope
        # switches its validator to the modern 2026 shape and is rejected
        # unless clientCapabilities are supplied.
        self.assertEqual(
            request["params"],
            {"name": "plex_get_libraries", "arguments": {"include": "all"}},
        )
        self.assertNotIn("_meta", request["params"])

    def test_redaction_covers_canonical_and_camel_credential_assignments(self) -> None:
        for text in (
            "MCP_AUTH_TOKEN=SECRET",
            "RADARR_API_KEY=SECRET",
            "TELEGRAM_BOT_TOKEN=SECRET",
            "apiToken=SECRET",
            "key=SECRET",
        ):
            sanitized = redact_text(text)
            self.assertNotIn("SECRET", sanitized)
            self.assertIn("<redacted>", sanitized)

        sanitized_json = redact_json(
            {"token": "SECRET", "apiToken": "SECRET", "key": "identifier"}
        )
        assert isinstance(sanitized_json, dict)
        self.assertEqual(sanitized_json["token"], "<redacted>")
        self.assertEqual(sanitized_json["apiToken"], "<redacted>")
        # ``key`` remains a useful non-secret identifier in typed structures;
        # assignment syntax above is scrubbed conservatively as credential
        # material.
        self.assertEqual(sanitized_json["key"], "identifier")

    def test_secret_selector_is_exact_and_never_uses_sole_wrong_assignment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upstream.env"
            path.write_text("OTHER=wrong-secret\n", encoding="utf-8")
            reference = SecretFileRef(path, key="MCP_AUTH_TOKEN")
            with self.assertRaises(ValueError) as context:
                _load_token(None, reference)
            self.assertIsNone(context.exception.__cause__)
            self.assertNotIn("wrong-secret", str(context.exception))
            with self.assertRaises(ValueError):
                _load_token(None, path, names=("MCP_AUTH_TOKEN",))

    def test_secret_file_reference_must_be_canonical(self) -> None:
        with self.assertRaises(ValueError):
            _load_token(None, "relative-upstream.env")
        with self.assertRaises(ValueError):
            _load_token(None, "file://remote.example/upstream.env")

    def test_timeout_validation_rejects_non_finite_values(self) -> None:
        with self.assertRaises(ValueError):
            UpstreamMCPClient("http://media-server:3000", total_timeout=math.nan)
        with self.assertRaises(ValueError):
            CircuitBreaker(recovery_seconds=math.nan)

    def test_response_limits_and_type_contract(self) -> None:
        oversized = FakeResponse(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "x" * (256 * 1024)}]},
            }
        )
        client = UpstreamMCPClient(
            "http://media-server:3000", transport=RecordingTransport(oversized)
        )
        with self.assertRaises(ResponseSizeError):
            client.call_tool("plex_get_libraries")

        bad = FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {"content": "raw"}})
        client = UpstreamMCPClient(
            "http://media-server:3000", transport=RecordingTransport(bad)
        )
        with self.assertRaises(ResponseValidationError):
            client.call_tool("plex_get_libraries")

    def test_streamable_sse_response_is_bounded_and_typed(self) -> None:
        response = FakeResponse(self._result(), content_type="text/event-stream")
        response.content = b"event: message\ndata: " + response.content + b"\n\n"
        client = UpstreamMCPClient(
            "http://media-server:3000", transport=RecordingTransport(response)
        )
        self.assertTrue(client.call_tool("plex_get_libraries").text.startswith("Movie"))

    def test_circuit_opens_after_five_failures_and_recovers(self) -> None:
        now = [0.0]
        breaker = CircuitBreaker(clock=lambda: now[0])
        client = UpstreamMCPClient(
            "http://media-server:3000",
            transport=FailingTransport(),
            circuit_breaker=breaker,
            clock=lambda: now[0],
        )
        for _ in range(5):
            with self.assertRaises(UpstreamTransportError):
                client.call_tool("plex_get_libraries")
        self.assertTrue(breaker.is_open)
        with self.assertRaises(CircuitOpenError):
            client.call_tool("plex_get_libraries")
        now[0] += 30
        # The half-open probe is allowed; this transport fails and re-opens it.
        with self.assertRaises(UpstreamTransportError):
            client.call_tool("plex_get_libraries")
        self.assertTrue(breaker.is_open)

    def test_half_open_probe_is_released_when_gate_times_out(self) -> None:
        now = [0.0]
        breaker = CircuitBreaker(clock=lambda: now[0])
        transport = RecordingTransport(FakeResponse(self._result()))
        client = UpstreamMCPClient(
            "http://media-server:3000",
            transport=transport,
            circuit_breaker=breaker,
            total_timeout=0.01,
            connect_timeout=0.01,
            max_concurrency=1,
            clock=lambda: now[0],
        )
        for _ in range(5):
            breaker.record_failure()
        now[0] = 30.0
        self.assertTrue(client.concurrency.acquire(timeout=0))
        try:
            with self.assertRaises(DeadlineExceededError):
                client.call_tool("plex_get_libraries")
            self.assertEqual(breaker.state, "open")
        finally:
            client.concurrency.release()

        # Once the old gate owner releases, a later recovery window can make
        # a fresh probe instead of being stranded in HALF_OPEN forever.
        now[0] = 60.0
        transport.response = FakeResponse(self._result(request_id=2))
        self.assertTrue(client.call_tool("plex_get_libraries").text.startswith("Movie"))

    def test_concurrency_ceiling_is_hard_bounded(self) -> None:
        with self.assertRaises(ValueError):
            ConcurrencyLimiter(9)
        limiter = ConcurrencyLimiter(1)
        self.assertTrue(limiter.acquire(timeout=0))
        self.assertEqual(limiter.active, 1)
        self.assertFalse(limiter.acquire(timeout=0))
        limiter.release()
        self.assertEqual(limiter.active, 0)

    def test_async_transport_uses_same_typed_result(self) -> None:
        class AsyncTransport:
            def __init__(self) -> None:
                self.redirects: list[bool] = []

            async def request(self, *args: object, **kwargs: object) -> object:
                allow_redirects = kwargs["allow_redirects"]
                assert isinstance(allow_redirects, bool)
                self.redirects.append(allow_redirects)
                return FakeResponse(self._payload)

            _payload: ClassVar[dict[str, object]] = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "ok"}]},
            }

        transport = AsyncTransport()

        async def run() -> MCPToolResult:
            client = UpstreamMCPClient("http://media-server:3000", transport=transport)
            return await client.call_tool_async("plex_get_libraries")

        result = asyncio.run(run())
        self.assertEqual(result.text, "ok")
        self.assertEqual(transport.redirects, [False])

    def test_custom_transport_error_has_no_provider_cause(self) -> None:
        class CauseTransport:
            def request(self, *args: object, **kwargs: object) -> object:
                raise OSError("https://provider.invalid/run/secrets/token")

        client = UpstreamMCPClient(
            "http://media-server:3000", transport=CauseTransport()
        )
        with self.assertRaises(UpstreamTransportError) as context:
            client.call_tool("plex_get_libraries")
        self.assertIsNone(context.exception.__cause__)
        self.assertIsNone(context.exception.__context__)
        self.assertIsNone(context.exception.__traceback__)
        self.assertNotIn("provider.invalid", str(context.exception))
        self.assertNotIn("/run/secrets", str(context.exception))

    def test_custom_transport_timeout_is_typed_as_deadline(self) -> None:
        class TimeoutTransport:
            def request(self, *args: object, **kwargs: object) -> object:
                raise requests.Timeout("provider URL and credential")

        client = UpstreamMCPClient(
            "http://media-server:3000", transport=TimeoutTransport()
        )
        with self.assertRaises(DeadlineExceededError) as context:
            client.call_tool("plex_get_libraries")
        self.assertIsNone(context.exception.__cause__)
        self.assertIsNone(context.exception.__context__)
        self.assertIsNone(context.exception.__traceback__)

    def test_redirected_response_is_denied_even_if_transport_ignored_flag(self) -> None:
        response = FakeResponse(self._result())
        setattr(response, "history", [object()])
        client = UpstreamMCPClient(
            "http://media-server:3000",
            transport=RecordingTransport(response),
        )
        with self.assertRaises(UpstreamTransportError) as context:
            client.call_tool("plex_get_libraries")
        self.assertIsNone(context.exception.__cause__)
        self.assertNotIn("media-server", str(context.exception))

    def test_stream_deadline_checked_before_next_chunk(self) -> None:
        now = [0.0]

        class AdvancingResponse:
            status_code = 200
            headers = {"content-type": "application/json"}

            def close(self) -> None:
                return

            def iter_content(self, *, chunk_size: int) -> object:
                def chunks() -> object:
                    yield b"{"  # The deadline advances before the next read.
                    now[0] = 1.0
                    yield b"}"

                return chunks()

        client = UpstreamMCPClient(
            "http://media-server:3000",
            transport=RecordingTransport(AdvancingResponse()),
            total_timeout=1.0,
            connect_timeout=1.0,
            clock=lambda: now[0],
        )
        with self.assertRaises(DeadlineExceededError):
            client.call_tool("plex_get_libraries")

    def test_stream_timeout_maps_to_deadline_without_cause(self) -> None:
        class TimeoutResponse:
            status_code = 200
            headers = {"content-type": "application/json"}

            def close(self) -> None:
                return

            def iter_content(self, *, chunk_size: int) -> object:
                def chunks() -> object:
                    raise requests.Timeout("https://provider.invalid/secret")
                    yield b"unreachable"

                return chunks()

        client = UpstreamMCPClient(
            "http://media-server:3000",
            transport=RecordingTransport(TimeoutResponse()),
        )
        with self.assertRaises(DeadlineExceededError) as context:
            client.call_tool("plex_get_libraries")
        self.assertIsNone(context.exception.__cause__)

    def test_async_default_requests_worker_keeps_gate_until_completion(self) -> None:
        session = BlockingSession(FakeResponse(self._result()))
        client = UpstreamMCPClient(
            "http://media-server:3000", session=cast(requests.Session, session)
        )

        async def run() -> None:
            task = asyncio.create_task(client.call_tool_async("plex_get_libraries"))
            self.assertTrue(await asyncio.to_thread(session.entered.wait, 1))
            self.assertEqual(client.concurrency.active, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            # Cancellation stops the caller but cannot release the gate while
            # the blocking requests worker still owns it.
            self.assertEqual(client.concurrency.active, 1)
            session.release.set()
            self.assertTrue(await asyncio.to_thread(session.finished.wait, 1))
            for _ in range(100):
                if client.concurrency.active == 0:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(client.concurrency.active, 0)

        asyncio.run(run())

    def test_async_half_open_probe_cancellation_reopens_circuit(self) -> None:
        now = [0.0]
        breaker = CircuitBreaker(clock=lambda: now[0])
        transport = RecordingTransport(FakeResponse(self._result()))
        client = UpstreamMCPClient(
            "http://media-server:3000",
            transport=transport,
            circuit_breaker=breaker,
            total_timeout=0.2,
            connect_timeout=0.2,
            max_concurrency=1,
            clock=lambda: now[0],
        )
        for _ in range(5):
            breaker.record_failure()
        now[0] = 30.0

        async def run() -> None:
            self.assertTrue(client.concurrency.acquire(timeout=0))
            task = asyncio.create_task(client.call_tool_async("plex_get_libraries"))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(breaker.state, "open")
            client.concurrency.release()

        asyncio.run(run())

    def test_async_half_open_probe_cancellation_after_acquire_reopens_circuit(
        self,
    ) -> None:
        now = [0.0]
        breaker = CircuitBreaker(clock=lambda: now[0])

        class SlowTransport:
            def __init__(self) -> None:
                self.started = asyncio.Event()

            async def request(self, *args: object, **kwargs: object) -> object:
                self.started.set()
                await asyncio.Event().wait()
                return FakeResponse(self._payload)

            _payload: ClassVar[dict[str, object]] = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "ok"}]},
            }

        transport = SlowTransport()
        client = UpstreamMCPClient(
            "http://media-server:3000",
            transport=transport,
            circuit_breaker=breaker,
            total_timeout=1.0,
            connect_timeout=1.0,
            max_concurrency=1,
            clock=lambda: now[0],
        )
        for _ in range(5):
            breaker.record_failure()
        now[0] = 30.0

        async def run() -> None:
            task = asyncio.create_task(client.call_tool_async("plex_get_libraries"))
            await transport.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(breaker.state, "open")
            self.assertEqual(client.concurrency.active, 0)

        asyncio.run(run())

    def test_env_file_credential_source_is_supported_without_repr_leak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upstream.env"
            path.write_text(
                "MCP_AUTH_TOKEN=env-secret\nOTHER=value\n", encoding="utf-8"
            )
            transport = RecordingTransport(FakeResponse(self._result()))
            client = UpstreamMCPClient(
                "http://media-server:3000",
                token_file=path,
                transport=transport,
            )
            client.call_tool("plex_get_libraries")
            self.assertEqual(
                transport.calls[0][2]["Authorization"], "Bearer env-secret"
            )
            self.assertNotIn("env-secret", repr(client))

    def test_radarr_queue_fallback_is_typed_and_has_no_provider_fields(self) -> None:
        transport = RecordingTransport(
            FakeResponse(
                {
                    "records": [
                        {
                            "id": 99,
                            "title": "Movie",
                            "status": "downloading",
                            "progress": 42,
                            "movieFile": {"path": "/media/movie.mkv"},
                            "errorMessage": "failed at /run/secrets/radarr.key",
                        }
                    ],
                    "totalRecords": 1,
                }
            )
        )
        adapter = RadarrQueueAdapter(
            "http://radarr:7878", api_key="radarr-secret", transport=transport
        )
        page = adapter.get_queue()
        self.assertEqual(len(page.items), 1)
        item = page.items[0]
        self.assertEqual(item.title, "Movie")
        self.assertEqual(item.progress_percent, 42.0)
        self.assertNotIn("radarr-secret", repr(item))
        self.assertNotIn("/media", repr(item))
        self.assertIn("<redacted-path>", item.error or "")
        self.assertEqual(transport.calls[0][2]["X-Api-Key"], "radarr-secret")
        self.assertEqual(transport.redirects, [False])

    def test_radarr_aggregate_snapshot_has_a_serialized_size_bound(self) -> None:
        records = [{"title": "queue-item", "status": "downloading"}] * 5_000

        class QueueTransport:
            def __init__(self) -> None:
                self.redirects: list[bool] = []

            def request(self, *args: object, **kwargs: object) -> object:
                allow_redirects = kwargs["allow_redirects"]
                assert isinstance(allow_redirects, bool)
                self.redirects.append(allow_redirects)
                return {"records": records, "totalRecords": len(records)}

        transport = QueueTransport()
        adapter = RadarrQueueAdapter(
            "http://radarr:7878", api_key="radarr-secret", transport=transport
        )
        page = adapter.get_queue(limit=250)
        self.assertTrue(page.truncated)
        self.assertLess(len(page.items), len(records))
        self.assertIsNotNone(page.as_of)
        assert page.as_of is not None
        self.assertLessEqual(
            _queue_page_wire_size(
                page.items,
                as_of=page.as_of,
                next_cursor=page.next_cursor,
                truncated=page.truncated,
                total=page.total,
            ),
            256 * 1024,
        )
        self.assertEqual(transport.redirects, [False])


if __name__ == "__main__":
    unittest.main()
