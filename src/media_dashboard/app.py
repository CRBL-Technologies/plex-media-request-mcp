"""Authenticated operations dashboard HTTP service.

This is a deliberately small stdlib HTTP application.  It owns browser
sessions and renders a no-script UI, while :class:`DashboardCompanionClient`
owns the only private data boundary.  No handler opens an environment file,
Hermes log, SQLite database, MCP connection, or provider endpoint.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import http.cookies
import http.server
import inspect
import json
import os
import re
import stat
import threading
import time
from typing import Any, Final, TypeAlias
from urllib.parse import parse_qs, unquote_to_bytes, urlsplit

from .auth import (
    InvalidRequestOrigin,
    SessionStore,
    hash_password,
    validate_password_hash,
    validate_request_origin,
    verify_password,
)
from .companion import (
    ALLOWED_OPERATIONS,
    CompanionClientError,
    CompanionConfigurationError,
    CompanionResponse,
    DashboardCompanionClient,
    OP_ASSUME_SENT,
    OP_AUDIT,
    OP_BLOCKED,
    OP_DELIVERIES,
    OP_HEALTH,
    OP_MARK_ABANDONED,
    OP_ORACLE,
    OP_QUARANTINE,
    OP_RESEND_ONCE,
    OP_RETRY_ONCE,
    OP_SUBSCRIPTIONS,
    OP_USERS,
    OP_USERS_RESOLVE,
    OP_USERS_ADD,
    OP_USERS_REMOVE,
    READ_OPERATIONS,
    RECOVERY_OPERATIONS,
    _sanitize_operation_response,
    _validate_base_url,
    load_api_key_file,
)
from .views import dashboard as dashboard_view
from .views import error_page
from .views import login as login_view
from .views import operation_result as operation_result_view


DEFAULT_PORT: Final[int] = 18082
PORT: Final[int] = DEFAULT_PORT
DEFAULT_HOST: Final[str] = "0.0.0.0"
DEFAULT_MAX_BODY_BYTES: Final[int] = 64 * 1024
DEFAULT_LOGIN_BODY_BYTES: Final[int] = 4 * 1024
DEFAULT_MAX_RESPONSE_BYTES: Final[int] = 256 * 1024
MAX_BODY_BYTES: Final[int] = DEFAULT_MAX_BODY_BYTES
MAX_RESPONSE_BYTES: Final[int] = DEFAULT_MAX_RESPONSE_BYTES
DEFAULT_LOGIN_LIMIT: Final[int] = 5
DEFAULT_LOGIN_WINDOW_SECONDS: Final[int] = 5 * 60
DEFAULT_ACTION_LIMIT: Final[int] = 5
DEFAULT_ACTION_WINDOW_SECONDS: Final[int] = 10 * 60
DEFAULT_RECOVERY_LIMIT: Final[int] = DEFAULT_ACTION_LIMIT
DEFAULT_RECOVERY_WINDOW_SECONDS: Final[int] = DEFAULT_ACTION_WINDOW_SECONDS
DEFAULT_READ_LIMIT: Final[int] = 30
DEFAULT_READ_WINDOW_SECONDS: Final[int] = 60
DEFAULT_GLOBAL_READ_LIMIT: Final[int] = 240
DEFAULT_GLOBAL_READ_WINDOW_SECONDS: Final[int] = 60
DEFAULT_GLOBAL_ACTION_LIMIT: Final[int] = 30
DEFAULT_GLOBAL_ACTION_WINDOW_SECONDS: Final[int] = 10 * 60
MAX_LOGIN_LIMIT: Final[int] = 20
MAX_LOGIN_WINDOW_SECONDS: Final[int] = 15 * 60
MAX_ACTION_LIMIT: Final[int] = 5
MAX_ACTION_WINDOW_SECONDS: Final[int] = 10 * 60
SESSION_COOKIE: Final[str] = "dashboard_session"
CSRF_COOKIE: Final[str] = "dashboard_csrf"
MAX_QUERY_VALUE_BYTES: Final[int] = 4096
MAX_PAGE_LIMIT: Final[int] = 250
DASHBOARD_OPERATIONS: Final[frozenset[str]] = ALLOWED_OPERATIONS
_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"^(0|[1-9][0-9]*)$")
_IDEMPOTENCY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9:_-]{1,256}$")
_BAD_PERCENT_RE: Final[re.Pattern[str]] = re.compile(r"%(?![0-9A-Fa-f]{2})")

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


class DashboardConfigurationError(ValueError):
    """Dashboard configuration is absent or outside a safe bound."""


class DashboardHTTPError(Exception):
    """Internal status-only exception; its message is never sent to a client."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status


