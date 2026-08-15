"""Typed, bounded Radarr adapter used by the companion.

The adapter is deliberately a small allowlisted client rather than a generic
provider proxy.  It talks only to the configured Radarr origin, never follows
redirects, ignores proxy environment variables, and converts provider objects
to records owned by the companion before they leave this module.

The private ``_ConfiguredHTTPTransport`` is shared by the other provider
adapters.  Keeping it here avoids another public module while preserving a
small protocol boundary that tests and an application container can replace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import requests

from ..config import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_TOTAL_TIMEOUT_SECONDS,
    SecretFileRef,
    ServiceEndpoint,
    TimeoutConfig,
    normalize_url,
)
from ..errors import DependencyError, ModelValidationError
from ..models import (
    MediaCandidate,
    MediaIdentity,
    MediaType,
    Page,
    QueueItem,
    QueueState,
    ServiceName,
)
from ..redaction import is_secret_key, redact_text


MAX_JSON_RESPONSE_BYTES = 256 * 1024
# Safe provider records use the same serialized-response ceiling as the
# companion's read tools.  Poster bytes are the sole larger, explicitly
# bounded exception and pass their own limit at the Plex adapter boundary.
MAX_PROVIDER_RESPONSE_BYTES = MAX_JSON_RESPONSE_BYTES
MAX_QUEUE_ITEMS = 5_000
DEFAULT_QUEUE_PAGE_SIZE = 250
MAX_QUEUE_PAGE_SIZE = 250
MAX_SEARCH_RESULTS = 100


class AdapterError(DependencyError):
    """Base class for provider transport/shape failures."""


class AdapterConfigurationError(AdapterError):
    """A provider adapter was built without a safe configured origin/secret."""


class AdapterTransportError(AdapterError):
    """The request failed before a usable provider response was obtained."""

    def __init__(self, message: str, *, transmitted: bool = False) -> None:
        self.transmitted = transmitted
        super().__init__(message)


class AdapterHTTPError(AdapterTransportError):
    """The provider returned an HTTP error or an explicit redirect."""

    def __init__(self, service: str, status_code: int, message: str) -> None:
        self.service = service
        self.status_code = status_code
        super().__init__(
            f"{service} request failed ({status_code}): {redact_text(message, max_bytes=512)}"
        )


class AdapterResponseError(AdapterError):
    """The provider response exceeded a bound or was not valid JSON."""


class AdapterTimeoutError(AdapterTransportError):
    """The connect or total deadline expired."""

    def __init__(self, message: str, *, transmitted: bool = False) -> None:
        super().__init__(message, transmitted=transmitted)


class AdapterCircuitOpenError(AdapterTransportError):
    """The dependency circuit is open after repeated failures."""


class SecretReader(Protocol):
    """Narrow secret-file boundary used by all configured-origin adapters."""

    def read_secret(self, reference: SecretFileRef | Path | str) -> str: ...


class HttpTransport(Protocol):
    """Replaceable typed HTTP boundary for integration tests/containers."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json_body: object | None = None,
        body: bytes | None = None,
        max_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    ) -> "HTTPResponse": ...


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    """Bounded response bytes and metadata; no ``requests.Response`` escapes."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str | None = None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8")) if self.body else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterResponseError("provider returned invalid JSON") from exc


class FileSecretReader:
    """Read one bounded mounted secret file without echoing its value."""

    MAX_SECRET_BYTES = 16 * 1024

    def read_secret(self, reference: SecretFileRef | Path | str) -> str:
        if isinstance(reference, SecretFileRef):
            path = reference.path
        else:
            path = Path(reference)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise AdapterConfigurationError(
                "configured secret file is unavailable"
            ) from exc
        if len(raw) > self.MAX_SECRET_BYTES:
            raise AdapterConfigurationError("configured secret file is too large")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdapterConfigurationError(
                "configured secret file is not UTF-8"
            ) from exc
        selector = getattr(reference, "key", None)
        if not isinstance(selector, str):
            selector = None
        value = _extract_secret_value(text, name=selector)
        if not value:
            raise AdapterConfigurationError("configured secret file is empty")
        return value


def _extract_secret_value(text: str, *, name: str | None = None) -> str:
    """Accept raw mounted secrets and simple ``NAME=value`` secret files."""

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        return ""
    assignments: dict[str, str] = {}
    raw_lines: list[str] = []
    for line in lines:
        if "=" not in line:
            raw_lines.append(line)
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        if not key or key in assignments:
            raise ValueError("secret file contains an invalid or duplicate assignment")
        assignments[key] = value.strip().strip("\"'")

    if name is not None:
        if name in assignments:
            return assignments[name]
        if not assignments and len(raw_lines) == 1:
            return raw_lines[0].strip().strip("\"'")
        return ""

    recognized = {
        key: value
        for key, value in assignments.items()
        if key.upper()
        in {
            "API_KEY",
            "APIKEY",
            "RADARR_API_KEY",
            "SONARR_API_KEY",
            "PLEX_API_KEY",
            "PLEX_TOKEN",
            "PLEX_TOKEN_FILE",
            "TMDB_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "BOT_TOKEN",
            "MCP_AUTH_TOKEN",
            "TOKEN",
        }
    }
    if len(recognized) == 1:
        return next(iter(recognized.values()))
    if not assignments and len(raw_lines) == 1:
        return raw_lines[0].strip().strip("\"'")
    if len(recognized) > 1:
        raise ValueError("secret file requires an explicit credential selector")
    return ""


class _ConfiguredHTTPTransport:
    """Requests-backed transport with explicit origin/deadline/body controls."""

    def __init__(
        self,
        *,
        timeouts: TimeoutConfig | None = None,
        session: requests.Session | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        max_concurrency: int = 8,
        breaker_failures: int = 5,
        breaker_seconds: float = 30.0,
        allowed_origin: str | None = None,
        allowed_addresses: Sequence[str] = (),
        allow_private_addresses: bool = False,
    ) -> None:
        self.timeouts = timeouts or TimeoutConfig(
            connect_seconds=connect_timeout,
            total_seconds=total_timeout,
            body_seconds=total_timeout,
        )
        self.session = session or requests.Session()
        # ``trust_env=False`` is the requests switch that disables HTTP(S)_PROXY,
        # NO_PROXY, and related ambient process configuration.  The empty map is
        # retained for fakes/tests that inspect the session itself.
        self.session.trust_env = False
        self.session.proxies = {}
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 1 <= max_concurrency <= 8
        ):
            raise ValueError("max_concurrency must be between 1 and 8")
        if (
            isinstance(breaker_failures, bool)
            or not isinstance(breaker_failures, int)
            or breaker_failures < 1
        ):
            raise ValueError("breaker_failures must be positive")
        if (
            not isinstance(breaker_seconds, (int, float))
            or isinstance(breaker_seconds, bool)
            or breaker_seconds <= 0
        ):
            raise ValueError("breaker_seconds must be positive")
        self._concurrency = threading.BoundedSemaphore(max_concurrency)
        self._breaker_lock = threading.Lock()
        self._breaker_failures = breaker_failures
        self._breaker_seconds = float(breaker_seconds)
        self._failure_count = 0
        self._opened_until = 0.0
        self._allowed_origin = (
            normalize_url(allowed_origin, field_name="allowed_origin")
            if allowed_origin is not None
            else None
        )
        addresses: set[str] = set()
        address_values: Sequence[object]
        if allowed_addresses is None:  # type: ignore[comparison-overlap]
            address_values = ()
        elif isinstance(allowed_addresses, str):
            address_values = (allowed_addresses,)
        else:
            address_values = allowed_addresses
        for address in address_values:
            try:
                addresses.add(str(ipaddress.ip_address(address)))
            except ValueError as exc:
                raise AdapterConfigurationError(
                    "allowed provider address is invalid"
                ) from exc
        self._allowed_addresses = frozenset(addresses)
        if not isinstance(allow_private_addresses, bool):
            raise AdapterConfigurationError("private-address policy is invalid")
        self._allow_private_addresses = allow_private_addresses
        self._dns_lock = threading.Lock()
        self._pinned_dns: dict[tuple[str, int], frozenset[str]] = {}

    @staticmethod
    def _origin_tuple(url: str) -> tuple[str, str, int]:
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise AdapterConfigurationError(
                "provider URL is outside the configured origin"
            )
        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise AdapterConfigurationError("provider URL has an invalid port") from exc
        return parsed.scheme.lower(), parsed.hostname.rstrip(".").lower(), port

    @staticmethod
    def _resolve_dns(
        host: str,
        port: int,
        *,
        deadline_at: float | None,
    ) -> list[tuple[object, ...]]:
        """Resolve one configured origin without escaping the request deadline.

        ``socket.getaddrinfo`` has no portable per-call timeout.  A daemon
        resolver thread lets the bounded request path stop waiting at the
        absolute deadline; the resolver itself cannot keep the worker process
        alive if a platform resolver is wedged.
        """

        if deadline_at is None:
            return cast(
                list[tuple[object, ...]],
                socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            )
        result: list[tuple[object, ...]] = []
        failure: list[Exception] = []
        completed = threading.Event()

        def resolve() -> None:
            try:
                value = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                result.extend(cast(list[tuple[object, ...]], value))
            except Exception as exc:  # noqa: BLE001
                failure.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(target=resolve, name="companion-dns", daemon=True)
        thread.start()
        remaining = max(0.0, deadline_at - time.monotonic())
        if not completed.wait(remaining):
            raise AdapterTimeoutError(
                "provider DNS resolution exceeded the total deadline"
            )
        if failure:
            raise failure[0]
        return result

    def _validate_origin_and_dns(
        self, url: str, *, deadline_at: float | None = None
    ) -> None:
        if self._allowed_origin is None:
            return
        actual = self._origin_tuple(url)
        expected = self._origin_tuple(self._allowed_origin)
        if actual != expected:
            raise AdapterConfigurationError(
                "provider URL is outside the configured origin"
            )
        host, port = actual[1], actual[2]
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            current = frozenset({str(literal)})
        else:
            try:
                resolved = self._resolve_dns(host, port, deadline_at=deadline_at)
            except OSError as exc:
                raise AdapterTransportError("provider DNS resolution failed") from exc
            addresses: set[str] = set()
            for info in resolved:
                if len(info) < 5:
                    continue
                sockaddr = info[4]
                if not isinstance(sockaddr, tuple) or not sockaddr:
                    continue
                address = sockaddr[0]
                if isinstance(address, str) and address:
                    addresses.add(str(ipaddress.ip_address(address)))
            current = frozenset(addresses)
            if not current:
                raise AdapterTransportError(
                    "provider DNS resolution returned no addresses"
                )
        if self._allowed_addresses and not current.issubset(self._allowed_addresses):
            raise AdapterConfigurationError(
                "provider DNS result is outside the configured address allowlist"
            )
        if not self._allow_private_addresses:
            for address in current:
                parsed_address = ipaddress.ip_address(address)
                if (
                    parsed_address.is_private
                    or parsed_address.is_loopback
                    or parsed_address.is_link_local
                    or parsed_address.is_reserved
                    or parsed_address.is_unspecified
                ):
                    raise AdapterConfigurationError(
                        "provider DNS result is a private or special-use address"
                    )
        key = (host, port)
        with self._dns_lock:
            previous = self._pinned_dns.get(key)
            if previous is None:
                self._pinned_dns[key] = current
            elif previous != current:
                raise AdapterTransportError(
                    "provider DNS result changed during the configured origin lifetime"
                )

    def _breaker_before(self) -> None:
        now = time.monotonic()
        with self._breaker_lock:
            if now < self._opened_until:
                raise AdapterCircuitOpenError("provider circuit is temporarily open")

    def _breaker_success(self) -> None:
        with self._breaker_lock:
            self._failure_count = 0
            self._opened_until = 0.0

    def _breaker_failure(self) -> None:
        with self._breaker_lock:
            self._failure_count += 1
            if self._failure_count >= self._breaker_failures:
                self._opened_until = time.monotonic() + self._breaker_seconds

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json_body: object | None = None,
        body: bytes | None = None,
        max_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    ) -> HTTPResponse:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        started = time.monotonic()
        deadline_at = started + self.timeouts.total_seconds
        try:
            self._validate_origin_and_dns(url, deadline_at=deadline_at)
        except (AdapterTransportError, AdapterTimeoutError):
            self._breaker_failure()
            raise
        self._breaker_before()
        wait_budget = self.timeouts.total_seconds - (time.monotonic() - started)
        if wait_budget <= 0:
            self._breaker_failure()
            raise AdapterTimeoutError("provider request exceeded the total deadline")
        acquired = self._concurrency.acquire(timeout=max(0.001, wait_budget))
        if not acquired:
            self._breaker_failure()
            raise AdapterTransportError("provider concurrency limit was reached")
        remaining = self.timeouts.total_seconds - (time.monotonic() - started)
        if remaining <= 0:
            self._concurrency.release()
            self._breaker_failure()
            raise AdapterTimeoutError("provider request exceeded the total deadline")
        try:
            response = self.session.request(
                method.upper(),
                url,
                headers=dict(headers or {}),
                params=cast(Any, dict(params or {})),
                json=json_body,
                data=body,
                # ``requests`` interprets the tuple as connect/read.  The
                # bounded read timeout is deliberately the smaller body/total
                # deadline; the monotonic checks below enforce the absolute
                # deadline between chunks as well.
                timeout=(
                    min(self.timeouts.connect_seconds, remaining),
                    min(self.timeouts.body_seconds, remaining),
                ),
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout as exc:
            self._breaker_failure()
            raise AdapterTimeoutError(
                f"{method.upper()} {redact_text(url, max_bytes=256)} exceeded the provider deadline"
            ) from exc
        except requests.RequestException as exc:
            self._breaker_failure()
            raise AdapterTransportError(
                f"{method.upper()} {redact_text(url, max_bytes=256)} transport failed: {redact_text(str(exc), max_bytes=384)}"
            ) from exc

        try:
            status = int(getattr(response, "status_code", 0))
            response_headers = {
                str(key).lower(): redact_text(str(value), max_bytes=512)
                for key, value in dict(getattr(response, "headers", {}) or {}).items()
                if str(key).lower()
                not in {"authorization", "proxy-authorization", "set-cookie"}
                and not is_secret_key(str(key))
            }
            raw_length = response_headers.get("content-length")
            if raw_length is not None:
                try:
                    if int(raw_length) > max_bytes:
                        raise AdapterResponseError(
                            "provider response exceeds the bounded body limit"
                        )
                except ValueError:
                    # An invalid length is not trusted; the streaming bound below
                    # remains authoritative.
                    pass

            chunks: list[bytes] = []
            size = 0
            iterator = getattr(response, "iter_content", None)
            if callable(iterator):
                for chunk in iterator(64 * 1024):
                    if time.monotonic() - started > self.timeouts.total_seconds:
                        raise AdapterTimeoutError(
                            "provider response exceeded the total deadline"
                        )
                    if not chunk:
                        continue
                    if not isinstance(chunk, bytes):
                        if isinstance(chunk, (bytearray, memoryview)):
                            chunk = bytes(chunk)
                        else:
                            raise AdapterResponseError(
                                "provider response contained a non-byte chunk"
                            )
                    size += len(chunk)
                    if size > max_bytes:
                        raise AdapterResponseError(
                            "provider response exceeds the bounded body limit"
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
            else:
                content_value = getattr(response, "content", b"")
                if isinstance(content_value, bytes):
                    content = content_value
                elif isinstance(content_value, (bytearray, memoryview)):
                    content = bytes(content_value)
                else:
                    raise AdapterResponseError("provider response body is not bytes")
                if len(content) > max_bytes:
                    raise AdapterResponseError(
                        "provider response exceeds the bounded body limit"
                    )
            if time.monotonic() - started > self.timeouts.total_seconds:
                raise AdapterTimeoutError(
                    "provider response exceeded the total deadline"
                )
            if status >= 500:
                self._breaker_failure()
            else:
                self._breaker_success()
            return HTTPResponse(status, response_headers, content, None)
        except requests.Timeout as exc:
            self._breaker_failure()
            raise AdapterTimeoutError(
                "provider response exceeded the total deadline", transmitted=True
            ) from exc
        except requests.RequestException as exc:
            self._breaker_failure()
            raise AdapterTransportError(
                "provider response transport failed", transmitted=True
            ) from exc
        except (AdapterTimeoutError, AdapterResponseError):
            self._breaker_failure()
            raise
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            self._concurrency.release()


RequestsTransport = _ConfiguredHTTPTransport
ConfiguredHTTPTransport = _ConfiguredHTTPTransport


def _endpoint_url(
    endpoint: ServiceEndpoint | str | None, *, name: str, config: object | None
) -> str:
    candidate: object | None = endpoint
    if candidate is None and config is not None:
        candidate = getattr(config, name, None)
        if candidate is None:
            candidate = getattr(config, f"{name}_url", None)
    if isinstance(candidate, ServiceEndpoint):
        return candidate.origin
    if isinstance(candidate, str):
        return normalize_url(candidate, field_name=f"{name}_url")
    raise AdapterConfigurationError(f"{name} origin is not configured")


def _secret_value(
    secret: str | SecretFileRef | Path | None,
    *,
    config: object | None,
    field_name: str,
    reader: SecretReader | Callable[[object], str] | None,
) -> str:
    value: object | None = secret
    if value is None and config is not None:
        value = getattr(config, f"{field_name}_file", None)
        if value is None:
            value = getattr(config, f"{field_name}_secret_file", None)
        if value is None and field_name.endswith("_api_key"):
            value = getattr(config, f"{field_name[:-8]}_secret_file", None)
    if value is None:
        raise AdapterConfigurationError(f"{field_name} credential is not configured")
    if (
        isinstance(value, str)
        and not value.startswith("/")
        and not value.startswith("file:")
    ):
        # Direct construction is useful in tests and an integration may supply
        # a secret manager.  The config loader rejects this form before runtime.
        result = value.strip()
    else:
        actual_reader: SecretReader | Callable[[object], str] = (
            reader or FileSecretReader()
        )
        try:
            if callable(actual_reader) and not hasattr(actual_reader, "read_secret"):
                result = actual_reader(value)
            else:
                result = cast(SecretReader, actual_reader).read_secret(
                    cast(SecretFileRef | Path | str, value)
                )
        except (OSError, ValueError) as exc:
            raise AdapterConfigurationError(
                f"{field_name} credential is unavailable"
            ) from exc
    if not isinstance(result, str) or not result.strip():
        raise AdapterConfigurationError(f"{field_name} credential is empty")
    return result.strip()


def _response_status(response: object) -> int:
    raw_status = getattr(response, "status_code", 0)
    if isinstance(raw_status, bool) or not isinstance(raw_status, (int, str)):
        return 0
    try:
        return int(raw_status)
    except ValueError:
        return 0


def _response_body(response: object) -> bytes:
    raw_body = getattr(response, "body", None)
    if raw_body is None:
        raw_body = getattr(response, "content", b"")
    if isinstance(raw_body, bytes):
        return raw_body
    if isinstance(raw_body, (bytearray, memoryview)):
        return bytes(raw_body)
    return b""


def _response_json(response: object) -> Any:
    parser = getattr(response, "json", None)
    if callable(parser):
        try:
            return parser()
        except AdapterResponseError:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise AdapterResponseError("provider returned invalid JSON") from exc
    body = _response_body(response)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterResponseError("provider returned invalid JSON") from exc


def _json_response(response: object, *, service: str) -> Any:
    status_code = _response_status(response)
    if status_code < 200:
        raise AdapterHTTPError(
            service, status_code, "provider returned an invalid HTTP status"
        )
    if 300 <= status_code < 400:
        raise AdapterHTTPError(service, status_code, "redirects are disabled")
    if status_code >= 400:
        detail = ""
        try:
            payload = _response_json(response)
            if isinstance(payload, Mapping):
                detail = str(
                    payload.get("message")
                    or payload.get("errorMessage")
                    or payload.get("error")
                    or ""
                )
            elif payload is not None:
                detail = str(payload)
        except AdapterResponseError:
            detail = _response_body(response).decode("utf-8", "replace")[:512]
        raise AdapterHTTPError(service, status_code, detail or "upstream error")
    return _response_json(response)


def _positive_int(
    value: object, field_name: str, *, optional: bool = False
) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if optional:
            return None
        raise AdapterResponseError(f"provider {field_name} is invalid")
    return value


def _nonnegative_int(
    value: object, field_name: str, *, optional: bool = False
) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        if optional:
            return None
        raise AdapterResponseError(f"provider {field_name} is invalid")
    return value


def _text(
    value: object, *, fallback: str | None = None, max_bytes: int = 512
) -> str | None:
    if not isinstance(value, str):
        return fallback
    cleaned = redact_text(value, max_bytes=max_bytes).strip()
    return cleaned or fallback


def _bool(value: object) -> bool:
    return value is True


@dataclass(frozen=True, slots=True)
class RadarrDefaults:
    """Server-owned Radarr request policy; callers cannot override it per call."""

    quality_profile_id: int | None = None
    quality_profile_name: str | None = None
    root_folder_path: str | None = None
    tag_ids: tuple[int, ...] = ()
    monitored: bool = True
    minimum_availability: str = "announced"
    search_for_movie: bool = True

    def __post_init__(self) -> None:
        if self.quality_profile_id is not None:
            _positive_int(self.quality_profile_id, "quality_profile_id")
        if self.root_folder_path is not None:
            if (
                not isinstance(self.root_folder_path, str)
                or not self.root_folder_path.startswith("/")
                or any(ord(character) < 0x20 for character in self.root_folder_path)
                or "?" in self.root_folder_path
                or "#" in self.root_folder_path
                or ".." in self.root_folder_path.split("/")
            ):
                raise ModelValidationError(
                    "root_folder_path must be an absolute configured path"
                )
        tags: list[int] = []
        for tag in self.tag_ids:
            parsed = _positive_int(tag, "tag_id")
            assert parsed is not None
            tags.append(parsed)
        object.__setattr__(self, "tag_ids", tuple(dict.fromkeys(tags)))
        if not isinstance(self.monitored, bool) or not isinstance(
            self.search_for_movie, bool
        ):
            raise ModelValidationError(
                "monitored and search_for_movie must be booleans"
            )


@dataclass(frozen=True, slots=True)
class RadarrMovie:
    """Allowlisted movie fields used by request/status workflows."""

    id: int | None
    tmdb_id: int | None
    imdb_id: str | None
    title: str
    year: int | None = None
    overview: str | None = None
    has_file: bool = False
    monitored: bool = False
    quality: str | None = None

    @property
    def provider_id(self) -> int:
        if self.tmdb_id is None:
            raise AdapterResponseError("Radarr movie has no TMDB identity")
        return self.tmdb_id

    @property
    def identity(self) -> MediaIdentity:
        return MediaIdentity(
            MediaType.MOVIE,
            tmdb_id=self.tmdb_id,
            imdb_id=self.imdb_id,
            # The Arr ``id`` is an internal object key, never a stable media
            # identity and must not escape as a candidate/provider handle.
            provider_id=None,
        )


@dataclass(frozen=True, slots=True)
class RadarrQueueRecord:
    """Internal bounded queue record; raw queue/download IDs are not exposed."""

    item: QueueItem
    provider_id: int | None = None


def _movie_from_mapping(value: Mapping[str, object]) -> RadarrMovie:
    movie_id = _positive_int(value.get("id"), "id", optional=True)
    tmdb_id = _positive_int(value.get("tmdbId"), "tmdbId", optional=True)
    title = _text(value.get("title"), fallback="Movie") or "Movie"
    year = _positive_int(value.get("year"), "year", optional=True)
    imdb_id = _text(value.get("imdbId"), max_bytes=32)
    overview = _text(value.get("overview"), max_bytes=2048)
    quality = _text(value.get("qualityProfileName"), max_bytes=128)
    has_file = (
        _bool(value.get("hasFile"))
        or _positive_int(value.get("movieFileId"), "movieFileId", optional=True)
        is not None
        or isinstance(value.get("movieFile"), Mapping)
    )
    return RadarrMovie(
        movie_id,
        tmdb_id,
        imdb_id,
        title,
        year,
        overview,
        has_file,
        _bool(value.get("monitored")),
        quality,
    )


def _records(value: object) -> list[Mapping[str, object]]:
    source: object = value.get("records") if isinstance(value, Mapping) else value
    if not isinstance(source, Sequence) or isinstance(source, (str, bytes, bytearray)):
        return []
    return [item for item in source if isinstance(item, Mapping)]


def _queue_state(value: Mapping[str, object]) -> QueueState:
    status = str(
        value.get("status") or value.get("trackedDownloadState") or "unknown"
    ).lower()
    if status in {"queued", "pending"}:
        return QueueState.QUEUED
    if status in {"downloading", "downloaded"}:
        return QueueState.DOWNLOADING
    if status in {"importpending", "importing", "importblocked"}:
        return QueueState.IMPORTING
    if status in {"paused", "pause"}:
        return QueueState.PAUSED
    if status in {"failed", "error"}:
        return QueueState.FAILED
    if status in {"completed", "complete", "done"}:
        return QueueState.COMPLETED
    return QueueState.UNKNOWN


def _progress(value: Mapping[str, object]) -> float | None:
    raw = value.get("progressPercent", value.get("progress"))
    if (
        isinstance(raw, (int, float))
        and not isinstance(raw, bool)
        and math.isfinite(float(raw))
    ):
        return max(0.0, min(100.0, float(raw)))
    size = value.get("size")
    remaining = value.get("sizeleft", value.get("sizeLeft"))
    if (
        isinstance(size, (int, float))
        and isinstance(remaining, (int, float))
        and float(size) > 0
    ):
        return max(
            0.0, min(100.0, 100.0 * (float(size) - float(remaining)) / float(size))
        )
    return None


def _eta_seconds(value: object) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(?:(\d+)\s*:)??(\d{1,2}):(\d{2})\s*", value)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    return hours * 3600 + int(match.group(2)) * 60 + int(match.group(3))


class RadarrClient:
    """Configured-origin Radarr v3 client with typed allowlisted methods."""

    service_name = "radarr"

    def __init__(
        self,
        endpoint: ServiceEndpoint | str | None = None,
        api_key: str | SecretFileRef | Path | None = None,
        *,
        config: object | None = None,
        secret_reader: SecretReader | Callable[[object], str] | None = None,
        transport: HttpTransport | None = None,
        defaults: RadarrDefaults | None = None,
        policy: RadarrDefaults | None = None,
        timeouts: TimeoutConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = _endpoint_url(endpoint, name="radarr", config=config)
        self.api_key = _secret_value(
            api_key, config=config, field_name="radarr_api_key", reader=secret_reader
        )
        self.defaults = policy or defaults or _defaults_from_config(config)
        configured_timeouts = timeouts or getattr(config, "timeouts", None)
        self.transport: HttpTransport = transport or _ConfiguredHTTPTransport(
            timeouts=configured_timeouts,
            session=session,
            allowed_origin=self.base_url,
            allowed_addresses=getattr(config, "radarr_allowed_addresses", ())
            if config is not None
            else (),
            allow_private_addresses=True,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        payload: Mapping[str, object] | None = None,
        max_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    ) -> Any:
        if not path.startswith("/") or "?" in path or "#" in path or ".." in path:
            raise ValueError("Radarr path must be a fixed API path")
        response = self.transport.request(
            method,
            self.base_url + path,
            headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
            params=params,
            json_body=payload,
            max_bytes=max_bytes,
        )
        if len(_response_body(response)) > max_bytes:
            raise AdapterResponseError("Radarr response exceeds the bounded body limit")
        return _json_response(response, service=self.service_name)

    def system_status(self) -> Mapping[str, object]:
        result = self._request("GET", "/api/v3/system/status")
        source = _typed_mapping(result, "Radarr system status")
        # Keep health diagnostics typed and bounded; provider-specific fields
        # (paths, URLs, build metadata, and future secrets) do not cross this
        # adapter boundary.
        return {
            "version": _text(source.get("version"), max_bytes=64),
            "branch": _text(source.get("branch"), max_bytes=64),
            "instance_name": _text(source.get("instanceName"), max_bytes=128),
            "is_up": True,
        }

    def list_movies(self) -> tuple[RadarrMovie, ...]:
        result = self._request("GET", "/api/v3/movie")
        return tuple(
            _movie_from_mapping(item) for item in _records(result) or _records([result])
        )

    def get_movie(self, movie_id: int) -> RadarrMovie:
        movie_id = _positive_int(movie_id, "movie_id") or 0
        result = self._request("GET", f"/api/v3/movie/{movie_id}")
        return _movie_from_mapping(_typed_mapping(result, "Radarr movie"))

    def find_existing_movie(self, tmdb_id: int) -> RadarrMovie | None:
        validated_tmdb_id = _positive_int(tmdb_id, "tmdb_id")
        assert validated_tmdb_id is not None
        movies = self.list_movies()
        return next(
            (movie for movie in movies if movie.tmdb_id == validated_tmdb_id), None
        )

    # Friendly aliases used by callers that distinguish a provider lookup from
    # a library search.
    get_existing_movie = find_existing_movie
    lookup_existing_movie = find_existing_movie

    def lookup_movie(
        self, tmdb_id: int | None = None, *, query: str | None = None
    ) -> tuple[RadarrMovie, ...]:
        if tmdb_id is not None:
            tmdb_id = _positive_int(tmdb_id, "tmdb_id")
        if tmdb_id is None and (not isinstance(query, str) or not query.strip()):
            raise ValueError("tmdb_id or query is required")
        query_value = query.strip() if isinstance(query, str) else ""
        params = (
            {"term": f"tmdb:{tmdb_id}"}
            if tmdb_id is not None
            else {"term": query_value}
        )
        result = self._request("GET", "/api/v3/movie/lookup", params=params)
        return tuple(_movie_from_mapping(item) for item in _records(result))

    def search_movie(self, query: str, *, limit: int = 25) -> Page[MediaCandidate]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be blank")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SEARCH_RESULTS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
        result = self.lookup_movie(query=query)
        candidates: list[MediaCandidate] = []
        for movie in result[:limit]:
            provider_id = movie.tmdb_id
            if provider_id is None:
                continue
            candidates.append(
                MediaCandidate(
                    MediaType.MOVIE,
                    provider_id,
                    movie.title,
                    movie.year,
                    movie.overview,
                    movie.identity,
                )
            )
        return Page(
            items=tuple(candidates), total=len(result), truncated=len(result) > limit
        )

    def add_movie(
        self, movie: RadarrMovie | Mapping[str, object], *, tmdb_id: int | None = None
    ) -> RadarrMovie:
        if isinstance(movie, RadarrMovie):
            source: Mapping[str, object] = {
                "title": movie.title,
                "year": movie.year,
                "tmdbId": movie.tmdb_id,
                "imdbId": movie.imdb_id,
                "overview": movie.overview,
            }
        else:
            source = movie
        if not isinstance(source, Mapping):
            raise ValueError("movie metadata must be a mapping")
        explicit_id = _positive_int(tmdb_id, "tmdb_id") if tmdb_id is not None else None
        identifier = explicit_id or (
            movie.tmdb_id
            if isinstance(movie, RadarrMovie)
            else _positive_int(source.get("tmdbId"), "tmdbId", optional=True)
        )
        if identifier is None:
            raise ValueError("tmdb_id is required")
        payload: dict[str, object] = {
            key: value
            for key, value in source.items()
            if value is not None
            and key
            in {
                "title",
                "year",
                "tmdbId",
                "imdbId",
                "titleSlug",
                "overview",
                "studio",
                "genres",
                "images",
                "cleanTitle",
                "sortTitle",
            }
        }
        payload["tmdbId"] = identifier
        payload["monitored"] = self.defaults.monitored
        payload["minimumAvailability"] = self.defaults.minimum_availability
        payload["addOptions"] = {"searchForMovie": self.defaults.search_for_movie}
        if self.defaults.quality_profile_id is not None:
            payload["qualityProfileId"] = self.defaults.quality_profile_id
        if self.defaults.root_folder_path is not None:
            payload["rootFolderPath"] = self.defaults.root_folder_path
        payload["tags"] = list(self.defaults.tag_ids)
        result = self._request("POST", "/api/v3/movie", payload=payload)
        normalized = _movie_from_mapping(_typed_mapping(result, "Radarr add movie"))
        if normalized.tmdb_id != identifier:
            raise AdapterResponseError(
                "Radarr add response did not match the requested TMDB identity"
            )
        return normalized

    def queue(
        self, *, page: int = 1, page_size: int = DEFAULT_QUEUE_PAGE_SIZE
    ) -> tuple[RadarrQueueRecord, ...]:
        if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
            raise ValueError("page must be a positive integer")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_QUEUE_PAGE_SIZE
        ):
            raise ValueError(f"page_size must be between 1 and {MAX_QUEUE_PAGE_SIZE}")
        result = self._request(
            "GET", "/api/v3/queue", params={"page": page, "pageSize": page_size}
        )
        records: list[RadarrQueueRecord] = []
        for value in _records(result)[:MAX_QUEUE_ITEMS]:
            title = _text(value.get("title"), fallback=None)
            nested = value.get("movie")
            if title is None and isinstance(nested, Mapping):
                title = _text(nested.get("title"), fallback=None)
            if title is None:
                title = "Radarr item"
            provider_id = _positive_int(value.get("movieId"), "movieId", optional=True)
            if provider_id is None and isinstance(nested, Mapping):
                provider_id = _positive_int(nested.get("id"), "id", optional=True)
            error = _text(
                value.get("errorMessage") or value.get("statusMessages"), max_bytes=512
            )
            queue_item = QueueItem(
                ServiceName.RADARR,
                title,
                _queue_state(value),
                _progress(value),
                _eta_seconds(value.get("timeleft", value.get("timeLeft"))),
                error,
                MediaType.MOVIE,
            )
            records.append(RadarrQueueRecord(queue_item, provider_id))
        return tuple(records)

    queue_items = queue
    get_queue = queue

    def health(self) -> bool:
        try:
            self.system_status()
        except AdapterError:
            return False
        return True


def _typed_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AdapterResponseError(f"{label} returned an unexpected shape")
    return cast(Mapping[str, object], value)


def _defaults_from_config(config: object | None) -> RadarrDefaults:
    if config is None:
        return RadarrDefaults()
    profile_id = _positive_int(
        getattr(config, "radarr_quality_profile_id", None),
        "quality_profile_id",
        optional=True,
    )
    profile_name = _text(
        getattr(config, "radarr_quality_profile_name", None), max_bytes=128
    )
    raw_root = getattr(config, "radarr_root_folder_path", None)
    root = raw_root.strip() if isinstance(raw_root, str) and raw_root.strip() else None
    tags_value = getattr(config, "radarr_tag_ids", ())
    tags: list[int] = []
    if isinstance(tags_value, str):
        for token in tags_value.split(","):
            parsed = (
                _positive_int(int(token.strip()), "tag_id", optional=True)
                if token.strip().isdigit()
                else None
            )
            if parsed is not None:
                tags.append(parsed)
    elif isinstance(tags_value, Sequence):
        for token in tags_value:
            parsed = _positive_int(token, "tag_id", optional=True)
            if parsed is not None:
                tags.append(parsed)
    return RadarrDefaults(profile_id, profile_name, root, tuple(tags))


__all__ = [
    "AdapterConfigurationError",
    "AdapterCircuitOpenError",
    "AdapterError",
    "AdapterHTTPError",
    "AdapterResponseError",
    "AdapterTimeoutError",
    "AdapterTransportError",
    "ConfiguredHTTPTransport",
    "FileSecretReader",
    "HTTPResponse",
    "HttpTransport",
    "MAX_JSON_RESPONSE_BYTES",
    "MAX_PROVIDER_RESPONSE_BYTES",
    "RadarrClient",
    "RadarrDefaults",
    "RadarrMovie",
    "RadarrQueueRecord",
    "RequestsTransport",
    "SecretReader",
]
