"""Typed HTTP adapters for the pinned media-server-mcp v2.3.0 service.

The upstream service is deliberately kept behind this narrow boundary:

* only the exact 102 reviewed tool names are accepted;
* JSON-RPC envelopes are parsed into typed records rather than returned raw;
* response bodies and normalized text are bounded before and after parsing;
* credentials, paths, and provider URLs are scrubbed from selected output and
  never appear in exception text; and
* sync ``requests`` and async call sites share deadlines, a dependency
  circuit breaker, and an eight-call concurrency ceiling.

``radarr_get_queue`` is intentionally *not* added to the upstream allowlist:
v2.3.0 advertises that name in profile metadata but does not register it.  The
companion's :class:`RadarrQueueAdapter` supplies a typed, sanitized fallback
against Radarr's native queue endpoint.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import TracebackType
from typing import Any, Final, Self, TypeAlias, cast
from urllib.parse import urlencode

import requests

from .. import tool_policy
from ..auth import canonical_json
from ..config import SecretFileRef, normalize_url
from ..errors import DependencyError
from ..models import Page, QueueItem, QueueState, ServiceName
from ..redaction import (
    is_path_key,
    is_secret_key,
    is_url_key,
    redact_json,
    redact_text,
)
from ..upstream_contract import (
    FROZEN_UPSTREAM_TOOL_NAMES,
    UPSTREAM_TOOL_CONTRACT_SHA256,
    canonical_tool_digest,
    validate_live_tools,
)

# Contract and bound constants.  Keep these names public so deployment/tests
# can assert the pinned behavior without importing implementation details.
UPSTREAM_VERSION: Final[str] = tool_policy.UPSTREAM_VERSION
UPSTREAM_REVISION: Final[str] = tool_policy.UPSTREAM_REVISION
UPSTREAM_MAX_RESPONSE_BYTES: Final[int] = 256 * 1024
MAX_RESPONSE_BYTES: Final[int] = UPSTREAM_MAX_RESPONSE_BYTES
MAX_REQUEST_BYTES: Final[int] = 64 * 1024
DEFAULT_CONNECT_TIMEOUT_SECONDS: Final[float] = 3.0
DEFAULT_TOTAL_TIMEOUT_SECONDS: Final[float] = 15.0
MAX_CONNECT_TIMEOUT_SECONDS: Final[float] = 3.0
MAX_TOTAL_TIMEOUT_SECONDS: Final[float] = 15.0
MAX_CONCURRENT_CALLS: Final[int] = 8
DEFAULT_CIRCUIT_FAILURE_THRESHOLD: Final[int] = 5
DEFAULT_CIRCUIT_OPEN_SECONDS: Final[float] = 30.0
DEFAULT_PAGE_SIZE: Final[int] = 100
MAX_PAGE_SIZE: Final[int] = 250
MAX_QUEUE_ITEMS: Final[int] = 5_000
MAX_QUEUE_ERROR_BYTES: Final[int] = 2 * 1024
MAX_CREDENTIAL_BYTES: Final[int] = 16 * 1024
MAX_CONTRACT_TOOLS: Final[int] = 102
MAX_CONTRACT_PAGES: Final[int] = 8
_JSONRPC_VERSION: Final[str] = "2.0"
# v2.3.0 uses the stateless Streamable HTTP transport from MCP SDK 2.x.  The
# mirrored headers and request metadata are part of that pinned HTTP contract.
MCP_PROTOCOL_VERSION: Final[str] = "2026-07-28"
UPSTREAM_MCP_PROTOCOL_VERSION: Final[str] = MCP_PROTOCOL_VERSION


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class UpstreamMCPError(DependencyError):
    """Base class for safe, typed upstream adapter failures."""


class ToolNotAllowedError(UpstreamMCPError):
    """The requested name is not part of the pinned upstream contract."""


class DeadlineExceededError(UpstreamMCPError):
    """The connect/read operation exceeded the total HTTP deadline."""


class ResponseSizeError(UpstreamMCPError):
    """An upstream response exceeded the bounded body/result size."""


class ResponseValidationError(UpstreamMCPError):
    """An upstream response was not valid bounded JSON of the expected shape."""


class UpstreamProtocolError(ResponseValidationError):
    """The response was JSON but not a valid MCP JSON-RPC result."""


class UpstreamTransportError(UpstreamMCPError):
    """A network/session failure occurred without exposing its details."""


class UpstreamContractError(UpstreamMCPError):
    """The live upstream did not match the frozen reviewed contract."""


class UpstreamHTTPError(UpstreamMCPError):
    """The dependency returned a non-success HTTP status."""

    def __init__(self, status_code: int, *, retry_after: int | None = None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        suffix = "" if retry_after is None else "; retry later"
        super().__init__(f"upstream HTTP status {status_code}{suffix}")


class CircuitOpenError(UpstreamMCPError):
    """Calls are temporarily disabled after repeated dependency failures."""

    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__("upstream dependency circuit is open")


class _CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


TransportCallable: TypeAlias = Callable[..., object]


def _validate_positive_number(value: object, *, name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > maximum:
        raise ValueError(f"{name} must be between 0 and {maximum:g} seconds")
    return result


def _validate_limit(value: object, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if value > maximum:
        raise ValueError(f"{name} exceeds the configured bound")
    return value


def _safe_json(
    value: object, *, max_bytes: int = UPSTREAM_MAX_RESPONSE_BYTES
) -> JsonValue:
    """Validate an already parsed JSON-like object and redact it."""

    if (
        isinstance(value, (Mapping, list, tuple, str, int, float, bool))
        or value is None
    ):
        try:
            redacted = redact_json(value, max_bytes=max_bytes)
        except (TypeError, ValueError):
            raise ResponseValidationError(
                "upstream response could not be sanitized"
            ) from None
        return cast(JsonValue, redacted)
    raise ResponseValidationError("upstream response contains an unsupported value")


# This is intentionally a conservative structural allowlist rather than a
# generic recursive pass-through.  Upstream's 102 tools have heterogeneous
# provider schemas; only fields useful for an operator-facing summary cross
# this adapter.  New fields stay denied until explicitly reviewed.
_SAFE_STRUCTURED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "key",
        "name",
        "title",
        "originalTitle",
        "sortTitle",
        "year",
        "overview",
        "summary",
        "description",
        "status",
        "state",
        "type",
        "mediaType",
        "kind",
        "available",
        "monitored",
        "isMonitored",
        "isAvailable",
        "quality",
        "resolution",
        "codec",
        "container",
        "bitrate",
        "progress",
        "progressPercent",
        "percentage",
        "eta",
        "etaSeconds",
        "season",
        "seasonNumber",
        "episode",
        "episodeNumber",
        "episodeCount",
        "count",
        "total",
        "totalRecords",
        "page",
        "pageSize",
        "records",
        "items",
        "results",
        "data",
        "movie",
        "series",
        "episodeFile",
        "movieFile",
        "genres",
        "tags",
        "credits",
        "cast",
        "crew",
        "releaseDate",
        "airDate",
        "firstAired",
        "lastAired",
        "createdAt",
        "updatedAt",
        "timestamp",
        "success",
        "message",
        "error",
        "code",
        "warnings",
        "notes",
    }
)


def _safe_structured_key(key: str) -> bool:
    if is_secret_key(key) or is_path_key(key) or is_url_key(key):
        return False
    if key in _SAFE_STRUCTURED_KEYS:
        return True
    # Accept case/casing variants of a reviewed spelling without accepting a
    # new semantic field.  This handles e.g. ``total_records`` and
    # ``totalRecords`` while keeping ``rawProviderObject`` denied.
    compact = "".join(character for character in key if character.isalnum()).lower()
    return any(
        compact
        == "".join(character for character in allowed if character.isalnum()).lower()
        for allowed in _SAFE_STRUCTURED_KEYS
    )


def _sanitize_structured(value: object, *, depth: int = 0) -> JsonValue:
    if depth > 32:
        raise ResponseValidationError(
            "upstream structured content is too deeply nested"
        )
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, child in value.items():
            if isinstance(key, str) and _safe_structured_key(key):
                result[key] = _sanitize_structured(child, depth=depth + 1)
        return result
    if isinstance(value, list):
        if len(value) > 10_000:
            raise ResponseSizeError(
                "upstream structured content exceeds the item limit"
            )
        return [_sanitize_structured(child, depth=depth + 1) for child in value]
    if value is None or isinstance(value, (bool, int, float)):
        return cast(JsonValue, value)
    if isinstance(value, str):
        return redact_text(value)
    raise ResponseValidationError(
        "upstream structured content contains an unsupported value"
    )


def _object_pairs_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ResponseValidationError("upstream response contains duplicate fields")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ResponseValidationError("upstream response contains a non-finite number")


def _validate_json_tree(
    value: object, *, depth: int = 0, count: list[int] | None = None
) -> None:
    counter = [0] if count is None else count
    if depth > 64:
        raise ResponseValidationError("upstream response nesting exceeds the bound")
    counter[0] += 1
    if counter[0] > 50_000:
        raise ResponseValidationError("upstream response contains too many values")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            try:
                value.encode("utf-8", "strict")
            except UnicodeEncodeError:
                raise ResponseValidationError(
                    "upstream response contains invalid Unicode"
                ) from None
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ResponseValidationError(
                "upstream response contains a non-finite number"
            )
        return
    if isinstance(value, list):
        if len(value) > 10_000:
            raise ResponseValidationError("upstream response array exceeds the bound")
        for child in value:
            _validate_json_tree(child, depth=depth + 1, count=counter)
        return
    if isinstance(value, dict):
        if len(value) > 10_000:
            raise ResponseValidationError("upstream response object exceeds the bound")
        for key, child in value.items():
            if not isinstance(key, str):
                raise ResponseValidationError("upstream response has a non-text key")
            _validate_json_tree(child, depth=depth + 1, count=counter)
        return
    raise ResponseValidationError("upstream response contains an unsupported value")


def _parse_json_body(body: bytes) -> JsonValue:
    try:
        text = body.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise ResponseValidationError("upstream response is not valid UTF-8") from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except ResponseValidationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ResponseValidationError("upstream response is not valid JSON") from None
    _validate_json_tree(value)
    return cast(JsonValue, value)


def _parse_mcp_body(body: bytes, *, content_type: str | None = None) -> JsonValue:
    """Parse JSON or a bounded Streamable-HTTP SSE result.

    Streamable HTTP may choose ``application/json`` for a single response or
    ``text/event-stream`` when the server emits an event.  The adapter only
    accepts JSON ``data:`` events and selects the last complete event; comment,
    retry, and non-JSON event fields are ignored without being surfaced.
    """

    if content_type is None or "text/event-stream" not in content_type.lower():
        return _parse_json_body(body)
    try:
        text = body.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise ResponseValidationError(
            "upstream event stream is not valid UTF-8"
        ) from None
    events: list[JsonValue] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].lstrip()
        if not data or data == "[DONE]":
            continue
        try:
            events.append(_parse_json_body(data.encode("utf-8")))
        except ResponseValidationError:
            # Ignore a non-MCP event but never forward its text.  If there is
            # no valid event at all, the caller receives one typed protocol
            # error below.
            continue
    if not events:
        raise UpstreamProtocolError("upstream event stream has no JSON result")
    return events[-1]


def _header_value(headers: Mapping[object, object], name: str) -> str | None:
    lowered = name.lower()
    for raw_name, raw_value in headers.items():
        if str(raw_name).lower() == lowered:
            return str(raw_value)
    return None


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            # Closing is cleanup only; never replace the typed dependency
            # result with an implementation-specific close error.
            return


def _reject_redirected_response(response: object, *, endpoint: str) -> None:
    """Reject a transport that followed a redirect despite the hard contract."""

    history = getattr(response, "history", None)
    if isinstance(history, (list, tuple)) and history:
        _close_response(response)
        raise UpstreamTransportError("upstream redirects are not allowed")
    response_url = getattr(response, "url", None)
    if isinstance(response_url, str) and response_url:
        try:
            normalized = normalize_url(response_url, field_name="upstream response URL")
        except Exception:
            _close_response(response)
            raise UpstreamTransportError("upstream response URL is invalid") from None
        if normalized != endpoint:
            _close_response(response)
            raise UpstreamTransportError("upstream response changed origin")


def _clear_exception_chain(error: BaseException) -> None:
    """Detach provider exceptions from a public safe adapter error."""

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        cause = current.__cause__
        context = current.__context__
        current.__cause__ = None
        current.__context__ = None
        current.__suppress_context__ = True
        current.__traceback__ = None
        current = cause if cause is not None else context


def _response_body(
    response: object, *, max_bytes: int, deadline: float, clock: Callable[[], float]
) -> bytes:
    """Read a requests/fake response without trusting Content-Length alone."""

    headers: Mapping[object, object] = {}
    if hasattr(response, "headers"):
        raw_headers = response.headers
        if isinstance(raw_headers, Mapping):
            headers = raw_headers
    content_length = _header_value(headers, "content-length")
    if content_length is not None:
        try:
            parsed_length = int(content_length)
        except (TypeError, ValueError):
            raise ResponseValidationError(
                "upstream response has an invalid length"
            ) from None
        if parsed_length < 0 or parsed_length > max_bytes:
            raise ResponseSizeError("upstream response exceeds the body limit")

    def append_chunk(chunks: list[bytes], total: list[int], chunk: object) -> None:
        if isinstance(chunk, str):
            try:
                encoded = chunk.encode("utf-8", "strict")
            except UnicodeEncodeError:
                raise ResponseValidationError(
                    "upstream response is not valid UTF-8"
                ) from None
        elif isinstance(chunk, (bytes, bytearray, memoryview)):
            encoded = bytes(chunk)
        else:
            raise ResponseValidationError("upstream response body has an invalid chunk")
        total[0] += len(encoded)
        if total[0] > max_bytes:
            raise ResponseSizeError("upstream response exceeds the body limit")
        chunks.append(encoded)
        if clock() >= deadline:
            raise DeadlineExceededError("upstream request exceeded its deadline")

    chunks: list[bytes] = []
    total = [0]
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        try:
            stream = iterator(chunk_size=16 * 1024)
            stream_iterator = iter(stream)
            while True:
                # Check before asking the provider iterator for another
                # chunk.  This closes the old loophole where a stream could
                # yield one final oversized/late chunk after the deadline.
                if clock() >= deadline:
                    raise DeadlineExceededError(
                        "upstream request exceeded its deadline"
                    )
                try:
                    chunk = next(stream_iterator)
                except StopIteration:
                    break
                append_chunk(chunks, total, chunk)
            if clock() >= deadline:
                raise DeadlineExceededError("upstream request exceeded its deadline")
            return b"".join(chunks)
        except (ResponseSizeError, ResponseValidationError, DeadlineExceededError):
            raise
        except requests.Timeout:
            raise DeadlineExceededError(
                "upstream request exceeded its deadline"
            ) from None
        except (requests.RequestException, OSError, TypeError):
            raise UpstreamTransportError(
                "upstream response could not be read"
            ) from None

    raw_content = getattr(response, "content", None)
    if raw_content is None:
        raw_content = getattr(response, "text", None)
    append_chunk(chunks, total, raw_content)
    return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class MCPToolContent:
    """The only MCP content form admitted by this adapter: bounded text."""

    text: str
    type: str = "text"

    def __post_init__(self) -> None:
        if self.type != "text":
            raise ResponseValidationError("unsupported MCP content type")
        if not isinstance(self.text, str) or not self.text:
            raise ResponseValidationError("MCP text content must be non-empty")
        object.__setattr__(self, "text", redact_text(self.text))

    @property
    def value(self) -> str:
        return self.text

    @property
    def kind(self) -> str:
        return self.type


# Concise aliases for callers that use MCP's content terminology.
MCPContent = MCPToolContent


@dataclass(frozen=True, slots=True)
class MCPToolResult:
    """Typed, sanitized result from one reviewed upstream tool call."""

    request_id: int | str | None
    content: tuple[MCPToolContent, ...] = ()
    structured_content: JsonObject | None = None
    is_error: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.request_id, bool) or not (
            self.request_id is None or isinstance(self.request_id, (int, str))
        ):
            raise ResponseValidationError("MCP response id has an invalid type")
        items = tuple(self.content)
        if any(not isinstance(item, MCPToolContent) for item in items):
            raise ResponseValidationError("MCP content is not typed text")
        object.__setattr__(self, "content", items)
        if self.structured_content is not None:
            if not isinstance(self.structured_content, dict):
                raise ResponseValidationError(
                    "MCP structured content must be an object"
                )
            object.__setattr__(
                self,
                "structured_content",
                cast(JsonObject, _safe_json(self.structured_content)),
            )
        if not isinstance(self.is_error, bool):
            raise ResponseValidationError("MCP error flag has an invalid type")

    @property
    def structuredContent(self) -> JsonObject | None:
        """Camel-case compatibility property matching the MCP wire name."""

        return self.structured_content

    @property
    def structured(self) -> JsonObject | None:
        return self.structured_content

    @property
    def text(self) -> str:
        return "\n".join(item.text for item in self.content)

    def to_dict(self) -> JsonObject:
        result: JsonObject = {
            "request_id": self.request_id,
            "content": [
                {"type": item.type, "text": item.text} for item in self.content
            ],
            "is_error": self.is_error,
        }
        if self.structured_content is not None:
            result["structuredContent"] = self.structured_content
        return result


MCPResponse = MCPToolResult
MCPResult = MCPToolResult


class ConcurrencyLimiter:
    """A sync/async-friendly bounded dependency call gate."""

    def __init__(self, maximum: int = MAX_CONCURRENT_CALLS) -> None:
        self.maximum = _validate_limit(
            maximum, name="maximum concurrency", maximum=MAX_CONCURRENT_CALLS
        )
        self._semaphore = threading.BoundedSemaphore(self.maximum)
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def acquire(self, timeout: float | None = None) -> bool:
        acquired = self._semaphore.acquire(timeout=timeout)
        if acquired:
            with self._lock:
                self._active += 1
        return acquired

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("concurrency limiter released without an owner")
            self._active -= 1
        self._semaphore.release()

    def __enter__(self) -> Self:
        if not self.acquire():
            raise RuntimeError("concurrency limiter is unavailable")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    async def acquire_async(self, timeout: float | None = None) -> bool:
        # The sync semaphore is shared by sync and async calls.  Running the
        # blocking acquire off-loop avoids starving other async tasks.  Keep
        # the worker shielded when the caller is cancelled: otherwise a
        # blocked worker could acquire the semaphore after cancellation and
        # leak the slot.
        worker = asyncio.create_task(asyncio.to_thread(self.acquire, timeout))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:

            def release_late_acquire(done: asyncio.Task[bool]) -> None:
                try:
                    acquired = done.result()
                except asyncio.CancelledError:
                    return
                except Exception:  # noqa: BLE001
                    return
                if acquired:
                    self.release()

            worker.add_done_callback(release_late_acquire)
            raise

    async def release_async(self) -> None:
        self.release()


# Names used by a few callers to describe the same primitive.
ConcurrencyGate = ConcurrencyLimiter
DependencyConcurrency = ConcurrencyLimiter


class CircuitBreaker:
    """Five-failure/30-second dependency circuit breaker.

    ``clock`` is injectable for deterministic tests.  A half-open circuit
    permits one probe; concurrent probes remain denied until that probe records
    success or failure.
    """

    def __init__(
        self,
        failure_threshold: int = DEFAULT_CIRCUIT_FAILURE_THRESHOLD,
        recovery_seconds: float = DEFAULT_CIRCUIT_OPEN_SECONDS,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.failure_threshold = _validate_limit(
            failure_threshold, name="failure threshold", maximum=100
        )
        self.recovery_seconds = _validate_positive_number(
            recovery_seconds, name="recovery seconds", maximum=300.0
        )
        self._clock = time.monotonic if clock is None else clock
        self._lock = threading.Lock()
        self._state = _CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            self._refresh_locked(self._clock())
            return self._state.value

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failures

    @property
    def is_open(self) -> bool:
        return self.state == _CircuitState.OPEN.value

    def _refresh_locked(self, now: float) -> None:
        if (
            self._state is _CircuitState.OPEN
            and self._opened_at is not None
            and now - self._opened_at >= self.recovery_seconds
        ):
            self._state = _CircuitState.HALF_OPEN
            self._probe_in_flight = False

    def before_call(self) -> None:
        with self._lock:
            now = self._clock()
            self._refresh_locked(now)
            if self._state is _CircuitState.OPEN:
                remaining = None
                if self._opened_at is not None:
                    remaining = max(
                        0.0, self.recovery_seconds - (now - self._opened_at)
                    )
                raise CircuitOpenError(remaining)
            if self._state is _CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpenError(self.recovery_seconds)
                self._probe_in_flight = True

    # Short aliases make the primitive usable in generic dependency wrappers.
    allow = before_call
    check = before_call

    def record_success(self) -> None:
        with self._lock:
            self._state = _CircuitState.CLOSED
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    success = record_success

    def record_failure(self) -> None:
        with self._lock:
            now = self._clock()
            self._refresh_locked(now)
            if self._state is _CircuitState.HALF_OPEN:
                self._state = _CircuitState.OPEN
                self._opened_at = now
                self._probe_in_flight = False
                self._failures = self.failure_threshold
                return
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._state = _CircuitState.OPEN
                self._opened_at = now
                self._probe_in_flight = False

    failure = record_failure

    def abort_probe(self) -> None:
        """Release a half-open probe that never acquired the call gate.

        ``before_call`` reserves the single half-open probe before semaphore
        acquisition.  A timeout or cancellation while waiting for that gate
        therefore has to transition back to OPEN; otherwise the breaker can
        remain permanently half-open with no in-flight operation to complete
        it.
        """

        with self._lock:
            if self._state is _CircuitState.HALF_OPEN and self._probe_in_flight:
                now = self._clock()
                self._state = _CircuitState.OPEN
                self._opened_at = now
                self._failures = self.failure_threshold
                self._probe_in_flight = False

    def reset(self) -> None:
        self.record_success()


def _safe_normalize_url(value: object, *, field_name: str) -> str:
    try:
        return normalize_url(value, field_name=field_name)
    except Exception:
        error = ValueError(f"{field_name} is invalid")
    raise error


def _endpoint_url(base_url: str, suffix: str) -> str:
    canonical = _safe_normalize_url(base_url, field_name="dependency_url")
    if canonical.endswith(suffix):
        return canonical
    return canonical + suffix


def _validate_credential(value: object, *, source: str) -> str:
    """Validate a credential before it can reach an HTTP header.

    Credentials are opaque bytes to the provider, but they must not contain
    whitespace/control characters that could create header injection or hide
    an accidental multiline file.  The error deliberately names only the
    source kind, never the offending value.
    """

    if not isinstance(value, str):
        raise ValueError(f"{source} credential is invalid")
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{source} credential must be non-empty")
    try:
        encoded_length = len(candidate.encode("utf-8", "strict"))
    except UnicodeEncodeError:
        raise ValueError(f"{source} credential contains invalid characters") from None
    if encoded_length > MAX_CREDENTIAL_BYTES:
        raise ResponseSizeError(f"{source} credential exceeds its bound")
    if any(character.isspace() or ord(character) < 0x20 for character in candidate):
        raise ValueError(f"{source} credential contains invalid characters")
    return candidate


def _load_token(
    token: object,
    token_file: object,
    *,
    names: tuple[str, ...] = (),
) -> str | None:
    if token is not None and token_file is None and not isinstance(token, str):
        # SecretFileRef/Path is accepted as a convenience at this narrow I/O
        # boundary; callers still cannot pass a non-file object as a secret.
        token_file = token
        token = None
    if token is not None and token_file is not None:
        raise ValueError("provide one upstream credential source")
    if token is not None:
        return _validate_credential(token, source="upstream")
    if token_file is None:
        return None
    try:
        raw_path: object = getattr(token_file, "path", token_file)
        selector: object = getattr(token_file, "key", None)
    except Exception:
        reference_error: ValueError | None = ValueError(
            "upstream credential file reference is invalid"
        )
    else:
        reference_error = None
    if reference_error is not None:
        raise reference_error
    strict_selector = bool(names)
    if selector is not None:
        if not isinstance(selector, str) or not selector:
            raise ValueError("upstream credential selector is invalid")
        if names and selector.upper() not in {name.upper() for name in names}:
            raise ValueError("upstream credential selector is not allowed")
        names = (selector,)
        strict_selector = True
    try:
        reference = SecretFileRef.from_value(
            raw_path,
            field_name="upstream credential file",
            key=selector if isinstance(selector, str) else None,
        )
    except (TypeError, ValueError):
        # Do not echo an untrusted path/URI or preserve its implementation
        # exception as a traceback cause.
        reference_error = ValueError("upstream credential file reference is invalid")
    else:
        reference_error = None
    if reference_error is not None:
        raise reference_error
    try:
        content = reference.path.read_bytes()
    except (OSError, ValueError):
        read_error: UpstreamTransportError | None = UpstreamTransportError(
            "upstream credential could not be read"
        )
    else:
        read_error = None
    if read_error is not None:
        raise read_error
    if len(content) > MAX_CREDENTIAL_BYTES:
        raise ResponseSizeError("upstream credential file exceeds its bound")
    try:
        text = content.decode("utf-8", "strict")
    except UnicodeDecodeError:
        decode_error: ValueError | None = ValueError(
            "upstream credential file is not valid UTF-8"
        )
    else:
        decode_error = None
    if decode_error is not None:
        raise decode_error
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        raise ValueError("upstream credential file is empty")
    assignments: dict[str, str] = {}
    raw_lines: list[str] = []
    for line in lines:
        if "=" not in line:
            raw_lines.append(line)
            continue
        key, assignment = line.split("=", 1)
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        if not key:
            continue
        assignments[key] = assignment.strip().strip("\"'")
    value: str | None = None
    if assignments:
        wanted = {name.upper() for name in names}
        for key, candidate in assignments.items():
            if key.upper() in wanted:
                value = candidate
                break
        # A caller with a canonical selector/name set must never silently
        # select a different (even sole) assignment from a shared env file.
        # The unkeyed one-line form below remains supported because it is
        # unambiguous and is used by single-secret mounts.
        if value is None and len(assignments) == 1 and not strict_selector:
            value = next(iter(assignments.values()))
    elif len(raw_lines) == 1:
        value = raw_lines[0].strip().strip("\"'")
    if value is None:
        raise ValueError("upstream credential file has no recognized credential")
    return _validate_credential(value, source="upstream file")


class UpstreamMCPClient:
    """Synchronous and asynchronous client for upstream ``tools/call``."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        bearer_token: str | None = None,
        token_file: object | None = None,
        session: requests.Session | None = None,
        transport: TransportCallable | object | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        max_concurrency: int = MAX_CONCURRENT_CALLS,
        circuit_breaker: CircuitBreaker | None = None,
        response_limit: int = UPSTREAM_MAX_RESPONSE_BYTES,
        endpoint_suffix: str = "/mcp",
        clock: Callable[[], float] | None = None,
    ) -> None:
        if bearer_token is not None:
            if token is not None or token_file is not None:
                raise ValueError("provide one upstream credential source")
            token = bearer_token
        elif token is not None and token_file is not None:
            raise ValueError("provide one upstream credential source")
        if isinstance(token, str):
            token = _load_token(token, None)
        self._base_url = _safe_normalize_url(base_url, field_name="upstream_url")
        if (
            not isinstance(endpoint_suffix, str)
            or not endpoint_suffix.startswith("/")
            or "?" in endpoint_suffix
            or "#" in endpoint_suffix
            or any(part == ".." for part in endpoint_suffix.split("/"))
            or any(
                character.isspace() or ord(character) < 0x20
                for character in endpoint_suffix
            )
        ):
            raise ValueError("endpoint_suffix must be an absolute path")
        self._endpoint = _endpoint_url(
            self._base_url, endpoint_suffix.rstrip("/") or "/"
        )
        self._token = token
        self._token_file = token_file
        self._session = session if session is not None else requests.Session()
        # Never inherit ambient proxy/credential settings from the container;
        # the configured origin and mounted upstream credential are the only
        # network authority for this client.
        if hasattr(self._session, "trust_env"):
            self._session.trust_env = False
        if hasattr(self._session, "proxies"):
            self._session.proxies = {}
        self._transport = transport
        self.connect_timeout = _validate_positive_number(
            connect_timeout,
            name="connect timeout",
            maximum=MAX_CONNECT_TIMEOUT_SECONDS,
        )
        self.total_timeout = _validate_positive_number(
            total_timeout,
            name="total timeout",
            maximum=MAX_TOTAL_TIMEOUT_SECONDS,
        )
        if self.connect_timeout > self.total_timeout:
            raise ValueError("connect timeout cannot exceed total timeout")
        self.response_limit = _validate_limit(
            response_limit, name="response limit", maximum=UPSTREAM_MAX_RESPONSE_BYTES
        )
        self.concurrency = ConcurrencyLimiter(max_concurrency)
        self.circuit_breaker = (
            CircuitBreaker() if circuit_breaker is None else circuit_breaker
        )
        self._clock = time.monotonic if clock is None else clock
        self._request_lock = threading.Lock()
        self._contract_lock = threading.Lock()
        self._contract_verified = False
        self._next_id = 0

    def __repr__(self) -> str:
        # Do not include endpoint, token-file path, or credential material in
        # repr/debug output.  This object is commonly logged at startup.
        return f"{type(self).__name__}(version={UPSTREAM_VERSION!r}, token=<redacted>)"

    @property
    def endpoint(self) -> str:
        """Return a deliberately redacted endpoint marker, not its URL."""

        return "<upstream-endpoint>"

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tool_policy.UPSTREAM_TOOLS

    @property
    def registered_tools(self) -> tuple[str, ...]:
        """Verify the live server before exposing the frozen inventory."""

        return self.verify_contract()

    def list_tools(self) -> tuple[str, ...]:
        """Verify the live contract, then return only the reviewed inventory."""

        return self.verify_contract()

    def verify_contract(self) -> tuple[str, ...]:
        """Perform a bounded live initialize/tools-list contract handshake.

        The result is always the checked-in policy tuple.  The live response
        is used only for equality verification and is never registered or
        returned to callers.  A successful check is cached for this client
        instance so listing tools cannot become an unbounded discovery loop.
        """

        with self._contract_lock:
            if self._contract_verified:
                return self.tool_names
        # Do not hold the state lock over network I/O.  A concurrent readiness
        # check gets its own finite deadline rather than blocking indefinitely
        # behind another handshake; initialize/tools-list are read-only.
        deadline = self._clock() + self.total_timeout
        try:
            self.circuit_breaker.before_call()
        except BaseException as exc:
            _clear_exception_chain(exc)
            raise
        acquired = False
        try:
            acquired = self.concurrency.acquire(
                timeout=max(0.0, deadline - self._clock())
            )
        except BaseException as exc:
            self.circuit_breaker.abort_probe()
            _clear_exception_chain(exc)
            raise
        if not acquired:
            self.circuit_breaker.abort_probe()
            error = DeadlineExceededError("upstream request exceeded its deadline")
            _clear_exception_chain(error)
            raise error
        try:
            self._verify_contract_exchange(deadline=deadline)
        except CircuitOpenError:
            raise
        except Exception as exc:
            self.circuit_breaker.record_failure()
            if isinstance(exc, UpstreamMCPError):
                _clear_exception_chain(exc)
                raise
            contract_error = UpstreamContractError(
                "upstream contract verification failed"
            )
            _clear_exception_chain(contract_error)
            raise contract_error
        else:
            self.circuit_breaker.record_success()
            with self._contract_lock:
                self._contract_verified = True
            return self.tool_names
        finally:
            self.concurrency.release()

    async def verify_contract_async(self) -> tuple[str, ...]:
        """Async counterpart to :meth:`verify_contract` for custom transports."""

        with self._contract_lock:
            if self._contract_verified:
                return self.tool_names
        if self._transport is None:
            # A requests session is blocking and cancellation cannot interrupt
            # its socket.  Keep the worker's circuit/gate ownership intact.
            try:
                return await asyncio.to_thread(self.verify_contract)
            except BaseException as exc:
                _clear_exception_chain(exc)
                raise
        deadline = self._clock() + self.total_timeout
        try:
            self.circuit_breaker.before_call()
        except BaseException as exc:
            _clear_exception_chain(exc)
            raise
        try:
            acquired = await self.concurrency.acquire_async(
                max(0.0, deadline - self._clock())
            )
        except BaseException as exc:
            self.circuit_breaker.abort_probe()
            _clear_exception_chain(exc)
            raise
        if not acquired:
            self.circuit_breaker.abort_probe()
            error = DeadlineExceededError("upstream request exceeded its deadline")
            _clear_exception_chain(error)
            raise error
        try:
            await self._verify_contract_exchange_async(deadline=deadline)
        except CircuitOpenError:
            raise
        except asyncio.CancelledError:
            self.circuit_breaker.abort_probe()
            raise
        except Exception as exc:
            self.circuit_breaker.record_failure()
            if isinstance(exc, UpstreamMCPError):
                _clear_exception_chain(exc)
                raise
            contract_error = UpstreamContractError(
                "upstream contract verification failed"
            )
            _clear_exception_chain(contract_error)
            raise contract_error
        else:
            self.circuit_breaker.record_success()
            with self._contract_lock:
                self._contract_verified = True
            return self.tool_names
        finally:
            self.concurrency.release()

    def is_tool_allowed(self, name: object) -> bool:
        return isinstance(name, str) and name in tool_policy.UPSTREAM_TOOL_SET

    @property
    def next_request_id(self) -> int:
        with self._request_lock:
            self._next_id += 1
            return self._next_id

    def _credential(self) -> str | None:
        if self._token is not None:
            return _load_token(self._token, None)
        if self._token_file is not None:
            return _load_token(
                None,
                self._token_file,
                names=(
                    "MCP_AUTH_TOKEN",
                    "UPSTREAM_TOKEN",
                    "MEDIA_COMPANION_UPSTREAM_TOKEN",
                    "TOKEN",
                    "API_KEY",
                ),
            )
        return None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        credential = self._credential()
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential}"
        return headers

    def _request_payload(
        self, request_id: int, name: str, arguments: Mapping[str, object]
    ) -> bytes:
        if not isinstance(arguments, Mapping):
            raise TypeError("tool arguments must be an object")
        # canonical_json rejects duplicate/non-JSON values and gives the
        # transport a deterministic body for actor binding and tests.
        safe_arguments = cast(dict[str, JsonValue], dict(arguments))
        params: JsonObject = {
            "name": name,
            "arguments": safe_arguments,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {
                    "name": "crbl-media-companion",
                    "version": UPSTREAM_VERSION,
                },
            },
        }
        payload: JsonObject = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": request_id,
            "method": "tools/call",
            "params": params,
        }
        encoded = canonical_json(cast(Any, payload), max_bytes=MAX_REQUEST_BYTES)
        return encoded

    def _contract_payload(
        self, request_id: int, method: str, params: Mapping[str, object]
    ) -> bytes:
        if method not in {"initialize", "tools/list"}:
            raise ValueError("unsupported upstream contract method")
        payload: JsonObject = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": request_id,
            "method": method,
            "params": cast(JsonObject, dict(params)),
        }
        try:
            return canonical_json(cast(Any, payload), max_bytes=MAX_REQUEST_BYTES)
        except (TypeError, ValueError):
            raise UpstreamContractError(
                "upstream contract request is invalid"
            ) from None

    def _transport_request(
        self,
        payload: bytes,
        *,
        tool_name: str | None,
        deadline: float,
        method: str = "tools/call",
        session_id: str | None = None,
    ) -> object:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise DeadlineExceededError("upstream request exceeded its deadline")
        headers = self._headers()
        headers.update(
            {
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                "Mcp-Method": method,
            }
        )
        if tool_name is not None:
            headers["Mcp-Name"] = tool_name
        if session_id is not None:
            headers["Mcp-Session-Id"] = session_id
        timeout = (
            min(self.connect_timeout, remaining),
            min(self.total_timeout, remaining),
        )
        if self._transport is not None:
            # Injected transports are held to the same contract as the
            # requests session: they must accept ``timeout`` and
            # ``allow_redirects=False`` and honor both.  Do not retry a
            # TypeError with a second calling convention; a provider-side
            # mutation may already have been submitted before that error.
            target: object = getattr(self._transport, "request", self._transport)
            if not callable(target):
                raise UpstreamTransportError("upstream transport is unavailable")
            try:
                result = target(
                    "POST",
                    self._endpoint,
                    headers=headers,
                    data=payload,
                    timeout=timeout,
                    allow_redirects=False,
                )
            except requests.Timeout:
                raise DeadlineExceededError(
                    "upstream request exceeded its deadline"
                ) from None
            except Exception:
                raise UpstreamTransportError("upstream request failed") from None
            if inspect.isawaitable(result):
                raise UpstreamTransportError("async transport used by sync client")
            return result
        try:
            return self._session.request(
                "POST",
                self._endpoint,
                headers=headers,
                data=payload,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise DeadlineExceededError(
                "upstream request exceeded its deadline"
            ) from None
        except (requests.RequestException, OSError):
            raise UpstreamTransportError("upstream request failed") from None

    async def _transport_request_async(
        self,
        payload: bytes,
        *,
        tool_name: str | None,
        deadline: float,
        method: str = "tools/call",
        session_id: str | None = None,
    ) -> object:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise DeadlineExceededError("upstream request exceeded its deadline")
        headers = self._headers()
        headers.update(
            {
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                "Mcp-Method": method,
            }
        )
        if tool_name is not None:
            headers["Mcp-Name"] = tool_name
        if session_id is not None:
            headers["Mcp-Session-Id"] = session_id
        timeout = (
            min(self.connect_timeout, remaining),
            min(self.total_timeout, remaining),
        )
        if self._transport is not None:
            # The async transport contract mirrors the sync path and is
            # awaited under the remaining total deadline.  A single calling
            # convention avoids duplicate side effects after a TypeError.
            target: object = getattr(self._transport, "arequest", None)
            if target is None:
                target = getattr(self._transport, "request_async", None)
            if target is None:
                target = getattr(self._transport, "request", self._transport)
            if not callable(target):
                raise UpstreamTransportError("upstream transport is unavailable")
            try:
                result = target(
                    "POST",
                    self._endpoint,
                    headers=headers,
                    data=payload,
                    timeout=timeout,
                    allow_redirects=False,
                )
                if not inspect.isawaitable(result):
                    raise UpstreamTransportError(
                        "async transport must return an awaitable response"
                    )
                return await asyncio.wait_for(result, timeout=remaining)
            except asyncio.TimeoutError:
                raise DeadlineExceededError(
                    "upstream request exceeded its deadline"
                ) from None
            except requests.Timeout:
                raise DeadlineExceededError(
                    "upstream request exceeded its deadline"
                ) from None
            except (requests.RequestException, OSError, TypeError):
                raise UpstreamTransportError("upstream request failed") from None
        # requests is blocking by design; run it off the event loop and bound
        # the wait.  The in-thread deadline still bounds response streaming.
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._session.request,
                    "POST",
                    self._endpoint,
                    headers=headers,
                    data=payload,
                    timeout=timeout,
                    stream=True,
                    allow_redirects=False,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            raise DeadlineExceededError(
                "upstream request exceeded its deadline"
            ) from None
        except requests.Timeout:
            raise DeadlineExceededError(
                "upstream request exceeded its deadline"
            ) from None
        except (requests.RequestException, OSError):
            raise UpstreamTransportError("upstream request failed") from None

    def _parse_contract_response(
        self, response: object, *, request_id: int, deadline: float
    ) -> tuple[JsonObject, Mapping[object, object], int]:
        """Parse one bounded initialize/tools-list JSON-RPC response."""

        _reject_redirected_response(response, endpoint=self._endpoint)
        status_raw = getattr(response, "status_code", 200)
        if isinstance(status_raw, bool) or not isinstance(status_raw, int):
            _close_response(response)
            raise UpstreamContractError(
                "upstream contract response has an invalid status"
            )
        raw_headers = getattr(response, "headers", None)
        headers: Mapping[object, object] = (
            raw_headers if isinstance(raw_headers, Mapping) else {}
        )
        if status_raw < 200 or status_raw >= 300:
            _close_response(response)
            raise UpstreamHTTPError(status_raw)
        if self._clock() >= deadline:
            _close_response(response)
            raise DeadlineExceededError("upstream request exceeded its deadline")
        if isinstance(response, dict) and not hasattr(response, "status_code"):
            document = cast(JsonValue, response)
            _validate_json_tree(document)
            try:
                encoded_size = len(
                    json.dumps(
                        document,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            except (TypeError, ValueError):
                raise UpstreamContractError(
                    "upstream contract response is not serializable"
                ) from None
            if encoded_size > self.response_limit:
                raise ResponseSizeError("upstream response exceeds the body limit")
            wire_size = encoded_size
        elif isinstance(response, (bytes, bytearray, memoryview)):
            raw_body = bytes(cast(bytes | bytearray | memoryview, response))
            if len(raw_body) > self.response_limit:
                raise ResponseSizeError("upstream response exceeds the body limit")
            document = _parse_mcp_body(raw_body)
            wire_size = len(raw_body)
        else:
            try:
                body = _response_body(
                    response,
                    max_bytes=self.response_limit,
                    deadline=deadline,
                    clock=self._clock,
                )
            finally:
                _close_response(response)
            content_type = _header_value(headers, "content-type")
            document = _parse_mcp_body(body, content_type=content_type)
            wire_size = len(body)
        if self._clock() >= deadline:
            raise DeadlineExceededError("upstream request exceeded its deadline")
        if not isinstance(document, dict):
            raise UpstreamContractError("upstream contract response must be an object")
        if document.get("jsonrpc") != _JSONRPC_VERSION:
            raise UpstreamContractError(
                "upstream contract response has an invalid protocol"
            )
        response_id = document.get("id")
        if isinstance(response_id, bool) or response_id != request_id:
            raise UpstreamContractError("upstream contract response id does not match")
        if "error" in document:
            raise UpstreamContractError("upstream contract handshake returned an error")
        result = document.get("result")
        if not isinstance(result, dict):
            raise UpstreamContractError(
                "upstream contract response is missing a result"
            )
        return result, headers, wire_size

    @staticmethod
    def _contract_session_id(headers: Mapping[object, object]) -> str | None:
        value = _header_value(headers, "Mcp-Session-Id")
        if value is None:
            return None
        if (
            not value
            or len(value) > 256
            or any(character.isspace() or ord(character) < 0x20 for character in value)
        ):
            raise UpstreamContractError("upstream contract session id is invalid")
        return value

    @staticmethod
    def _contract_cursor(result: JsonObject) -> str | None:
        cursor = result.get("nextCursor")
        if cursor is None:
            return None
        if (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > 512
            or any(character.isspace() or ord(character) < 0x20 for character in cursor)
        ):
            raise UpstreamContractError("upstream contract cursor is invalid")
        return cursor

    def _verify_contract_exchange(self, *, deadline: float) -> tuple[str, ...]:
        if len(self.tool_names) != MAX_CONTRACT_TOOLS or set(self.tool_names) != set(
            FROZEN_UPSTREAM_TOOL_NAMES
        ):
            raise UpstreamContractError("local upstream tool policy is not pinned")
        initialize_id = self.next_request_id
        initialize_payload = self._contract_payload(
            initialize_id,
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "crbl-media-companion",
                    "version": UPSTREAM_VERSION,
                },
            },
        )
        response = self._transport_request(
            initialize_payload,
            tool_name=None,
            method="initialize",
            deadline=deadline,
        )
        initialize_result, headers, contract_bytes = self._parse_contract_response(
            response, request_id=initialize_id, deadline=deadline
        )
        if initialize_result.get("protocolVersion") != MCP_PROTOCOL_VERSION:
            raise UpstreamContractError("upstream protocol version is not pinned")
        session_id = self._contract_session_id(headers)
        tools: list[Mapping[str, object]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_CONTRACT_PAGES):
            list_id = self.next_request_id
            params: dict[str, object] = {}
            if cursor is not None:
                params["cursor"] = cursor
            list_payload = self._contract_payload(list_id, "tools/list", params)
            response = self._transport_request(
                list_payload,
                tool_name=None,
                method="tools/list",
                deadline=deadline,
                session_id=session_id,
            )
            list_result, list_headers, page_bytes = self._parse_contract_response(
                response, request_id=list_id, deadline=deadline
            )
            contract_bytes += page_bytes
            if contract_bytes > self.response_limit:
                raise ResponseSizeError("upstream contract exceeds the body limit")
            new_session_id = self._contract_session_id(list_headers)
            if new_session_id is not None:
                session_id = new_session_id
            # Keep the session binding explicit even though the pinned service
            # is currently stateless; a future stateful response must not be
            # silently sent without its server-issued session identifier.
            raw_tools = list_result.get("tools")
            if not isinstance(raw_tools, list):
                raise UpstreamContractError("upstream tools/list result is invalid")
            if len(tools) + len(raw_tools) > MAX_CONTRACT_TOOLS:
                raise UpstreamContractError("upstream tools/list exceeds its bound")
            for entry in raw_tools:
                if not isinstance(entry, Mapping):
                    raise UpstreamContractError("upstream tool entry is invalid")
                tools.append(cast(Mapping[str, object], entry))
            next_cursor = self._contract_cursor(list_result)
            if next_cursor is None:
                break
            if next_cursor in seen_cursors:
                raise UpstreamContractError("upstream tools/list cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise UpstreamContractError(
                "upstream tools/list pagination exceeded its bound"
            )
        try:
            names = validate_live_tools(tools)
        except (TypeError, ValueError):
            raise UpstreamContractError("upstream tool contract drifted") from None
        if canonical_tool_digest(tools) != UPSTREAM_TOOL_CONTRACT_SHA256:
            raise UpstreamContractError("upstream tool contract digest drifted")
        return names

    async def _verify_contract_exchange_async(
        self, *, deadline: float
    ) -> tuple[str, ...]:
        if len(self.tool_names) != MAX_CONTRACT_TOOLS or set(self.tool_names) != set(
            FROZEN_UPSTREAM_TOOL_NAMES
        ):
            raise UpstreamContractError("local upstream tool policy is not pinned")
        initialize_id = self.next_request_id
        initialize_payload = self._contract_payload(
            initialize_id,
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "crbl-media-companion",
                    "version": UPSTREAM_VERSION,
                },
            },
        )
        response = await self._transport_request_async(
            initialize_payload,
            tool_name=None,
            method="initialize",
            deadline=deadline,
        )
        initialize_result, headers, contract_bytes = self._parse_contract_response(
            response, request_id=initialize_id, deadline=deadline
        )
        if initialize_result.get("protocolVersion") != MCP_PROTOCOL_VERSION:
            raise UpstreamContractError("upstream protocol version is not pinned")
        session_id = self._contract_session_id(headers)
        tools: list[Mapping[str, object]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_CONTRACT_PAGES):
            list_id = self.next_request_id
            params: dict[str, object] = {}
            if cursor is not None:
                params["cursor"] = cursor
            list_payload = self._contract_payload(list_id, "tools/list", params)
            response = await self._transport_request_async(
                list_payload,
                tool_name=None,
                method="tools/list",
                deadline=deadline,
                session_id=session_id,
            )
            list_result, list_headers, page_bytes = self._parse_contract_response(
                response, request_id=list_id, deadline=deadline
            )
            contract_bytes += page_bytes
            if contract_bytes > self.response_limit:
                raise ResponseSizeError("upstream contract exceeds the body limit")
            new_session_id = self._contract_session_id(list_headers)
            if new_session_id is not None:
                session_id = new_session_id
            raw_tools = list_result.get("tools")
            if not isinstance(raw_tools, list):
                raise UpstreamContractError("upstream tools/list result is invalid")
            if len(tools) + len(raw_tools) > MAX_CONTRACT_TOOLS:
                raise UpstreamContractError("upstream tools/list exceeds its bound")
            for entry in raw_tools:
                if not isinstance(entry, Mapping):
                    raise UpstreamContractError("upstream tool entry is invalid")
                tools.append(cast(Mapping[str, object], entry))
            next_cursor = self._contract_cursor(list_result)
            if next_cursor is None:
                break
            if next_cursor in seen_cursors:
                raise UpstreamContractError("upstream tools/list cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise UpstreamContractError(
                "upstream tools/list pagination exceeded its bound"
            )
        try:
            names = validate_live_tools(tools)
        except (TypeError, ValueError):
            raise UpstreamContractError("upstream tool contract drifted") from None
        if canonical_tool_digest(tools) != UPSTREAM_TOOL_CONTRACT_SHA256:
            raise UpstreamContractError("upstream tool contract digest drifted")
        return names

    def _parse_response(
        self, response: object, *, request_id: int, deadline: float
    ) -> MCPToolResult:
        _reject_redirected_response(response, endpoint=self._endpoint)
        status_raw = getattr(response, "status_code", 200)
        if isinstance(status_raw, bool) or not isinstance(status_raw, int):
            _close_response(response)
            raise ResponseValidationError("upstream response has an invalid status")
        if status_raw < 200 or status_raw >= 300:
            retry_after: int | None = None
            headers = getattr(response, "headers", None)
            if isinstance(headers, Mapping):
                raw_retry = _header_value(headers, "retry-after")
                if raw_retry is not None:
                    try:
                        parsed_retry = int(raw_retry)
                        if parsed_retry >= 0:
                            retry_after = min(parsed_retry, 86_400)
                    except (TypeError, ValueError):
                        retry_after = None
            _close_response(response)
            raise UpstreamHTTPError(status_raw, retry_after=retry_after)
        if self._clock() >= deadline:
            _close_response(response)
            raise DeadlineExceededError("upstream request exceeded its deadline")
        if isinstance(response, Mapping) and not hasattr(response, "status_code"):
            if self._clock() >= deadline:
                raise DeadlineExceededError("upstream request exceeded its deadline")
            document = cast(JsonValue, response)
            _validate_json_tree(document)
            try:
                encoded_size = len(
                    json.dumps(
                        document,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            except (TypeError, ValueError):
                raise ResponseValidationError(
                    "upstream response is not serializable"
                ) from None
            if encoded_size > self.response_limit:
                raise ResponseSizeError("upstream response exceeds the body limit")
            parsed = self._parse_envelope(document, request_id=request_id)
            if self._clock() >= deadline:
                raise DeadlineExceededError("upstream request exceeded its deadline")
            return parsed
        if isinstance(response, (bytes, bytearray, memoryview)):
            body = response
        else:
            try:
                body = _response_body(
                    response,
                    max_bytes=self.response_limit,
                    deadline=deadline,
                    clock=self._clock,
                )
            finally:
                _close_response(response)
        if self._clock() >= deadline:
            raise DeadlineExceededError("upstream request exceeded its deadline")
        if isinstance(body, (bytearray, memoryview)):
            raw_body = bytes(body)
        elif isinstance(body, bytes):
            raw_body = body
        else:
            raise ResponseValidationError("upstream response body has an invalid type")
        if len(raw_body) > self.response_limit:
            raise ResponseSizeError("upstream response exceeds the body limit")
        content_type = None
        raw_headers = getattr(response, "headers", None)
        if isinstance(raw_headers, Mapping):
            content_type = _header_value(raw_headers, "content-type")
        document = _parse_mcp_body(raw_body, content_type=content_type)
        parsed = self._parse_envelope(document, request_id=request_id)
        if self._clock() >= deadline:
            raise DeadlineExceededError("upstream request exceeded its deadline")
        return parsed

    def _parse_envelope(self, document: JsonValue, *, request_id: int) -> MCPToolResult:
        if not isinstance(document, dict):
            raise UpstreamProtocolError("upstream MCP response must be an object")
        if document.get("jsonrpc") != _JSONRPC_VERSION:
            raise UpstreamProtocolError(
                "upstream MCP response has an invalid JSON-RPC version"
            )
        response_id = document.get("id")
        if isinstance(response_id, bool) or not (
            response_id is None or isinstance(response_id, (int, str))
        ):
            raise UpstreamProtocolError("upstream MCP response id has an invalid type")
        if response_id != request_id:
            raise UpstreamProtocolError(
                "upstream MCP response id does not match the request"
            )
        if "error" in document:
            error = document["error"]
            if not isinstance(error, dict):
                raise UpstreamProtocolError("upstream MCP error has an invalid type")
            code = error.get("code")
            if isinstance(code, bool) or not isinstance(code, (int, str)):
                raise UpstreamProtocolError("upstream MCP error has an invalid code")
            # Do not forward provider exception text.  It can contain paths or
            # credentials; the typed error retains only its safe code.
            raise UpstreamProtocolError(
                f"upstream MCP tool error ({redact_text(str(code), max_bytes=128)})"
            )
        result = document.get("result")
        if not isinstance(result, dict):
            raise UpstreamProtocolError("upstream MCP response is missing a result")
        raw_content = result.get("content", [])
        if not isinstance(raw_content, list):
            raise UpstreamProtocolError("upstream MCP content must be an array")
        content: list[MCPToolContent] = []
        for raw_item in raw_content:
            if not isinstance(raw_item, dict) or raw_item.get("type") != "text":
                raise UpstreamProtocolError(
                    "upstream MCP content contains an unsupported item"
                )
            text = raw_item.get("text")
            if not isinstance(text, str) or not text:
                raise UpstreamProtocolError("upstream MCP text content is invalid")
            content.append(MCPToolContent(text))
        raw_structured = result.get(
            "structuredContent", result.get("structured_content")
        )
        structured: JsonObject | None = None
        if raw_structured is not None:
            if not isinstance(raw_structured, dict):
                raise UpstreamProtocolError(
                    "upstream structured content must be an object"
                )
            sanitized = _sanitize_structured(raw_structured)
            if not isinstance(sanitized, dict):
                raise UpstreamProtocolError(
                    "upstream structured content must be an object"
                )
            structured = sanitized
        raw_is_error = result.get("isError", result.get("is_error", False))
        if not isinstance(raw_is_error, bool):
            raise UpstreamProtocolError("upstream MCP error flag is invalid")
        parsed = MCPToolResult(
            request_id=response_id,
            content=tuple(content),
            structured_content=structured,
            is_error=raw_is_error,
        )
        try:
            serialized_size = len(
                json.dumps(
                    parsed.to_dict(), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        except (TypeError, ValueError):
            raise ResponseValidationError(
                "upstream result is not serializable"
            ) from None
        if serialized_size > self.response_limit:
            raise ResponseSizeError("upstream result exceeds the response limit")
        return parsed

    def _invoke_sync(self, name: str, arguments: Mapping[str, object]) -> MCPToolResult:
        request_id = self.next_request_id
        payload = self._request_payload(request_id, name, arguments)
        deadline = self._clock() + self.total_timeout
        self.circuit_breaker.before_call()
        try:
            acquired = self.concurrency.acquire(
                timeout=max(0.0, deadline - self._clock())
            )
        except BaseException:
            self.circuit_breaker.abort_probe()
            raise
        if not acquired:
            self.circuit_breaker.abort_probe()
            raise DeadlineExceededError("upstream request exceeded its deadline")
        try:
            response = self._transport_request(
                payload, tool_name=name, deadline=deadline
            )
            result = self._parse_response(
                response, request_id=request_id, deadline=deadline
            )
        except (CircuitOpenError, ToolNotAllowedError):
            raise
        except Exception as exc:
            # Circuit state is intentionally based on safe typed failures, not
            # on caller argument validation (which occurs before this point).
            self.circuit_breaker.record_failure()
            if isinstance(exc, UpstreamMCPError):
                raise
            raise UpstreamTransportError("upstream request failed") from None
        else:
            self.circuit_breaker.record_success()
            return result
        finally:
            self.concurrency.release()

    async def _invoke_async(
        self, name: str, arguments: Mapping[str, object]
    ) -> MCPToolResult:
        # ``requests`` cannot be cancelled once its socket call is running.
        # Run the complete sync invocation in one worker instead of acquiring
        # an async slot and then releasing it when ``wait_for`` cancels only
        # the Future.  The sync invocation keeps its semaphore claim until the
        # worker exits (and enforces the same total deadline internally).
        if self._transport is None:
            return await asyncio.to_thread(self._invoke_sync, name, arguments)
        request_id = self.next_request_id
        payload = self._request_payload(request_id, name, arguments)
        deadline = self._clock() + self.total_timeout
        self.circuit_breaker.before_call()
        try:
            acquired = await self.concurrency.acquire_async(
                max(0.0, deadline - self._clock())
            )
        except BaseException:
            self.circuit_breaker.abort_probe()
            raise
        if not acquired:
            self.circuit_breaker.abort_probe()
            raise DeadlineExceededError("upstream request exceeded its deadline")
        try:
            response = await self._transport_request_async(
                payload, tool_name=name, deadline=deadline
            )
            # An injected async transport has already completed its network
            # operation.  Parse inline so cancellation cannot release the
            # dependency gate while a parser worker continues in the
            # background.
            result = self._parse_response(
                response, request_id=request_id, deadline=deadline
            )
        except (CircuitOpenError, ToolNotAllowedError):
            raise
        except asyncio.CancelledError:
            # A half-open probe that is cancelled after acquiring the gate
            # still has to be closed out; otherwise no future call can own
            # the probe and the circuit remains stranded in HALF_OPEN.
            self.circuit_breaker.abort_probe()
            raise
        except asyncio.TimeoutError:
            self.circuit_breaker.record_failure()
            raise DeadlineExceededError(
                "upstream request exceeded its deadline"
            ) from None
        except Exception as exc:
            self.circuit_breaker.record_failure()
            if isinstance(exc, UpstreamMCPError):
                raise
            raise UpstreamTransportError("upstream request failed") from None
        else:
            self.circuit_breaker.record_success()
            return result
        finally:
            self.concurrency.release()

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> MCPToolResult:
        """Call one exact pinned upstream tool synchronously."""

        if not isinstance(name, str) or name not in tool_policy.UPSTREAM_TOOL_SET:
            raise ToolNotAllowedError(
                "tool is not registered in the pinned upstream contract"
            )
        args: Mapping[str, object] = {} if arguments is None else arguments
        try:
            return self._invoke_sync(name, args)
        except BaseException as exc:
            _clear_exception_chain(exc)
            raise

    def call(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> MCPToolResult:
        return self.call_tool(name, arguments)

    def invoke(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> MCPToolResult:
        return self.call_tool(name, arguments)

    async def call_tool_async(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> MCPToolResult:
        """Call one exact pinned upstream tool asynchronously."""

        if not isinstance(name, str) or name not in tool_policy.UPSTREAM_TOOL_SET:
            raise ToolNotAllowedError(
                "tool is not registered in the pinned upstream contract"
            )
        args: Mapping[str, object] = {} if arguments is None else arguments
        try:
            return await self._invoke_async(name, args)
        except BaseException as exc:
            _clear_exception_chain(exc)
            raise

    async def call_async(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> MCPToolResult:
        return await self.call_tool_async(name, arguments)

    async def ainvoke(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> MCPToolResult:
        return await self.call_tool_async(name, arguments)

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    async def aclose(self) -> None:
        self.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


# Adapter spelling used by the implementation plan.
UpstreamMCPAdapter = UpstreamMCPClient
UpstreamClient = UpstreamMCPClient
MCPClient = UpstreamMCPClient
MediaServerMCPClient = UpstreamMCPClient


def _queue_state(raw: object) -> QueueState:
    if not isinstance(raw, str):
        return QueueState.UNKNOWN
    state = raw.strip().lower()
    if any(token in state for token in ("fail", "error")):
        return QueueState.FAILED
    if any(token in state for token in ("import", "pendingimport")):
        return QueueState.IMPORTING
    if any(token in state for token in ("pause", "hold")):
        return QueueState.PAUSED
    if any(token in state for token in ("download", "downloading")):
        return QueueState.DOWNLOADING
    if any(token in state for token in ("complete", "completed", "imported")):
        return QueueState.COMPLETED
    if any(token in state for token in ("queue", "queued", "pending")):
        return QueueState.QUEUED
    return QueueState.UNKNOWN


def _queue_title(record: Mapping[str, object]) -> str | None:
    for key in ("title", "name"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nested_key in ("movie", "series", "movieFile"):
        nested = record.get(nested_key)
        if isinstance(nested, Mapping):
            value = nested.get("title") or nested.get("name")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _queue_progress(record: Mapping[str, object]) -> float | None:
    progress = record.get("progress")
    if isinstance(progress, Mapping):
        raw: object = progress.get("percent")
        if raw is None:
            raw = progress.get("percentage")
    else:
        raw = progress
    if raw is None:
        raw = record.get("progressPercent", record.get("percentage"))
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if isinstance(raw, float) and not math.isfinite(raw):
        return None
    result = float(raw)
    return min(100.0, max(0.0, result))


def _queue_eta(record: Mapping[str, object]) -> int | None:
    raw: object = record.get("etaSeconds")
    if raw is None:
        raw = record.get("timeleft", record.get("eta"))
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and not math.isfinite(raw):
            return None
        value = int(raw)
        return value if value >= 0 else None
    if not isinstance(raw, str):
        return None
    parts = raw.strip().split(":")
    if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
        return None
    values = [int(part) for part in parts]
    if len(values) == 2:
        minutes, seconds = values
        if seconds >= 60:
            return None
        return minutes * 60 + seconds
    hours, minutes, seconds = values
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _queue_item(record: object) -> QueueItem:
    if not isinstance(record, Mapping):
        raise ResponseValidationError("Radarr queue record must be an object")
    title = _queue_title(record)
    if title is None:
        raise ResponseValidationError("Radarr queue record has no title")
    error = record.get("errorMessage") or record.get("error") or record.get("message")
    safe_error = None
    if isinstance(error, str) and error.strip():
        safe_error = redact_text(error, max_bytes=MAX_QUEUE_ERROR_BYTES)
    try:
        return QueueItem(
            service=ServiceName.RADARR,
            title=redact_text(title, max_bytes=512),
            state=_queue_state(record.get("status", record.get("state"))),
            progress_percent=_queue_progress(record),
            eta_seconds=_queue_eta(record),
            error=safe_error,
        )
    except ValueError:
        raise ResponseValidationError("Radarr queue record is invalid") from None


def _queue_item_document(item: QueueItem) -> dict[str, object]:
    """Build the exact normalized shape used by the safe response serializer."""

    result: dict[str, object] = {
        "service": item.service.value,
        "title": item.title,
        "state": item.state.value,
    }
    if item.progress_percent is not None:
        result["progress_percent"] = item.progress_percent
    if item.eta_seconds is not None:
        result["eta_seconds"] = item.eta_seconds
    if item.error is not None:
        result["error"] = item.error
    if item.media_type is not None:
        result["media_type"] = item.media_type.value
    return result


def _queue_page_wire_size(
    items: tuple[QueueItem, ...],
    *,
    as_of: datetime,
    next_cursor: str | None,
    truncated: bool,
    total: int | None,
) -> int:
    """Return the bounded JSON size of a normalized queue page."""

    document: dict[str, object] = {
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "items": [_queue_item_document(item) for item in items],
        "truncated": truncated,
    }
    if next_cursor is not None:
        document["next_cursor"] = next_cursor
    if total is not None:
        document["total"] = total
    try:
        return len(
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeError):
        raise ResponseValidationError(
            "normalized queue response is not serializable"
        ) from None


def _fit_queue_items(
    items: list[QueueItem],
    *,
    as_of: datetime,
    total: int | None,
    next_cursor: str | None = None,
    truncated: bool = False,
    max_bytes: int = UPSTREAM_MAX_RESPONSE_BYTES,
) -> tuple[tuple[QueueItem, ...], bool]:
    """Trim an aggregate queue snapshot until its serialized form fits."""

    did_truncate = truncated
    original = tuple(items)
    if (
        _queue_page_wire_size(
            original,
            as_of=as_of,
            next_cursor=next_cursor,
            truncated=did_truncate,
            total=total,
        )
        <= max_bytes
    ):
        return original, did_truncate

    # Serialized size is monotonic in the item prefix, so find the largest
    # fitting prefix without repeatedly serializing thousands of records.
    did_truncate = True
    lower = 0
    upper = len(original)
    while lower < upper:
        midpoint = (lower + upper + 1) // 2
        if (
            _queue_page_wire_size(
                original[:midpoint],
                as_of=as_of,
                next_cursor=next_cursor,
                truncated=did_truncate,
                total=total,
            )
            <= max_bytes
        ):
            lower = midpoint
        else:
            upper = midpoint - 1
    selected = original[:lower]
    if (
        _queue_page_wire_size(
            selected,
            as_of=as_of,
            next_cursor=next_cursor,
            truncated=did_truncate,
            total=total,
        )
        > max_bytes
    ):
        raise ResponseSizeError("normalized queue response exceeds the response limit")
    return selected, did_truncate


class RadarrQueueAdapter:
    """Typed fallback for the missing upstream ``radarr_get_queue`` tool."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        token: str | None = None,
        api_key_file: object | None = None,
        session: requests.Session | None = None,
        transport: TransportCallable | object | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        max_concurrency: int = MAX_CONCURRENT_CALLS,
        circuit_breaker: CircuitBreaker | None = None,
        response_limit: int = UPSTREAM_MAX_RESPONSE_BYTES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if token is not None:
            if api_key is not None or api_key_file is not None:
                raise ValueError("provide one Radarr credential source")
            api_key = token
        elif api_key is not None and api_key_file is not None:
            raise ValueError("provide one Radarr credential source")
        if isinstance(api_key, str):
            api_key = _load_token(api_key, None)
        self._base_url = _safe_normalize_url(base_url, field_name="radarr_url")
        self._endpoint = (
            self._base_url + "/queue"
            if self._base_url.endswith("/api/v3")
            else _endpoint_url(self._base_url, "/api/v3/queue")
        )
        self._api_key = api_key
        self._api_key_file = api_key_file
        self._session = session if session is not None else requests.Session()
        if hasattr(self._session, "trust_env"):
            self._session.trust_env = False
        if hasattr(self._session, "proxies"):
            self._session.proxies = {}
        self._transport = transport
        self.connect_timeout = _validate_positive_number(
            connect_timeout, name="connect timeout", maximum=MAX_CONNECT_TIMEOUT_SECONDS
        )
        self.total_timeout = _validate_positive_number(
            total_timeout, name="total timeout", maximum=MAX_TOTAL_TIMEOUT_SECONDS
        )
        if self.connect_timeout > self.total_timeout:
            raise ValueError("connect timeout cannot exceed total timeout")
        self.response_limit = _validate_limit(
            response_limit, name="response limit", maximum=UPSTREAM_MAX_RESPONSE_BYTES
        )
        self.concurrency = ConcurrencyLimiter(max_concurrency)
        self.circuit_breaker = (
            CircuitBreaker() if circuit_breaker is None else circuit_breaker
        )
        self._clock = time.monotonic if clock is None else clock

    def __repr__(self) -> str:
        return "RadarrQueueAdapter(token=<redacted>)"

    def _credential(self) -> str | None:
        return _load_token(
            self._api_key,
            self._api_key_file,
            names=("RADARR_API_KEY", "API_KEY", "APIKEY", "TOKEN"),
        )

    def _request(self, *, page: int, page_size: int, deadline: float) -> object:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise DeadlineExceededError("Radarr request exceeded its deadline")
        query = urlencode(
            {
                "page": page,
                "pageSize": page_size,
                "includeUnknownMovieItems": "true",
            }
        )
        url = self._endpoint + "?" + query
        headers = {"Accept": "application/json"}
        credential = self._credential()
        if credential is not None:
            headers["X-Api-Key"] = credential
        timeout = (
            min(self.connect_timeout, remaining),
            min(self.total_timeout, remaining),
        )
        if self._transport is not None:
            # Custom queue transports must honor this timeout and the
            # redirect-denial flag; never retry a failed call with another
            # signature because a GET fallback can still have side effects.
            target: object = getattr(self._transport, "request", self._transport)
            if not callable(target):
                raise UpstreamTransportError("Radarr transport is unavailable")
            try:
                result = target(
                    "GET",
                    url,
                    headers=headers,
                    data=None,
                    timeout=timeout,
                    allow_redirects=False,
                )
            except requests.Timeout:
                raise DeadlineExceededError(
                    "Radarr request exceeded its deadline"
                ) from None
            except Exception:
                raise UpstreamTransportError("Radarr request failed") from None
            if inspect.isawaitable(result):
                raise UpstreamTransportError("async transport used by sync client")
            return result
        try:
            return self._session.request(
                "GET",
                url,
                headers=headers,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise DeadlineExceededError(
                "Radarr request exceeded its deadline"
            ) from None
        except (requests.RequestException, OSError):
            raise UpstreamTransportError("Radarr request failed") from None

    def _parse_page(
        self, response: object, *, deadline: float
    ) -> tuple[list[QueueItem], int | None, bool]:
        status = getattr(response, "status_code", 200)
        if isinstance(status, bool) or not isinstance(status, int):
            _close_response(response)
            raise ResponseValidationError("Radarr response has an invalid status")
        if status < 200 or status >= 300:
            _close_response(response)
            raise UpstreamHTTPError(status)
        if isinstance(response, Mapping) and not hasattr(response, "status_code"):
            if self._clock() >= deadline:
                raise DeadlineExceededError("Radarr request exceeded its deadline")
            document = cast(JsonValue, response)
            _validate_json_tree(document)
            try:
                encoded_size = len(
                    json.dumps(
                        document, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                )
            except (TypeError, ValueError):
                raise ResponseValidationError(
                    "Radarr response is not serializable"
                ) from None
            if encoded_size > self.response_limit:
                raise ResponseSizeError("Radarr response exceeds the body limit")
            parsed = self._parse_page_document(document)
            if self._clock() >= deadline:
                raise DeadlineExceededError("Radarr request exceeded its deadline")
            return parsed
        if isinstance(response, (bytes, bytearray, memoryview)):
            body = response
        else:
            try:
                body = _response_body(
                    response,
                    max_bytes=self.response_limit,
                    deadline=deadline,
                    clock=self._clock,
                )
            finally:
                _close_response(response)
        if not isinstance(body, bytes):
            body = bytes(body)
        document = _parse_json_body(body)
        if self._clock() >= deadline:
            raise DeadlineExceededError("Radarr request exceeded its deadline")
        parsed = self._parse_page_document(document)
        if self._clock() >= deadline:
            raise DeadlineExceededError("Radarr request exceeded its deadline")
        return parsed

    def _parse_page_document(
        self, document: JsonValue
    ) -> tuple[list[QueueItem], int | None, bool]:
        records: object = document
        total_records: int | None = None
        if isinstance(document, dict):
            records = document.get("records", document.get("items"))
            raw_total = document.get("totalRecords", document.get("total"))
            if (
                isinstance(raw_total, int)
                and not isinstance(raw_total, bool)
                and raw_total >= 0
            ):
                total_records = raw_total
        if not isinstance(records, list):
            raise ResponseValidationError("Radarr queue response must contain records")
        items = [_queue_item(record) for record in records]
        return items, total_records, len(records) == 0

    def get_queue_page(
        self, *, page: int = 1, limit: int = DEFAULT_PAGE_SIZE
    ) -> Page[QueueItem]:
        try:
            return self._get_queue_page(page=page, limit=limit)
        except BaseException as exc:
            _clear_exception_chain(exc)
            raise

    def _get_queue_page(
        self, *, page: int = 1, limit: int = DEFAULT_PAGE_SIZE
    ) -> Page[QueueItem]:
        page = _validate_limit(page, name="page", maximum=20_000)
        limit = _validate_limit(limit, name="limit", maximum=MAX_PAGE_SIZE)
        deadline = self._clock() + self.total_timeout
        self.circuit_breaker.before_call()
        try:
            acquired = self.concurrency.acquire(
                timeout=max(0.0, deadline - self._clock())
            )
        except BaseException:
            self.circuit_breaker.abort_probe()
            raise
        if not acquired:
            self.circuit_breaker.abort_probe()
            raise DeadlineExceededError("Radarr request exceeded its deadline")
        try:
            response = self._request(page=page, page_size=limit, deadline=deadline)
            items, total, empty = self._parse_page(response, deadline=deadline)
        except Exception as exc:
            self.circuit_breaker.record_failure()
            if isinstance(exc, UpstreamMCPError):
                _clear_exception_chain(exc)
                raise
            raise UpstreamTransportError("Radarr request failed") from None
        else:
            self.circuit_breaker.record_success()
        finally:
            self.concurrency.release()
        has_next = (
            not empty
            and (total is None or page * limit < total)
            and len(items) >= limit
        )
        as_of = datetime.now(timezone.utc)
        page_items = tuple(items)
        next_cursor = str(page + 1) if has_next else None
        if (
            _queue_page_wire_size(
                page_items,
                as_of=as_of,
                next_cursor=next_cursor,
                truncated=False,
                total=total,
            )
            > self.response_limit
        ):
            raise ResponseSizeError(
                "normalized queue response exceeds the response limit"
            )
        if self._clock() >= deadline:
            raise DeadlineExceededError("Radarr request exceeded its deadline")
        return Page(
            items=page_items,
            as_of=as_of,
            next_cursor=next_cursor,
            truncated=False,
            total=total,
            partial_errors=(),
        )

    def get_queue(self, *, limit: int = DEFAULT_PAGE_SIZE) -> Page[QueueItem]:
        try:
            return self._get_queue(limit=limit)
        except BaseException as exc:
            _clear_exception_chain(exc)
            raise

    def _get_queue(self, *, limit: int = DEFAULT_PAGE_SIZE) -> Page[QueueItem]:
        """Read a bounded queue snapshot, never returning provider objects."""

        limit = _validate_limit(limit, name="limit", maximum=MAX_PAGE_SIZE)
        deadline = self._clock() + self.total_timeout
        self.circuit_breaker.before_call()
        try:
            acquired = self.concurrency.acquire(
                timeout=max(0.0, deadline - self._clock())
            )
        except BaseException:
            self.circuit_breaker.abort_probe()
            raise
        if not acquired:
            self.circuit_breaker.abort_probe()
            raise DeadlineExceededError("Radarr request exceeded its deadline")
        all_items: list[QueueItem] = []
        total: int | None = None
        truncated = False
        try:
            page = 1
            while len(all_items) < MAX_QUEUE_ITEMS:
                response = self._request(page=page, page_size=limit, deadline=deadline)
                items, page_total, empty = self._parse_page(response, deadline=deadline)
                if page_total is not None:
                    total = page_total
                if not items:
                    break
                remaining = MAX_QUEUE_ITEMS - len(all_items)
                all_items.extend(items[:remaining])
                if len(items) > remaining:
                    truncated = True
                    break
                if empty or len(items) < limit:
                    break
                if total is not None and page * limit >= total:
                    break
                page += 1
            if len(all_items) >= MAX_QUEUE_ITEMS and (
                total is None or total > MAX_QUEUE_ITEMS
            ):
                truncated = True
        except Exception as exc:
            self.circuit_breaker.record_failure()
            if isinstance(exc, UpstreamMCPError):
                _clear_exception_chain(exc)
                raise
            raise UpstreamTransportError("Radarr request failed") from None
        else:
            self.circuit_breaker.record_success()
        finally:
            self.concurrency.release()
        as_of = datetime.now(timezone.utc)
        selected_items, truncated = _fit_queue_items(
            all_items,
            as_of=as_of,
            total=total,
            truncated=truncated,
            max_bytes=self.response_limit,
        )
        if self._clock() >= deadline:
            raise DeadlineExceededError("Radarr request exceeded its deadline")
        return Page(
            items=selected_items,
            as_of=as_of,
            next_cursor=None,
            truncated=truncated,
            total=total,
            partial_errors=(),
        )

    def get_queue_items(
        self, *, limit: int = DEFAULT_PAGE_SIZE
    ) -> tuple[QueueItem, ...]:
        return self.get_queue(limit=limit).items

    fetch_queue = get_queue
    queue = get_queue

    async def get_queue_page_async(
        self, *, page: int = 1, limit: int = DEFAULT_PAGE_SIZE
    ) -> Page[QueueItem]:
        return await asyncio.to_thread(self.get_queue_page, page=page, limit=limit)

    async def get_queue_async(
        self, *, limit: int = DEFAULT_PAGE_SIZE
    ) -> Page[QueueItem]:
        return await asyncio.to_thread(self.get_queue, limit=limit)

    async def aclose(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()


RadarrQueueClient = RadarrQueueAdapter
RadarrQueueFallback = RadarrQueueAdapter


__all__ = [
    "DEFAULT_CIRCUIT_FAILURE_THRESHOLD",
    "DEFAULT_CIRCUIT_OPEN_SECONDS",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_TOTAL_TIMEOUT_SECONDS",
    "MAX_CONCURRENT_CALLS",
    "MAX_CREDENTIAL_BYTES",
    "MAX_CONNECT_TIMEOUT_SECONDS",
    "MAX_PAGE_SIZE",
    "MAX_QUEUE_ERROR_BYTES",
    "MAX_QUEUE_ITEMS",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_TOTAL_TIMEOUT_SECONDS",
    "MCP_PROTOCOL_VERSION",
    "UPSTREAM_MAX_RESPONSE_BYTES",
    "UPSTREAM_MCP_PROTOCOL_VERSION",
    "UPSTREAM_REVISION",
    "UPSTREAM_VERSION",
    "CircuitBreaker",
    "CircuitOpenError",
    "ConcurrencyGate",
    "ConcurrencyLimiter",
    "DeadlineExceededError",
    "DependencyConcurrency",
    "MCPClient",
    "MCPContent",
    "MCPResponse",
    "MCPResult",
    "MCPToolContent",
    "MCPToolResult",
    "MediaServerMCPClient",
    "RadarrQueueAdapter",
    "RadarrQueueClient",
    "RadarrQueueFallback",
    "ResponseSizeError",
    "ResponseValidationError",
    "ToolNotAllowedError",
    "UpstreamClient",
    "UpstreamHTTPError",
    "UpstreamMCPAdapter",
    "UpstreamMCPClient",
    "UpstreamMCPError",
    "UpstreamProtocolError",
    "UpstreamTransportError",
]