def _text(value: object, field_name: str, *, max_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value:
        raise DashboardConfigurationError(
            f"invalid dashboard configuration: {field_name}"
        )
    try:
        value_bytes = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise DashboardConfigurationError(
            f"invalid dashboard configuration: {field_name}"
        ) from exc
    if len(value_bytes) > max_bytes:
        raise DashboardConfigurationError(
            f"invalid dashboard configuration: {field_name}"
        )
    if any(ord(char) < 0x20 for char in value):
        raise DashboardConfigurationError(
            f"invalid dashboard configuration: {field_name}"
        )
    return value


def _positive_int(value: object, field_name: str, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise DashboardConfigurationError(
            f"invalid dashboard configuration: {field_name}"
        )
    return value


def _origin_authority(origin: str) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise DashboardConfigurationError(
            "invalid dashboard configuration: allowed_origins"
        )
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DashboardConfigurationError(
            "invalid dashboard configuration: allowed_origins"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise DashboardConfigurationError(
            "invalid dashboard configuration: allowed_origins"
        ) from exc
    default_port = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname.lower()
    if "%" in host or any(ord(char) < 0x21 or char.isspace() for char in host):
        raise DashboardConfigurationError(
            "invalid dashboard configuration: allowed_origins"
        )
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and port != default_port:
        host = f"{host}:{port}"
    return host


def _split_origins(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        pieces = tuple(piece.strip() for piece in value.split(","))
        if any(not piece for piece in pieces):
            raise DashboardConfigurationError(
                "invalid dashboard configuration: allowed_origins"
            )
        values = pieces
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = tuple(piece.strip() for piece in value if isinstance(piece, str))
        if any(not piece for piece in values) or len(values) != len(value):
            raise DashboardConfigurationError(
                "invalid dashboard configuration: allowed_origins"
            )
    else:
        values = ()
    if not values or len(values) > 16:
        raise DashboardConfigurationError(
            "invalid dashboard configuration: allowed_origins"
        )
    authorities = {_origin_authority(origin) for origin in values}
    if len(authorities) != len(values):
        raise DashboardConfigurationError(
            "invalid dashboard configuration: allowed_origins"
        )
    return tuple(values)


def _json_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _canonical_secret_path(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise DashboardConfigurationError(
            "invalid dashboard configuration: secret_file"
        )
    normalized = os.path.normpath(value)
    if normalized != value or normalized in {"", "/", ".", ".."}:
        raise DashboardConfigurationError(
            "invalid dashboard configuration: secret_file"
        )
    return value


def _read_bounded_secret(path: str, *, maximum: int = 4096) -> bytes:
    """Read one regular, non-symlink, restrictive secret file."""

    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_size > maximum
            ):
                raise OSError("secret file policy")
            raw = os.read(fd, maximum + 1)
            if len(raw) > maximum or os.read(fd, 1):
                raise OSError("secret file size")
            return raw
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError) as exc:
        raise DashboardConfigurationError(
            "invalid dashboard configuration: secret_file"
        ) from exc


def _validate_companion_key(value: object) -> bytes:
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise DashboardConfigurationError(
                "invalid dashboard configuration: companion_api_key"
            ) from exc
        if any(ord(char) < 0x20 for char in value):
            raise DashboardConfigurationError(
                "invalid dashboard configuration: companion_api_key"
            )
    elif isinstance(value, (bytes, bytearray, memoryview)):
        encoded = bytes(value)
    else:
        raise DashboardConfigurationError(
            "invalid dashboard configuration: companion_api_key"
        )
    if not 32 <= len(encoded) <= 4096:
        raise DashboardConfigurationError(
            "invalid dashboard configuration: companion_api_key"
        )
    return encoded


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    """Validated dashboard settings.

    ``password_hash`` and ``companion_api_key`` are accepted for dependency
    injection in tests and startup code.  Production should load them from the
    two dedicated mounted secret files via :meth:`from_env`.
    """

    password_hash: str
    allowed_origins: tuple[str, ...]
    companion_url: str = "http://media-companion:18080"
    companion_api_key: bytes | str | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    login_body_bytes: int = DEFAULT_LOGIN_BODY_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    login_limit: int = DEFAULT_LOGIN_LIMIT
    login_window_seconds: int = DEFAULT_LOGIN_WINDOW_SECONDS
    action_limit: int = DEFAULT_ACTION_LIMIT
    action_window_seconds: int = DEFAULT_ACTION_WINDOW_SECONDS
    read_limit: int = DEFAULT_READ_LIMIT
    read_window_seconds: int = DEFAULT_READ_WINDOW_SECONDS
    global_read_limit: int = DEFAULT_GLOBAL_READ_LIMIT
    global_read_window_seconds: int = DEFAULT_GLOBAL_READ_WINDOW_SECONDS
    global_action_limit: int = DEFAULT_GLOBAL_ACTION_LIMIT
    global_action_window_seconds: int = DEFAULT_GLOBAL_ACTION_WINDOW_SECONDS
    cookie_secure: bool | None = None
    session_idle_seconds: int = 30 * 60
    session_absolute_seconds: int = 12 * 60 * 60
    session_max: int = 64

    def __post_init__(self) -> None:
        password_hash_value = _text(self.password_hash, "password_hash", max_bytes=4096)
        # Parse the complete encoding at startup; a malformed hash must not
        # survive until the first login request.
        try:
            validate_password_hash(password_hash_value)
        except ValueError as exc:
            raise DashboardConfigurationError(
                "invalid dashboard configuration: password_hash"
            ) from exc
        object.__setattr__(self, "password_hash", password_hash_value)
        if self.companion_api_key is not None:
            object.__setattr__(
                self,
                "companion_api_key",
                _validate_companion_key(self.companion_api_key),
            )
        object.__setattr__(
            self, "allowed_origins", _split_origins(self.allowed_origins)
        )
        if (
            not isinstance(self.host, str)
            or not self.host
            or any(ord(char) < 0x20 or char.isspace() for char in self.host)
        ):
            raise DashboardConfigurationError("invalid dashboard configuration: host")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or self.port < 0
            or self.port > 65535
        ):
            raise DashboardConfigurationError("invalid dashboard configuration: port")
        # Port zero is reserved for tests; production's default and only
        # documented listener is 18082.
        if self.port not in {0, DEFAULT_PORT}:
            raise DashboardConfigurationError("invalid dashboard configuration: port")
        try:
            normalized_companion_url = _validate_base_url(self.companion_url)
        except CompanionConfigurationError as exc:
            raise DashboardConfigurationError(
                "invalid dashboard configuration: companion_url"
            ) from exc
        object.__setattr__(self, "companion_url", normalized_companion_url)
        for name, value, maximum in (
            ("max_body_bytes", self.max_body_bytes, DEFAULT_MAX_BODY_BYTES),
            ("login_body_bytes", self.login_body_bytes, DEFAULT_LOGIN_BODY_BYTES),
            ("max_response_bytes", self.max_response_bytes, DEFAULT_MAX_RESPONSE_BYTES),
        ):
            _positive_int(value, name, maximum=maximum)
        if self.login_body_bytes > self.max_body_bytes or self.max_response_bytes <= 0:
            raise DashboardConfigurationError(
                "invalid dashboard configuration: body bounds"
            )
        _positive_int(self.login_limit, "login_limit", maximum=MAX_LOGIN_LIMIT)
        _positive_int(
            self.login_window_seconds,
            "login_window_seconds",
            maximum=MAX_LOGIN_WINDOW_SECONDS,
        )
        _positive_int(self.action_limit, "action_limit", maximum=MAX_ACTION_LIMIT)
        _positive_int(
            self.action_window_seconds,
            "action_window_seconds",
            maximum=MAX_ACTION_WINDOW_SECONDS,
        )
        _positive_int(self.read_limit, "read_limit", maximum=DEFAULT_READ_LIMIT)
        _positive_int(
            self.read_window_seconds,
            "read_window_seconds",
            maximum=DEFAULT_READ_WINDOW_SECONDS,
        )
        _positive_int(
            self.global_read_limit,
            "global_read_limit",
            maximum=DEFAULT_GLOBAL_READ_LIMIT,
        )
        _positive_int(
            self.global_read_window_seconds,
            "global_read_window_seconds",
            maximum=DEFAULT_GLOBAL_READ_WINDOW_SECONDS,
        )
        _positive_int(
            self.global_action_limit,
            "global_action_limit",
            maximum=MAX_ACTION_LIMIT * 6,
        )
        _positive_int(
            self.global_action_window_seconds,
            "global_action_window_seconds",
            maximum=MAX_ACTION_WINDOW_SECONDS,
        )
        if not isinstance(self.cookie_secure, (bool, type(None))):
            raise DashboardConfigurationError(
                "invalid dashboard configuration: cookie_secure"
            )
        for name, value, maximum in (
            ("session_idle_seconds", self.session_idle_seconds, 12 * 60 * 60),
            (
                "session_absolute_seconds",
                self.session_absolute_seconds,
                7 * 24 * 60 * 60,
            ),
            ("session_max", self.session_max, 1024),
        ):
            _positive_int(value, name, maximum=maximum)
        if self.session_absolute_seconds < self.session_idle_seconds:
            raise DashboardConfigurationError(
                "invalid dashboard configuration: session lifetimes"
            )
        if any(
            origin.lower().startswith("https://") for origin in self.allowed_origins
        ):
            if self.cookie_secure is False:
                raise DashboardConfigurationError(
                    "invalid dashboard configuration: cookie_secure"
                )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DashboardConfig":
        """Load only dedicated dashboard settings and mounted secret files."""

        values = os.environ if environ is None else environ

        def first(*names: str) -> str | None:
            for name in names:
                candidate = values.get(name)
                if candidate is not None and candidate.strip():
                    return candidate.strip()
            return None

        origins = first("MEDIA_DASHBOARD_ALLOWED_ORIGINS", "DASHBOARD_ALLOWED_ORIGINS")
        if origins is None:
            raise DashboardConfigurationError(
                "invalid dashboard configuration: allowed_origins"
            )
        password_hash_value = first(
            "MEDIA_DASHBOARD_PASSWORD_HASH", "DASHBOARD_PASSWORD_HASH"
        )
        if password_hash_value is None:
            password_path = first(
                "MEDIA_DASHBOARD_PASSWORD_HASH_FILE",
                "DASHBOARD_PASSWORD_HASH_FILE",
            )
            if password_path is None:
                raise DashboardConfigurationError(
                    "invalid dashboard configuration: password_hash"
                )
            password_path = _canonical_secret_path(password_path)
            try:
                password_bytes = _read_bounded_secret(password_path)
                if password_bytes.endswith(b"\n"):
                    password_bytes = password_bytes[:-1]
                    if password_bytes.endswith(b"\r"):
                        password_bytes = password_bytes[:-1]
                if b"\r" in password_bytes or b"\n" in password_bytes:
                    raise ValueError("password hash contains framing whitespace")
                password_hash_value = password_bytes.decode("utf-8", "strict")
            except (DashboardConfigurationError, UnicodeError, ValueError) as exc:
                raise DashboardConfigurationError(
                    "invalid dashboard configuration: password_hash"
                ) from exc
        key_path = first("MEDIA_DASHBOARD_API_KEY_FILE", "DASHBOARD_API_KEY_FILE")
        if key_path is None:
            # Do not accept a bearer key in an environment value.  Environment
            # snapshots and supervisor diagnostics commonly expose them.
            if first("MEDIA_DASHBOARD_API_KEY", "DASHBOARD_API_KEY") is not None:
                raise DashboardConfigurationError(
                    "invalid dashboard configuration: companion_api_key_file"
                )
            raise DashboardConfigurationError(
                "invalid dashboard configuration: companion_api_key"
            )
        key_path = _canonical_secret_path(key_path)
        key_bytes = load_api_key_file(key_path)
        host = first("MEDIA_DASHBOARD_HOST", "DASHBOARD_HOST") or DEFAULT_HOST
        port_text = first("MEDIA_DASHBOARD_PORT", "DASHBOARD_PORT")
        try:
            port = DEFAULT_PORT if port_text is None else int(port_text)
        except ValueError as exc:
            raise DashboardConfigurationError(
                "invalid dashboard configuration: port"
            ) from exc
        if port == 0:
            raise DashboardConfigurationError("invalid dashboard configuration: port")
        secure_text = first(
            "MEDIA_DASHBOARD_SECURE_COOKIES", "DASHBOARD_SECURE_COOKIES"
        )
        secure: bool | None = None
        if secure_text is not None:
            if secure_text.lower() not in {"0", "1", "false", "true", "no", "yes"}:
                raise DashboardConfigurationError(
                    "invalid dashboard configuration: cookie_secure"
                )
            secure = secure_text.lower() in {"1", "true", "yes"}
        return cls(
            password_hash=password_hash_value,
            allowed_origins=_split_origins(origins),
            companion_url=first(
                "MEDIA_DASHBOARD_COMPANION_URL", "DASHBOARD_COMPANION_URL"
            )
            or "http://media-companion:18080",
            companion_api_key=key_bytes,
            host=host,
            port=port,
            cookie_secure=secure,
        )


class SlidingWindowRateLimiter:
    """Thread-safe bounded sliding-window limiter for login/actions."""

    def __init__(
        self, *, max_buckets: int = 4096, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if (
            isinstance(max_buckets, bool)
            or not isinstance(max_buckets, int)
            or max_buckets <= 0
        ):
            raise ValueError("max_buckets must be positive")
        self._max_buckets = max_buckets
        self._clock = clock
        self._buckets: dict[tuple[str, str], deque[float]] = {}
        self._bucket_windows: dict[tuple[str, str], int] = {}
        self._lock = threading.RLock()

    def allow(
        self, category: str, key: str, *, limit: int, window_seconds: int
    ) -> bool:
        if (
            not isinstance(category, str)
            or not isinstance(key, str)
            or not category
            or not key
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or limit <= 0
            or window_seconds <= 0
        ):
            return False
        try:
            if len(category.encode("utf-8")) > 64 or len(key.encode("utf-8")) > 256:
                return False
        except UnicodeEncodeError:
            return False
        now = self._clock()
        bucket_key = (category, key)
        with self._lock:
            existing_bucket = self._buckets.get(bucket_key)
            if existing_bucket is not None:
                self._bucket_windows[bucket_key] = max(
                    self._bucket_windows.get(bucket_key, window_seconds),
                    window_seconds,
                )
            self._purge_locked(now)
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                if len(self._buckets) >= self._max_buckets:
                    # Evicting an active bucket lets an attacker rotate keys
                    # and bypass a per-peer limit.  Fail closed until purge
                    # frees a slot instead.
                    return False
                bucket = deque()
                self._buckets[bucket_key] = bucket
                self._bucket_windows[bucket_key] = window_seconds
            cutoff = now - window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True

    def allow_many(
        self,
        requests: Sequence[tuple[str, str, int, int]],
    ) -> bool:
        """Atomically reserve several scopes for one request.

        A denied global bucket must not consume a per-peer unit (or vice
        versa), which matters when dashboard traffic is bursty across many
        sessions.
        """

        if not requests:
            return False
        normalized: list[tuple[str, str, int, int]] = []
        for category, key, limit, window_seconds in requests:
            if (
                not isinstance(category, str)
                or not isinstance(key, str)
                or not category
                or not key
                or isinstance(limit, bool)
                or not isinstance(limit, int)
                or isinstance(window_seconds, bool)
                or not isinstance(window_seconds, int)
                or limit <= 0
                or window_seconds <= 0
            ):
                return False
            try:
                if len(category.encode("utf-8")) > 64 or len(key.encode("utf-8")) > 256:
                    return False
            except UnicodeEncodeError:
                return False
            normalized.append((category, key, limit, window_seconds))
        now = self._clock()
        with self._lock:
            grouped: dict[tuple[str, str], list[tuple[int, int]]] = {}
            for category, key, limit, window_seconds in normalized:
                grouped.setdefault((category, key), []).append((limit, window_seconds))
            for bucket_key, constraints in grouped.items():
                if bucket_key in self._buckets:
                    self._bucket_windows[bucket_key] = max(
                        self._bucket_windows.get(
                            bucket_key,
                            max(window for _limit, window in constraints),
                        ),
                        max(window for _limit, window in constraints),
                    )
            self._purge_locked(now)
            unique = set(grouped)
            missing = sum(1 for item in unique if item not in self._buckets)
            if len(self._buckets) + missing > self._max_buckets:
                return False
            buckets: list[tuple[tuple[str, str], deque[float], int]] = []
            for bucket_key, constraints in grouped.items():
                bucket = self._buckets.get(bucket_key)
                if bucket is None:
                    bucket = deque()
                reservations = len(constraints)
                for limit, window_seconds in constraints:
                    active = sum(
                        timestamp > now - window_seconds for timestamp in bucket
                    )
                    if active + reservations > limit:
                        return False
                buckets.append((bucket_key, bucket, reservations))
            for bucket_key, bucket, reservations in buckets:
                if bucket_key not in self._buckets:
                    self._buckets[bucket_key] = bucket
                    self._bucket_windows[bucket_key] = max(
                        window for _limit, window in grouped[bucket_key]
                    )
                bucket.extend([now] * reservations)
            return True

    def purge(self) -> int:
        now = self._clock()
        removed = 0
        with self._lock:
            removed = self._purge_locked(now)
        return removed

    def _purge_locked(self, now: float) -> int:
        removed = 0
        for key in tuple(self._buckets):
            bucket = self._buckets[key]
            retention = self._bucket_windows.get(
                key, max(MAX_LOGIN_WINDOW_SECONDS, MAX_ACTION_WINDOW_SECONDS)
            )
            while bucket and bucket[0] <= now - retention:
                bucket.popleft()
            if not bucket:
                del self._buckets[key]
                self._bucket_windows.pop(key, None)
                removed += 1
        return removed


class DashboardHTTPServer(http.server.ThreadingHTTPServer):
    """HTTP server carrying one immutable :class:`DashboardApp` instance."""

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], app: "DashboardApp") -> None:
        self.app = app
        super().__init__(address, DashboardRequestHandler)


class DashboardApp:
    """Dashboard application and its server factory."""

    def __init__(
        self,
        config: DashboardConfig,
        *,
        companion: DashboardCompanionClient | Any | None = None,
        session_store: SessionStore | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        self.config = config
        self.sessions = session_store or SessionStore(
            idle_seconds=config.session_idle_seconds,
            absolute_seconds=config.session_absolute_seconds,
            max_sessions=config.session_max,
        )
        if companion is None:
            if config.companion_api_key is None:
                raise DashboardConfigurationError(
                    "dashboard companion key is unavailable"
                )
            self.companion = DashboardCompanionClient(
                config.companion_url,
                config.companion_api_key,
            )
        else:
            self.companion = companion
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter()

    def make_server(
        self, *, host: str | None = None, port: int | None = None
    ) -> DashboardHTTPServer:
        bind_host = self.config.host if host is None else host
        bind_port = self.config.port if port is None else port
        if not isinstance(bind_host, str) or not bind_host:
            raise DashboardConfigurationError("invalid dashboard bind host")
        if (
            isinstance(bind_port, bool)
            or not isinstance(bind_port, int)
            or not 0 <= bind_port <= 65535
        ):
            raise DashboardConfigurationError("invalid dashboard bind port")
        return DashboardHTTPServer((bind_host, bind_port), self)

    def serve_forever(self) -> None:
        server = self.make_server()
        try:
            server.serve_forever()
        finally:
            server.server_close()

    run = serve_forever


def create_app(
    config: DashboardConfig,
    *,
    companion: DashboardCompanionClient | Any | None = None,
) -> DashboardApp:
    return DashboardApp(config, companion=companion)


class DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    """Security-conscious request router for :class:`DashboardApp`."""

    server_version = "MediaDashboard"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # Exact browser route mapping.  Unknown operations do not reach the
    # companion, even if a caller guesses its private endpoint shape.
    _READ_PATHS: Final[dict[str, str]] = {
        "/api/health": OP_HEALTH,
        "/api/users": OP_USERS,
        "/api/users/resolve": OP_USERS_RESOLVE,
        "/api/blocked": OP_BLOCKED,
        "/api/subscriptions": OP_SUBSCRIPTIONS,
        "/api/deliveries": OP_DELIVERIES,
        "/api/quarantine": OP_QUARANTINE,
        "/api/oracle": OP_ORACLE,
        "/api/audit": OP_AUDIT,
    }
    _MUTATION_PATHS: Final[dict[str, str]] = {
        "/api/users/add": OP_USERS_ADD,
        "/api/users/remove": OP_USERS_REMOVE,
        "/api/deliveries/retry-once": OP_RETRY_ONCE,
        "/api/deliveries/mark-abandoned": OP_MARK_ABANDONED,
        "/api/deliveries/assume-sent": OP_ASSUME_SENT,
        "/api/deliveries/resend-once": OP_RESEND_ONCE,
    }
    _HTML_READ_PATHS: Final[dict[str, str]] = {
        "/health": OP_HEALTH,
        "/users": OP_USERS,
        "/users/resolve": OP_USERS_RESOLVE,
        "/blocked": OP_BLOCKED,
        "/subscriptions": OP_SUBSCRIPTIONS,
        "/deliveries": OP_DELIVERIES,
        "/quarantine": OP_QUARANTINE,
        "/oracle": OP_ORACLE,
        "/audit": OP_AUDIT,
    }
    _HTML_MUTATION_PATHS: Final[dict[str, str]] = {
        "/users/add": OP_USERS_ADD,
        "/users/remove": OP_USERS_REMOVE,
        "/deliveries/retry-once": OP_RETRY_ONCE,
        "/deliveries/mark-abandoned": OP_MARK_ABANDONED,
        "/deliveries/assume-sent": OP_ASSUME_SENT,
        "/deliveries/resend-once": OP_RESEND_ONCE,
    }

    def setup(self) -> None:
        super().setup()
        # Bound header/body reads as well as downstream companion calls.  A
        # client that dribbles a bounded body forever cannot retain a worker.
        self.request.settimeout(15.0)

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, _format: str, *_args: object) -> None:
        # Request paths/headers may contain passwords, CSRF values, cursors, or
        # accidental secrets.  The dashboard intentionally emits no access log.
        return

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self._send_json(405, {"error": "request_failed"})

    @property
    def app(self) -> DashboardApp:
        return self.server.app  # type: ignore[attr-defined,return-value]

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlsplit(self.path)
            raw_path = parsed.path
            if (
                parsed.scheme
                or parsed.netloc
                or parsed.fragment
                or "\\" in raw_path
                or any(
                    token in raw_path.lower() for token in ("%2f", "%5c", "%2e", "%00")
                )
                or any(segment in {".", ".."} for segment in raw_path.split("/"))
            ):
                raise DashboardHTTPError(400)
            if _BAD_PERCENT_RE.search(raw_path):
                raise DashboardHTTPError(400)
            try:
                path = unquote_to_bytes(raw_path).decode("utf-8", "strict")
            except (UnicodeDecodeError, ValueError):
                raise DashboardHTTPError(400) from None
            if "\x00" in path or "\\" in path or len(path.encode("utf-8")) > 4096:
                raise DashboardHTTPError(400)
            if len(parsed.query.encode("utf-8")) > MAX_QUERY_VALUE_BYTES:
                raise DashboardHTTPError(400)
            if _BAD_PERCENT_RE.search(parsed.query):
                raise DashboardHTTPError(400)
            self._validate_request_framing(method)
            if path == "/healthz":
                if parsed.query:
                    raise DashboardHTTPError(400)
                self._healthz(method)
                return
            if path == "/readyz":
                if parsed.query:
                    raise DashboardHTTPError(400)
                self._validate_browser_boundary(require_origin=False)
                self._require_session()
                self._readyz(method)
                return
            login_post = path == "/login" and method == "POST"
            self._validate_browser_boundary(
                require_origin=method in {"POST", "PUT", "PATCH", "DELETE"},
                allow_referer_fallback=login_post,
                allow_missing_origin=login_post,
            )
            if path == "/login":
                if method == "GET" or method == "HEAD":
                    self._login_page()
                elif method == "POST":
                    self._login()
                else:
                    raise DashboardHTTPError(405)
                return
            if path == "/logout":
                if method != "POST":
                    raise DashboardHTTPError(405)
                self._logout()
                return
            if path == "/":
                if method not in {"GET", "HEAD"}:
                    raise DashboardHTTPError(405)
                self._dashboard_page()
                return
            html_operation = self._HTML_READ_PATHS.get(path)
            if html_operation is not None:
                if method not in {"GET", "HEAD"}:
                    raise DashboardHTTPError(405)
                self._read_operation(html_operation, parsed.query, html=True)
                return
            operation = self._operation_for_path(path)
            if operation is None:
                raise DashboardHTTPError(404)
            if operation in READ_OPERATIONS:
                if method not in {"GET", "HEAD"}:
                    raise DashboardHTTPError(405)
                self._read_operation(operation, parsed.query)
            elif operation in ALLOWED_OPERATIONS:
                if method != "POST":
                    raise DashboardHTTPError(405)
                self._mutation_operation(operation)
            else:
                raise DashboardHTTPError(404)
        except DashboardHTTPError as exc:
            self.close_connection = True
            self._send_failure(exc.status)
        except CompanionConfigurationError:
            self.close_connection = True
            self._send_failure(400)
        except CompanionClientError:
            self.close_connection = True
            self._send_failure(503)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            # No exception text reaches browser or logs.  A handler error is
            # intentionally indistinguishable from another temporary failure.
            self.close_connection = True
            self._send_failure(500)

    def _healthz(self, method: str) -> None:
        if method not in {"GET", "HEAD"}:
            raise DashboardHTTPError(405)
        self._send_json(200, {"status": "ok"})

    def _readyz(self, method: str) -> None:
        if method not in {"GET", "HEAD"}:
            raise DashboardHTTPError(405)
        # Readiness reports only a bounded boolean; it never relays companion
        # health data or accepts browser credentials.  /healthz remains the
        # process-only liveness endpoint.
        ready = False
        probe = getattr(self.app.companion, "ready", None)
        if callable(probe):
            try:
                ready = bool(probe())
            except Exception:
                ready = False
        self._send_json(
            200 if ready else 503,
            {"status": "ready" if ready else "not_ready", "ready": ready},
        )

    def _validate_request_framing(self, method: str) -> None:
        transfer_values = self.headers.get_all("Transfer-Encoding", [])
        if len(transfer_values) > 1:
            raise DashboardHTTPError(400)
        if transfer_values:
            raise DashboardHTTPError(400)
        length_values = self.headers.get_all("Content-Length", [])
        if len(length_values) > 1:
            raise DashboardHTTPError(400)
        if not length_values:
            if method not in {"GET", "HEAD"}:
                raise DashboardHTTPError(411)
            # There is no framing field with which to prove that a GET/HEAD
            # has no trailing bytes.  Close this request after the response so
            # an unsolicited body cannot become the next request line.
            self.close_connection = True
            return
        text = length_values[0]
        if not isinstance(text, str) or not _DECIMAL_RE.fullmatch(text):
            raise DashboardHTTPError(400)
        length = int(text)
        if length > self.app.config.max_body_bytes:
            raise DashboardHTTPError(413)
        if method in {"GET", "HEAD"} and length != 0:
            raise DashboardHTTPError(400)

    def _validate_browser_boundary(
        self,
        *,
        require_origin: bool,
        allow_referer_fallback: bool = False,
        allow_missing_origin: bool = False,
    ) -> None:
        host_values = self.headers.get_all("Host", [])
        if len(host_values) != 1:
            raise DashboardHTTPError(400)
        origin_values = self.headers.get_all("Origin", [])
        if len(origin_values) > 1:
            raise DashboardHTTPError(400)
        host = host_values[0]
        origin = origin_values[0] if origin_values else None
        if origin is None and require_origin and allow_referer_fallback:
            referer_values = self.headers.get_all("Referer", [])
            if len(referer_values) > 1:
                raise DashboardHTTPError(400)
            if referer_values:
                referer = urlsplit(referer_values[0])
                if (
                    referer.scheme.lower() not in {"http", "https"}
                    or not referer.netloc
                    or referer.username is not None
                    or referer.password is not None
                    or referer.fragment
                ):
                    raise DashboardHTTPError(400)
                origin = f"{referer.scheme.lower()}://{referer.netloc}"
        try:
            validate_request_origin(
                host=host,
                origin=origin,
                allowed_origins=self.app.config.allowed_origins,
                # Some privacy-focused browsers omit both Origin and Referer
                # on a same-origin password form POST. Login remains protected
                # by an exact Host allowlist, a strong password, and a strict
                # rate limit. Explicit Origin/Referer values are still checked;
                # only their total absence is tolerated on this one endpoint.
                require_origin=require_origin
                and not (allow_missing_origin and origin is None),
            )
        except (InvalidRequestOrigin, ValueError):
            raise DashboardHTTPError(400) from None

    def _login_page(self) -> None:
        self._send_html(200, login_view())

    def _login(self) -> None:
        client_key = self._client_key()
        if not self.app.rate_limiter.allow(
            "login",
            client_key,
            limit=self.app.config.login_limit,
            window_seconds=self.app.config.login_window_seconds,
        ):
            self.close_connection = True
            self._send_generic(429, retry_after=self.app.config.login_window_seconds)
            return
        body = self._read_body(
            max_bytes=self.app.config.login_body_bytes, required=True
        )
        password = self._login_password(body)
        if not verify_password(password, self.app.config.password_hash):
            self._send_generic(401)
            return
        old_token = self._cookie_value(SESSION_COOKIE)
        if old_token is not None:
            self.app.sessions.revoke(old_token)
        secrets, session = self.app.sessions.create(actor="dashboard-admin")
        secure = self._secure_cookies()
        cookies = (
            self._cookie(SESSION_COOKIE, secrets.token, http_only=True, secure=secure),
            self._cookie(
                CSRF_COOKIE, secrets.csrf_token, http_only=False, secure=secure
            ),
        )
        # The CSRF value is safe to expose to the browser because it is not a
        # bearer credential; keeping it in a cookie also supports no-script
        # forms and clients that use the header.
        if self._accepts_html():
            self._send_redirect("/", cookies=cookies)
        else:
            self._send_json(
                200,
                {
                    "ok": True,
                    "csrf_token": secrets.csrf_token,
                    "expires_at": int(session.absolute_expires_at),
                },
                cookies=cookies,
            )

    def _logout(self) -> None:
        session, token = self._require_session()
        body = self._read_body(
            max_bytes=self.app.config.login_body_bytes, required=False
        )
        body_csrf = self._logout_csrf(body) if body else None
        csrf = self._request_csrf_token(body_csrf)
        self._validate_session_csrf(token, csrf)
        del session
        if token is not None:
            self.app.sessions.revoke(token)
        cookies = (
            self._cookie(
                SESSION_COOKIE,
                "",
                http_only=True,
                secure=self._secure_cookies(),
                max_age=0,
            ),
            self._cookie(
                CSRF_COOKIE,
                "",
                http_only=False,
                secure=self._secure_cookies(),
                max_age=0,
            ),
        )
        if self._accepts_html():
            self._send_redirect("/login", cookies=cookies)
        else:
            self._send_json(200, {"ok": True}, cookies=cookies)

    def _dashboard_page(self) -> None:
        try:
            session, _token = self._require_session()
        except DashboardHTTPError as exc:
            if exc.status == 401 and self._accepts_html():
                self._send_redirect("/login")
                return
            raise
        self._send_html(
            200,
            dashboard_view(
                actor=session.actor,
                csrf_token=self._cookie_value(CSRF_COOKIE),
            ),
        )

    def _read_operation(
        self, operation: str, query: str, *, html: bool = False
    ) -> None:
        session, _token = self._require_session()
        client_key = self._client_key()
        if not self.app.rate_limiter.allow_many(
            (
                (
                    "read",
                    client_key,
                    self.app.config.read_limit,
                    self.app.config.read_window_seconds,
                ),
                (
                    "read-global",
                    "all",
                    self.app.config.global_read_limit,
                    self.app.config.global_read_window_seconds,
                ),
            )
        ):
            self.close_connection = True
            self._send_generic(429, retry_after=self.app.config.read_window_seconds)
            return
        payload = self._query_payload(operation, query)
        response = self._call_read(operation, payload, session=session)
        status, result = self._response_payload(operation, response)
        result_data = result.get("data")
        if not isinstance(result_data, Mapping):
            raise CompanionClientError("companion response data is invalid")
        if html:
            self._send_html(
                status,
                operation_result_view(
                    operation,
                    result_data,
                    actor=session.actor,
                    csrf_token=self._cookie_value(CSRF_COOKIE),
                ),
            )
        else:
            self._send_json(status, result)

    def _mutation_operation(self, operation: str) -> None:
        session, session_token = self._require_session()
        key = self._client_key()
        if not self.app.rate_limiter.allow_many(
            (
                (
                    "action",
                    key,
                    self.app.config.action_limit,
                    self.app.config.action_window_seconds,
                ),
                (
                    "action-global",
                    "all",
                    self.app.config.global_action_limit,
                    self.app.config.global_action_window_seconds,
                ),
            )
        ):
            self.close_connection = True
            self._send_generic(429, retry_after=self.app.config.action_window_seconds)
            return
        body = self._read_body(max_bytes=self.app.config.max_body_bytes, required=True)
        payload = self._mutation_payload(operation, body, session_token=session_token)
        response = self._call_mutation(operation, payload, session=session)
        status, result = self._response_payload(operation, response)
        result_data = result.get("data")
        if not isinstance(result_data, Mapping):
            raise CompanionClientError("companion response data is invalid")
        if self._accepts_html():
            self._send_html(
                status,
                operation_result_view(
                    operation,
                    result_data,
                    actor=session.actor,
                    csrf_token=self._cookie_value(CSRF_COOKIE),
                    action=self.path.split("?", 1)[0],
                    submitted=payload,
                ),
            )
        else:
            self._send_json(status, result)

    def _operation_for_path(self, path: str) -> str | None:
        if path in self._READ_PATHS:
            return self._READ_PATHS[path]
        if path in self._MUTATION_PATHS:
            return self._MUTATION_PATHS[path]
        if path in self._HTML_MUTATION_PATHS:
            return self._HTML_MUTATION_PATHS[path]
        return None

    def _call_read(
        self, operation: str, payload: Mapping[str, object], *, session: Any
    ) -> Any:
        if operation == OP_HEALTH:
            return self._companion_operation(
                operation, "health", payload, session=session
            )
        if operation == OP_USERS:
            return self._companion_operation(
                operation, "users", payload, session=session
            )
        if operation == OP_USERS_RESOLVE:
            return self._companion_operation(
                operation, "resolve_user", payload, session=session
            )
        if operation == OP_BLOCKED:
            return self._companion_operation(
                operation, "blocked", payload, session=session
            )
        if operation == OP_SUBSCRIPTIONS:
            return self._companion_operation(
                operation, "subscriptions", payload, session=session
            )
        if operation == OP_DELIVERIES:
            return self._companion_operation(
                operation, "deliveries", payload, session=session
            )
        if operation == OP_QUARANTINE:
            return self._companion_operation(
                operation, "quarantine", payload, session=session
            )
        if operation == OP_ORACLE:
            return self._companion_operation(
                operation, "oracle", payload, session=session
            )
        if operation == OP_AUDIT:
            return self._companion_operation(
                operation, "audit", payload, session=session
            )
        raise DashboardHTTPError(404)

    def _call_mutation(
        self, operation: str, payload: Mapping[str, object], *, session: Any
    ) -> Any:
        if operation == OP_USERS_ADD:
            return self._companion_operation(
                operation, "add_user", payload, session=session
            )
        if operation == OP_USERS_REMOVE:
            return self._companion_operation(
                operation, "remove_user", payload, session=session
            )
        if operation == OP_RETRY_ONCE:
            return self._companion_operation(
                operation, "retry_once", payload, session=session
            )
        if operation == OP_MARK_ABANDONED:
            return self._companion_operation(
                operation, "mark_abandoned", payload, session=session
            )
        if operation == OP_ASSUME_SENT:
            return self._companion_operation(
                operation, "assume_sent", payload, session=session
            )
        if operation == OP_RESEND_ONCE:
            return self._companion_operation(
                operation, "resend_once", payload, session=session
            )
        raise DashboardHTTPError(404)

    def _companion_operation(
        self,
        operation: str,
        method_name: str,
        payload: Mapping[str, object],
        *,
        session: Any,
    ) -> Any:
        if isinstance(self.app.companion, DashboardCompanionClient):
            return self.app.companion.call(
                operation,
                payload,
                session_actor=session.actor,
                session_digest=session.session_digest,
                audit_context=f"dashboard:{session.session_digest}:{operation}",
            )
        method = getattr(self.app.companion, method_name, None)
        if callable(method):
            try:
                parameters = inspect.signature(method).parameters
            except (TypeError, ValueError):
                return method(**dict(payload))
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            metadata = {
                "session_actor": session.actor,
                "session_digest": session.session_digest,
                "audit_context": f"dashboard:{session.session_digest}:{operation}",
            }
            method_payload = dict(payload)
            if accepts_kwargs:
                method_payload.update(metadata)
            else:
                method_payload.update(
                    {key: value for key, value in metadata.items() if key in parameters}
                )
            return method(**method_payload)
        call = getattr(self.app.companion, "call", None)
        if callable(call):
            try:
                parameters = inspect.signature(call).parameters
            except (TypeError, ValueError):
                return call(operation, payload)
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            metadata = {
                "session_actor": session.actor,
                "session_digest": session.session_digest,
                "audit_context": f"dashboard:{session.session_digest}:{operation}",
            }
            if accepts_kwargs:
                return call(operation, payload, **metadata)
            supported = {
                key: value for key, value in metadata.items() if key in parameters
            }
            return call(operation, payload, **supported)
        raise DashboardHTTPError(404)

    def _query_payload(self, operation: str, query: str) -> dict[str, object]:
        if len(query.encode("utf-8")) > MAX_QUERY_VALUE_BYTES:
            raise DashboardHTTPError(400)
        try:
            values = parse_qs(
                query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=8,
            )
        except ValueError:
            raise DashboardHTTPError(400) from None
        if operation == OP_USERS_RESOLVE:
            if set(values) - {"user_id", "fingerprint", "version"}:
                raise DashboardHTTPError(400)
            if "user_id" not in values or any(
                len(items) != 1 for items in values.values()
            ):
                raise DashboardHTTPError(400)
            raw_user_id = values["user_id"][0]
            if not _DECIMAL_RE.fullmatch(raw_user_id):
                raise DashboardHTTPError(400)
            user_id = int(raw_user_id)
            if not 0 < user_id <= (1 << 53) - 1:
                raise DashboardHTTPError(400)
            resolve_result: dict[str, object] = {"user_id": user_id}
            if "fingerprint" in values:
                resolve_result["fingerprint"] = self._bounded_field(
                    values["fingerprint"][0], "fingerprint", 256
                )
            if "version" in values:
                version = values["version"][0]
                if not _DECIMAL_RE.fullmatch(version):
                    raise DashboardHTTPError(400)
                parsed_version = int(version)
                if parsed_version > (1 << 53) - 1:
                    raise DashboardHTTPError(400)
                resolve_result["version"] = parsed_version
            return resolve_result
        allowed = {"limit", "cursor"}
        if operation == OP_DELIVERIES:
            allowed.add("status")
        if any(key not in allowed for key in values):
            raise DashboardHTTPError(400)
        if any(len(items) != 1 for items in values.values()):
            raise DashboardHTTPError(400)
        result: dict[str, object] = {}
        if "limit" in values:
            raw = values["limit"][0]
            try:
                limit = int(raw)
            except ValueError:
                raise DashboardHTTPError(400) from None
            if not 1 <= limit <= MAX_PAGE_LIMIT:
                raise DashboardHTTPError(400)
            result["limit"] = limit
        if "cursor" in values:
            cursor = values["cursor"][0]
            if not cursor or len(cursor.encode("utf-8")) > MAX_QUERY_VALUE_BYTES:
                raise DashboardHTTPError(400)
            result["cursor"] = cursor
        if "status" in values:
            status = values["status"][0]
            if (
                not status
                or len(status) > 64
                or any(ord(char) < 0x20 or char.isspace() for char in status)
            ):
                raise DashboardHTTPError(400)
            result["status"] = status
        return result

    def _mutation_payload(
        self,
        operation: str,
        body: bytes,
        *,
        session_token: str | None,
    ) -> dict[str, object]:
        content_type_values = self.headers.get_all("Content-Type", [])
        if len(content_type_values) != 1:
            raise DashboardHTTPError(415)
        content_type = content_type_values[0].split(";", 1)[0].strip().lower()
        if content_type not in {
            "application/json",
            "application/x-www-form-urlencoded",
        }:
            raise DashboardHTTPError(415)
        if content_type == "application/json":
            try:
                decoded = json.loads(
                    body.decode("utf-8"), object_pairs_hook=_json_object_pairs
                )
            except (UnicodeDecodeError, ValueError):
                raise DashboardHTTPError(400) from None
        else:
            try:
                fields = parse_qs(
                    body.decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=32,
                )
            except (UnicodeDecodeError, ValueError):
                raise DashboardHTTPError(400) from None
            if any(len(items) != 1 for items in fields.values()):
                raise DashboardHTTPError(400)
            decoded = {key: items[0] for key, items in fields.items()}
        if not isinstance(decoded, dict):
            raise DashboardHTTPError(400)
        allowed = {"csrf_token"}
        if operation in {OP_USERS_ADD, OP_USERS_REMOVE}:
            allowed |= {
                "user_id",
                "fingerprint",
                "idempotency_key",
                "version",
                "state_fingerprint",
                "preview_digest",
                "confirmation",
            }
        elif operation in RECOVERY_OPERATIONS:
            allowed |= {
                "delivery_id",
                "idempotency_key",
                "preview_digest",
                "confirmation",
            }
        else:
            raise DashboardHTTPError(404)
        if any(not isinstance(key, str) or key not in allowed for key in decoded):
            raise DashboardHTTPError(400)
        # Header CSRF is preferred; a body token is accepted only as a no-script
        # fallback and is removed before any signed companion request.
        body_csrf_present = "csrf_token" in decoded
        body_csrf = decoded.pop("csrf_token", None)
        if body_csrf_present and body_csrf is None:
            raise DashboardHTTPError(403)
        csrf = self._request_csrf_token(body_csrf)
        self._validate_session_csrf(session_token, csrf)
        if operation in {OP_USERS_ADD, OP_USERS_REMOVE}:
            required = {"user_id", "fingerprint", "idempotency_key", "version"}
            if (
                not required.issubset(decoded)
                or ("confirmation" in decoded and "preview_digest" not in decoded)
                or ("preview_digest" in decoded and "confirmation" not in decoded)
            ):
                raise DashboardHTTPError(400)
            user_id = decoded["user_id"]
            if isinstance(user_id, str):
                if not _DECIMAL_RE.fullmatch(user_id):
                    raise DashboardHTTPError(400)
                user_id = int(user_id)
            if (
                isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or not 0 < user_id <= (1 << 53) - 1
            ):
                raise DashboardHTTPError(400)
            result: dict[str, object] = {
                "user_id": user_id,
                "fingerprint": self._bounded_field(
                    decoded["fingerprint"], "fingerprint", 256
                ),
                "idempotency_key": self._idempotency_key(decoded["idempotency_key"]),
            }
            if "version" in decoded:
                version = decoded["version"]
                if isinstance(version, str):
                    if not _DECIMAL_RE.fullmatch(version):
                        raise DashboardHTTPError(400)
                    version = int(version)
                if (
                    isinstance(version, bool)
                    or not isinstance(version, int)
                    or not 0 <= version <= (1 << 53) - 1
                ):
                    raise DashboardHTTPError(400)
                result["version"] = version
            for name, maximum in (("state_fingerprint", 256), ("preview_digest", 64)):
                if name in decoded:
                    result[name] = self._bounded_field(decoded[name], name, maximum)
            if "preview_digest" in result and not re.fullmatch(
                r"[0-9a-f]{64}", str(result["preview_digest"])
            ):
                raise DashboardHTTPError(400)
            if "confirmation" in decoded:
                result["confirmation"] = self._confirmation(decoded["confirmation"])
            return result
        required = {"delivery_id", "idempotency_key"}
        if (
            not required.issubset(decoded)
            or ("confirmation" in decoded and "preview_digest" not in decoded)
            or ("preview_digest" in decoded and "confirmation" not in decoded)
        ):
            raise DashboardHTTPError(400)
        delivery_id = decoded["delivery_id"]
        if isinstance(delivery_id, str):
            if not _DECIMAL_RE.fullmatch(delivery_id):
                raise DashboardHTTPError(400)
            delivery_id = int(delivery_id)
        if (
            isinstance(delivery_id, bool)
            or not isinstance(delivery_id, int)
            or not 0 < delivery_id <= (1 << 53) - 1
        ):
            raise DashboardHTTPError(400)
        result = {
            "delivery_id": delivery_id,
            "idempotency_key": self._idempotency_key(decoded["idempotency_key"]),
        }
        if "preview_digest" in decoded:
            preview_digest = self._bounded_field(
                decoded["preview_digest"], "preview_digest", 64
            )
            if not re.fullmatch(r"[0-9a-f]{64}", preview_digest):
                raise DashboardHTTPError(400)
            result["preview_digest"] = preview_digest
        if "confirmation" in decoded:
            result["confirmation"] = self._confirmation(decoded["confirmation"])
        return result

    @staticmethod
    def _bounded_field(value: object, _name: str, maximum: int) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > maximum
        ):
            raise DashboardHTTPError(400)
        if any(ord(char) < 0x20 or char.isspace() for char in value):
            raise DashboardHTTPError(400)
        return value

    @staticmethod
    def _idempotency_key(value: object) -> str:
        if not isinstance(value, str) or not _IDEMPOTENCY_RE.fullmatch(value):
            raise DashboardHTTPError(400)
        return value

    @classmethod
    def _confirmation(cls, value: object) -> str:
        confirmation = cls._bounded_field(value, "confirmation", 256)
        if len(confirmation) < 16 or not re.fullmatch(
            r"[A-Za-z0-9_-]{16,256}", confirmation
        ):
            raise DashboardHTTPError(400)
        return confirmation

    def _response_payload(
        self, operation: str, response: Any
    ) -> tuple[int, dict[str, JsonValue]]:
        status_code = 200
        if isinstance(response, CompanionResponse):
            data: object = response.data
            status_code = response.status
        elif isinstance(response, Mapping):
            data = response
        else:
            raise CompanionClientError("companion response is invalid")
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            raise CompanionClientError("companion response status is invalid")
        try:
            safe = _sanitize_operation_response(operation, data)
            if (
                isinstance(safe.get("data"), Mapping)
                and safe.get("operation") == operation
            ):
                typed = safe["data"]
            else:
                typed = safe
            encoded = json.dumps(
                typed, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise CompanionClientError("companion response is invalid") from exc
        if len(encoded) > self.app.config.max_response_bytes:
            raise CompanionClientError("companion response is too large")
        if not isinstance(typed, dict):
            raise CompanionClientError("companion response is invalid")
        if operation == OP_HEALTH and (
            typed.get("healthy") is False or typed.get("ready") is False
        ):
            status_code = 503
        return status_code, {"operation": operation, "data": typed}

    def _logout_csrf(self, body: bytes) -> str | None:
        content_type_values = self.headers.get_all("Content-Type", [])
        if len(content_type_values) != 1:
            raise DashboardHTTPError(415)
        content_type = content_type_values[0].split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise DashboardHTTPError(415)
        try:
            fields = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=4,
            )
        except (UnicodeDecodeError, ValueError):
            raise DashboardHTTPError(400) from None
        if set(fields) != {"csrf_token"} or len(fields["csrf_token"]) != 1:
            raise DashboardHTTPError(403)
        return fields["csrf_token"][0]

    def _header_csrf(self) -> str | None:
        values = self.headers.get_all("X-CSRF-Token", [])
        if len(values) > 1:
            raise DashboardHTTPError(403)
        if not values:
            return None
        token = values[0]
        if (
            not token
            or len(token.encode("utf-8", "ignore")) > 512
            or any(ord(char) < 0x20 or char.isspace() for char in token)
        ):
            raise DashboardHTTPError(403)
        return token

    def _request_csrf_token(self, body_token: object | None = None) -> str:
        header_token = self._header_csrf()
        if body_token is not None and (
            not isinstance(body_token, str)
            or not body_token
            or len(body_token.encode("utf-8")) > 512
            or any(ord(char) < 0x20 or char.isspace() for char in body_token)
        ):
            raise DashboardHTTPError(403)
        if header_token is not None and body_token is not None:
            if header_token != body_token:
                raise DashboardHTTPError(403)
        token = header_token if header_token is not None else body_token
        cookie_token = self._cookie_value(CSRF_COOKIE)
        if not isinstance(token, str) or cookie_token is None or token != cookie_token:
            raise DashboardHTTPError(403)
        return token

    def _validate_session_csrf(self, session_token: str | None, csrf: str) -> None:
        if session_token is None:
            raise DashboardHTTPError(403)
        if (
            self.app.sessions.validate(
                session_token,
                csrf_token=csrf,
                require_csrf=True,
                touch=False,
            )
            is None
        ):
            raise DashboardHTTPError(403)

    def _require_session(self, *, require_csrf: bool = False) -> tuple[Any, str | None]:
        token = self._cookie_value(SESSION_COOKIE)
        if token is None:
            raise DashboardHTTPError(401)
        csrf: str | None = None
        if require_csrf:
            csrf = self._request_csrf_token()
        session = self.app.sessions.validate(
            token, csrf_token=csrf, require_csrf=require_csrf
        )
        if session is None:
            raise DashboardHTTPError(401 if not require_csrf else 403)
        return session, token

    def _cookie_value(self, name: str) -> str | None:
        header_values = self.headers.get_all("Cookie", [])
        if len(header_values) > 1:
            return None
        header = header_values[0] if header_values else ""
        if len(header.encode("utf-8", "ignore")) > 8192:
            return None
        parsed = http.cookies.SimpleCookie()
        try:
            parsed.load(header)
        except (http.cookies.CookieError, ValueError):
            return None
        morsel = parsed.get(name)
        if (
            morsel is None
            or not morsel.value
            or any(ord(char) < 0x20 for char in morsel.value)
        ):
            return None
        # Duplicate cookie names are ambiguous; SimpleCookie retains the last,
        # so reject a second occurrence explicitly.
        if header.count(f"{name}=") != 1:
            return None
        return morsel.value

    def _login_password(self, body: bytes) -> str:
        content_type_values = self.headers.get_all("Content-Type", [])
        if len(content_type_values) != 1:
            raise DashboardHTTPError(415)
        content_type = content_type_values[0].split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            try:
                decoded = json.loads(
                    body.decode("utf-8"), object_pairs_hook=_json_object_pairs
                )
            except (UnicodeDecodeError, ValueError):
                raise DashboardHTTPError(400) from None
            if not isinstance(decoded, dict) or set(decoded) != {"password"}:
                raise DashboardHTTPError(400)
            password = decoded.get("password")
        elif content_type == "application/x-www-form-urlencoded":
            try:
                fields = parse_qs(
                    body.decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=4,
                )
            except (UnicodeDecodeError, ValueError):
                raise DashboardHTTPError(400) from None
            if set(fields) != {"password"} or len(fields["password"]) != 1:
                raise DashboardHTTPError(400)
            password = fields["password"][0]
        else:
            raise DashboardHTTPError(415)
        if (
            not isinstance(password, str)
            or not password
            or len(password.encode("utf-8")) > 1024
        ):
            raise DashboardHTTPError(401)
        return password

    def _read_body(self, *, max_bytes: int, required: bool) -> bytes:
        transfer_values = self.headers.get_all("Transfer-Encoding", [])
        if len(transfer_values) > 1:
            raise DashboardHTTPError(400)
        if transfer_values:
            raise DashboardHTTPError(400)
        length_values = self.headers.get_all("Content-Length", [])
        if len(length_values) > 1:
            raise DashboardHTTPError(400)
        length_text = length_values[0] if length_values else None
        if length_text is None:
            if required:
                raise DashboardHTTPError(411)
            return b""
        try:
            if not _DECIMAL_RE.fullmatch(length_text):
                raise ValueError
            length = int(length_text)
        except (TypeError, ValueError):
            raise DashboardHTTPError(400) from None
        if length < 0 or length > max_bytes:
            raise DashboardHTTPError(413)
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            raise DashboardHTTPError(408) from None
        if len(body) != length:
            raise DashboardHTTPError(400)
        return body

    def _client_key(self) -> str:
        # Do not trust X-Forwarded-For or any model-supplied header.  The
        # directly connected peer is the only bounded rate-limit identity.
        address = self.client_address[0] if self.client_address else "unknown"
        if not isinstance(address, str) or not address or len(address) > 128:
            return "unknown"
        return address

    def _secure_cookies(self) -> bool:
        configured_https = any(
            origin.lower().startswith("https://")
            for origin in self.app.config.allowed_origins
        )
        if configured_https:
            return True
        return bool(self.app.config.cookie_secure)

    def _accepts_html(self) -> bool:
        values = self.headers.get_all("Accept", [])
        if len(values) > 1:
            return False
        accept = values[0].lower() if values else ""
        return "text/html" in accept and "application/json" not in accept

    def _cookie(
        self,
        name: str,
        value: str,
        *,
        http_only: bool,
        secure: bool,
        max_age: int | None = None,
    ) -> str:
        if any(char in name + value for char in "\r\n;,"):
            raise DashboardHTTPError(500)
        parts = [f"{name}={value}", "Path=/", "SameSite=Strict"]
        if http_only:
            parts.append("HttpOnly")
        if secure:
            parts.append("Secure")
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        return "; ".join(parts)

    def _headers(self, *, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'none'; script-src 'none'; "
            "img-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        if self._secure_cookies():
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def _send_json(
        self,
        status: int,
        data: Mapping[str, object],
        *,
        cookies: Sequence[str] = (),
        retry_after: int | None = None,
    ) -> None:
        try:
            body = json.dumps(
                data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            self._send_generic(500)
            return
        if len(body) > self.app.config.max_response_bytes:
            self._send_generic(500)
            return
        self.send_response(status)
        self._headers(content_type="application/json; charset=utf-8", length=len(body))
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        if retry_after is not None:
            self.send_header("Retry-After", str(max(1, min(retry_after, 3600))))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self, status: int, body_text: str) -> None:
        body = body_text.encode("utf-8")
        if len(body) > self.app.config.max_response_bytes:
            self._send_generic(500)
            return
        self.send_response(status)
        self._headers(content_type="text/html; charset=utf-8", length=len(body))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_redirect(self, location: str, *, cookies: Sequence[str] = ()) -> None:
        if not location.startswith("/") or any(char in location for char in "\r\n"):
            self._send_generic(500)
            return
        self.send_response(303)
        self._headers(content_type="text/plain; charset=utf-8", length=0)
        self.send_header("Location", location)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _send_generic(self, status: int, *, retry_after: int | None = None) -> None:
        self._send_json(status, {"error": "request_failed"}, retry_after=retry_after)

    def _send_failure(self, status: int) -> None:
        if self._accepts_html():
            self._send_html(status, error_page())
        else:
            self._send_generic(status)


# Compatibility aliases used by simple process supervisors and tests.
DashboardServer = DashboardHTTPServer
RequestHandler = DashboardRequestHandler
Application = DashboardApp


def default_password_hash(password: str) -> str:
    """Small explicit helper for local setup/tests; never called at startup."""

    return hash_password(password)


__all__ = [
    "ALLOWED_OPERATIONS",
    "Application",
    "CSRF_COOKIE",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "PORT",
    "DashboardApp",
    "DashboardConfig",
    "DashboardConfigurationError",
    "DashboardHTTPServer",
    "DashboardRequestHandler",
    "DashboardServer",
    "RequestHandler",
    "SESSION_COOKIE",
    "SlidingWindowRateLimiter",
    "create_app",
    "default_password_hash",
]
