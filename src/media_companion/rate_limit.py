"""Small, dependency-free rate limiting primitives for companion tools.

The limits in this module are part of the public authorization contract.  A
deployment may make a limit *stricter*, but it cannot make one more permissive
through configuration.  The default implementation is deliberately local and
thread safe; a production multi-process deployment can provide the same
``RateLimiter`` interface backed by a durable atomic counter.

There are three classes of traffic:

``shared_read``
    The seven shared search/status/library tools.
``safe_request``
    The bounded ``request_movie`` and ``request_series`` mutations.
``admin_preview`` and ``admin_execution``
    Privileged mutation previews and their confirmed executions.  These are
    global ceilings, including dashboard recovery actions.

The implementation uses a sliding window rather than a fixed wall-clock
bucket.  This prevents callers from doubling a budget at a minute boundary and
keeps the documented "bursts may consume only the same budget" property.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from .errors import AuthorizationError

# Frozen contract ceilings.  Keep these values literal and easy to audit.
SHARED_READ_USER_LIMIT: Final[int] = 30
SHARED_READ_CHAT_LIMIT: Final[int] = 60
SHARED_READ_GLOBAL_LIMIT: Final[int] = 240
SHARED_READ_WINDOW_SECONDS: Final[int] = 60

SAFE_REQUEST_USER_LIMIT: Final[int] = 5
SAFE_REQUEST_CHAT_LIMIT: Final[int] = 15
SAFE_REQUEST_GLOBAL_LIMIT: Final[int] = 30
SAFE_REQUEST_WINDOW_SECONDS: Final[int] = 10 * 60

ADMIN_PREVIEW_LIMIT: Final[int] = 20
ADMIN_PREVIEW_WINDOW_SECONDS: Final[int] = 60
ADMIN_EXECUTION_LIMIT: Final[int] = 5
ADMIN_EXECUTION_WINDOW_SECONDS: Final[int] = 10 * 60
MAX_RATE_BUCKETS: Final[int] = 4096

# More descriptive aliases are useful to adapters and make the policy easy to
# discover without changing the one canonical set of values.
SHARED_READS_PER_USER: Final[int] = SHARED_READ_USER_LIMIT
SHARED_READS_PER_CHAT: Final[int] = SHARED_READ_CHAT_LIMIT
SHARED_READS_PER_GLOBAL: Final[int] = SHARED_READ_GLOBAL_LIMIT
SAFE_REQUESTS_PER_USER: Final[int] = SAFE_REQUEST_USER_LIMIT
SAFE_REQUESTS_PER_CHAT: Final[int] = SAFE_REQUEST_CHAT_LIMIT
SAFE_REQUESTS_PER_GLOBAL: Final[int] = SAFE_REQUEST_GLOBAL_LIMIT
ADMIN_PREVIEWS_PER_MINUTE: Final[int] = ADMIN_PREVIEW_LIMIT
ADMIN_EXECUTIONS_PER_TEN_MINUTES: Final[int] = ADMIN_EXECUTION_LIMIT
SHARED_READ_USER_PER_MINUTE: Final[int] = SHARED_READ_USER_LIMIT
SHARED_READ_CHAT_PER_MINUTE: Final[int] = SHARED_READ_CHAT_LIMIT
SHARED_READ_GLOBAL_PER_MINUTE: Final[int] = SHARED_READ_GLOBAL_LIMIT
SAFE_REQUEST_USER_PER_TEN_MINUTES: Final[int] = SAFE_REQUEST_USER_LIMIT
SAFE_REQUEST_CHAT_PER_TEN_MINUTES: Final[int] = SAFE_REQUEST_CHAT_LIMIT
SAFE_REQUEST_GLOBAL_PER_TEN_MINUTES: Final[int] = SAFE_REQUEST_GLOBAL_LIMIT
ADMIN_PREVIEW_PER_MINUTE: Final[int] = ADMIN_PREVIEW_LIMIT
ADMIN_EXECUTION_PER_TEN_MINUTES: Final[int] = ADMIN_EXECUTION_LIMIT


class RateOperation(str, Enum):
    """Canonical operation classes understood by :class:`RateLimiter`."""

    SHARED_READ = "shared_read"
    SAFE_REQUEST = "safe_request"
    ADMIN_PREVIEW = "admin_preview"
    ADMIN_EXECUTION = "admin_execution"


class RateLimitExceeded(AuthorizationError):
    """A frozen rate budget would be exceeded by the attempted operation."""

    def __init__(
        self,
        message: str = "rate limit exceeded",
        *,
        operation: str | RateOperation | None = None,
        scope: str | None = None,
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.operation = (
            None
            if operation is None
            else operation.value
            if isinstance(operation, RateOperation)
            else str(operation)
        )
        self.scope = scope
        self.retry_after = max(0.0, float(retry_after))


# Common spellings used by callers that want an explicit generic error name.
RateLimitError = RateLimitExceeded
TooManyRequests = RateLimitExceeded


@dataclass(frozen=True, slots=True)
class RateRule:
    """One scoped budget in a rate policy."""

    limit: int
    window_seconds: int
    scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or self.limit <= 0
        ):
            raise ValueError("rate limit must be a positive integer")
        if (
            isinstance(self.window_seconds, bool)
            or not isinstance(self.window_seconds, int)
            or self.window_seconds <= 0
        ):
            raise ValueError("rate window must be a positive integer")
        if not self.scopes or any(
            scope not in {"user", "chat", "global"} for scope in self.scopes
        ):
            raise ValueError("rate rule scopes are invalid")


def _bounded_int(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if value > maximum:
        raise ValueError(f"{name} cannot exceed the frozen authorization ceiling")
    return value


def _bounded_window(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    # A longer window weakens a per-window budget.  It is therefore forbidden
    # even when the count itself remains unchanged.
    if value > maximum:
        raise ValueError(f"{name} cannot exceed the frozen authorization window")
    return value


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """Rate ceilings with optional stricter deployment values.

    All fields default to the reviewed contract.  Values can only be lowered;
    this is checked at construction time so an environment/configuration typo
    cannot silently weaken abuse controls.
    """

    shared_read_user_limit: int = SHARED_READ_USER_LIMIT
    shared_read_chat_limit: int = SHARED_READ_CHAT_LIMIT
    shared_read_global_limit: int = SHARED_READ_GLOBAL_LIMIT
    shared_read_window_seconds: int = SHARED_READ_WINDOW_SECONDS
    safe_request_user_limit: int = SAFE_REQUEST_USER_LIMIT
    safe_request_chat_limit: int = SAFE_REQUEST_CHAT_LIMIT
    safe_request_global_limit: int = SAFE_REQUEST_GLOBAL_LIMIT
    safe_request_window_seconds: int = SAFE_REQUEST_WINDOW_SECONDS
    admin_preview_limit: int = ADMIN_PREVIEW_LIMIT
    admin_preview_window_seconds: int = ADMIN_PREVIEW_WINDOW_SECONDS
    admin_execution_limit: int = ADMIN_EXECUTION_LIMIT
    admin_execution_window_seconds: int = ADMIN_EXECUTION_WINDOW_SECONDS

    def __post_init__(self) -> None:
        for name, maximum in (
            ("shared_read_user_limit", SHARED_READ_USER_LIMIT),
            ("shared_read_chat_limit", SHARED_READ_CHAT_LIMIT),
            ("shared_read_global_limit", SHARED_READ_GLOBAL_LIMIT),
            ("safe_request_user_limit", SAFE_REQUEST_USER_LIMIT),
            ("safe_request_chat_limit", SAFE_REQUEST_CHAT_LIMIT),
            ("safe_request_global_limit", SAFE_REQUEST_GLOBAL_LIMIT),
            ("admin_preview_limit", ADMIN_PREVIEW_LIMIT),
            ("admin_execution_limit", ADMIN_EXECUTION_LIMIT),
        ):
            object.__setattr__(
                self, name, _bounded_int(getattr(self, name), name, maximum)
            )
        for name, maximum in (
            ("shared_read_window_seconds", SHARED_READ_WINDOW_SECONDS),
            ("safe_request_window_seconds", SAFE_REQUEST_WINDOW_SECONDS),
            ("admin_preview_window_seconds", ADMIN_PREVIEW_WINDOW_SECONDS),
            ("admin_execution_window_seconds", ADMIN_EXECUTION_WINDOW_SECONDS),
        ):
            object.__setattr__(
                self, name, _bounded_window(getattr(self, name), name, maximum)
            )

    # Naming aliases make the reviewed units explicit for configuration
    # adapters without creating a second mutable source of truth.
    @property
    def shared_reads_user_per_minute(self) -> int:
        return self.shared_read_user_limit

    @property
    def shared_reads_chat_per_minute(self) -> int:
        return self.shared_read_chat_limit

    @property
    def shared_reads_global_per_minute(self) -> int:
        return self.shared_read_global_limit

    @property
    def safe_requests_user_per_ten_minutes(self) -> int:
        return self.safe_request_user_limit

    @property
    def safe_requests_chat_per_ten_minutes(self) -> int:
        return self.safe_request_chat_limit

    @property
    def safe_requests_global_per_ten_minutes(self) -> int:
        return self.safe_request_global_limit

    @property
    def admin_previews_per_minute(self) -> int:
        return self.admin_preview_limit

    @property
    def admin_executions_per_ten_minutes(self) -> int:
        return self.admin_execution_limit

    @property
    def shared_read(self) -> RateRule:
        return RateRule(
            self.shared_read_global_limit,
            self.shared_read_window_seconds,
            ("user", "chat", "global"),
        )

    @property
    def safe_request(self) -> RateRule:
        return RateRule(
            self.safe_request_global_limit,
            self.safe_request_window_seconds,
            ("user", "chat", "global"),
        )

    @property
    def admin_preview(self) -> RateRule:
        return RateRule(
            self.admin_preview_limit,
            self.admin_preview_window_seconds,
            ("global",),
        )

    @property
    def admin_execution(self) -> RateRule:
        return RateRule(
            self.admin_execution_limit,
            self.admin_execution_window_seconds,
            ("global",),
        )

    def rule(self, operation: str | RateOperation) -> RateRule:
        """Return a rule for an operation after normalizing its spelling."""

        operation_value = _canonical_operation(operation)
        if operation_value is RateOperation.SHARED_READ:
            return self.shared_read
        if operation_value is RateOperation.SAFE_REQUEST:
            return self.safe_request
        if operation_value is RateOperation.ADMIN_PREVIEW:
            return self.admin_preview
        return self.admin_execution

    def limits(self, operation: str | RateOperation) -> Mapping[str, tuple[int, int]]:
        """Return immutable-friendly ``scope -> (count, window)`` details."""

        operation_value = _canonical_operation(operation)
        if operation_value is RateOperation.SHARED_READ:
            return {
                "user": (self.shared_read_user_limit, self.shared_read_window_seconds),
                "chat": (self.shared_read_chat_limit, self.shared_read_window_seconds),
                "global": (
                    self.shared_read_global_limit,
                    self.shared_read_window_seconds,
                ),
            }
        if operation_value is RateOperation.SAFE_REQUEST:
            return {
                "user": (
                    self.safe_request_user_limit,
                    self.safe_request_window_seconds,
                ),
                "chat": (
                    self.safe_request_chat_limit,
                    self.safe_request_window_seconds,
                ),
                "global": (
                    self.safe_request_global_limit,
                    self.safe_request_window_seconds,
                ),
            }
        if operation_value is RateOperation.ADMIN_PREVIEW:
            return {
                "global": (self.admin_preview_limit, self.admin_preview_window_seconds)
            }
        return {
            "global": (self.admin_execution_limit, self.admin_execution_window_seconds)
        }


DEFAULT_RATE_LIMIT_POLICY: Final[RateLimitPolicy] = RateLimitPolicy()


def _canonical_operation(operation: str | RateOperation) -> RateOperation:
    if isinstance(operation, RateOperation):
        return operation
    if not isinstance(operation, str):
        raise TypeError("unknown rate-limited operation")
    value = operation.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "read": RateOperation.SHARED_READ,
        "reads": RateOperation.SHARED_READ,
        "shared": RateOperation.SHARED_READ,
        "shared_read": RateOperation.SHARED_READ,
        "shared_reads": RateOperation.SHARED_READ,
        "search": RateOperation.SHARED_READ,
        "status": RateOperation.SHARED_READ,
        "request": RateOperation.SAFE_REQUEST,
        "requests": RateOperation.SAFE_REQUEST,
        "safe_request": RateOperation.SAFE_REQUEST,
        "safe_requests": RateOperation.SAFE_REQUEST,
        "request_mutation": RateOperation.SAFE_REQUEST,
        "request_mutations": RateOperation.SAFE_REQUEST,
        "admin_preview": RateOperation.ADMIN_PREVIEW,
        "admin_previews": RateOperation.ADMIN_PREVIEW,
        "preview": RateOperation.ADMIN_PREVIEW,
        "admin_execution": RateOperation.ADMIN_EXECUTION,
        "admin_executions": RateOperation.ADMIN_EXECUTION,
        "execution": RateOperation.ADMIN_EXECUTION,
        "execute": RateOperation.ADMIN_EXECUTION,
        "confirmed_execution": RateOperation.ADMIN_EXECUTION,
        "dashboard_recovery": RateOperation.ADMIN_EXECUTION,
    }
    try:
        return aliases[value]
    except KeyError as exc:
        raise ValueError("unknown rate-limited operation") from exc


def _id_value(name: str, value: object, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if name == "chat_id":
        if value == 0:
            raise ValueError("chat_id must be non-zero")
    elif value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Result of checking/consuming one operation."""

    allowed: bool
    operation: RateOperation
    remaining: int
    retry_after: float = 0.0
    blocked_scope: str | None = None
    limit: int = 0
    window_seconds: int = 0

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(slots=True)
class _Bucket:
    timestamps: deque[float] = field(default_factory=deque)


class InMemoryRateLimiter:
    """Atomic sliding-window limiter suitable for one companion process."""

    def __init__(
        self,
        policy: RateLimitPolicy | None = None,
        *,
        max_buckets: int = MAX_RATE_BUCKETS,
        clock: object = time.time,
    ) -> None:
        if policy is not None and not isinstance(policy, RateLimitPolicy):
            raise TypeError("policy must be RateLimitPolicy")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if (
            isinstance(max_buckets, bool)
            or not isinstance(max_buckets, int)
            or max_buckets <= 0
            or max_buckets > MAX_RATE_BUCKETS
        ):
            raise ValueError(f"max_buckets must be between 1 and {MAX_RATE_BUCKETS}")
        self.policy = DEFAULT_RATE_LIMIT_POLICY if policy is None else policy
        self.clock = clock
        self.max_buckets = max_buckets
        self._buckets: dict[tuple[str, str, int | str], _Bucket] = {}
        self._lock = threading.Lock()

    def _now(self, now: float | None) -> float:
        current = float(self.clock() if now is None else now)  # type: ignore[operator]
        if not math.isfinite(current):
            raise ValueError("rate-limit time must be finite")
        return current

    @staticmethod
    def _key(scope: str, user_id: int | None, chat_id: int | None) -> int | str:
        if scope == "user":
            assert user_id is not None
            return user_id
        if scope == "chat":
            assert chat_id is not None
            return chat_id
        return "global"

    @staticmethod
    def _prune(bucket: _Bucket, current: float, window: int) -> None:
        boundary = current - window
        while bucket.timestamps and bucket.timestamps[0] <= boundary:
            bucket.timestamps.popleft()

    def _drop_empty_locked(self, current: float) -> int:
        """Prune and remove expired buckets while the limiter lock is held."""

        removed = 0
        for key, bucket in tuple(self._buckets.items()):
            operation_text, _scope, _bucket_key = key
            operation = RateOperation(operation_text)
            self._prune(bucket, current, self.policy.rule(operation).window_seconds)
            if not bucket.timestamps:
                del self._buckets[key]
                removed += 1
        return removed

    def _check_locked(
        self,
        operation: RateOperation,
        *,
        user_id: int | None,
        chat_id: int | None,
        current: float,
        consume: bool,
    ) -> RateLimitDecision:
        limits = self.policy.limits(operation)
        if "user" in limits and user_id is None:
            raise ValueError("user_id is required for this rate-limited operation")
        if "chat" in limits and chat_id is None:
            raise ValueError("chat_id is required for this rate-limited operation")

        checked: list[
            tuple[str, tuple[str, str, int | str], _Bucket | None, int, int]
        ] = []
        remaining = min(limit for limit, _window in limits.values())
        blocked_scope: str | None = None
        retry_after = 0.0
        for scope, (limit, window) in limits.items():
            key = (operation.value, scope, self._key(scope, user_id, chat_id))
            bucket = self._buckets.get(key)
            if bucket is not None:
                self._prune(bucket, current, window)
            count = 0 if bucket is None else len(bucket.timestamps)
            scope_remaining = max(0, limit - count)
            remaining = min(remaining, scope_remaining)
            checked.append((scope, key, bucket, limit, window))
            if count >= limit and blocked_scope is None:
                assert bucket is not None
                blocked_scope = scope
                retry_after = max(0.0, bucket.timestamps[0] + window - current)

        if blocked_scope is not None:
            return RateLimitDecision(
                False,
                operation,
                remaining,
                retry_after,
                blocked_scope,
                limits[blocked_scope][0],
                limits[blocked_scope][1],
            )

        if consume:
            if checked:
                self._drop_empty_locked(current)
            missing = [entry for entry in checked if entry[1] not in self._buckets]
            if missing:
                if len(self._buckets) + len(missing) > self.max_buckets:
                    return RateLimitDecision(
                        False,
                        operation,
                        0,
                        0.0,
                        "capacity",
                        0,
                        0,
                    )
                for _scope, key, _bucket, _limit, _window in checked:
                    bucket = self._buckets.get(key)
                    if bucket is None:
                        bucket = _Bucket()
                        self._buckets[key] = bucket
                    bucket.timestamps.append(current)
            else:
                for _scope, key, _bucket, _limit, _window in checked:
                    bucket = self._buckets[key]
                    bucket.timestamps.append(current)
        # The operation's global budget is the useful summary when all scopes
        # are available; the minimum above still communicates the tightest
        # remaining scope to callers.
        global_limit, global_window = limits.get("global", next(iter(limits.values())))
        return RateLimitDecision(
            True,
            operation,
            remaining - (1 if consume else 0),
            0.0,
            None,
            global_limit,
            global_window,
        )

    def check(
        self,
        operation: str | RateOperation,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        now: float | None = None,
    ) -> RateLimitDecision:
        """Inspect a budget without consuming it."""

        normalized = _canonical_operation(operation)
        if user_id is None:
            user_id = actor_user_id
        elif actor_user_id is not None and user_id != actor_user_id:
            raise ValueError("conflicting actor user IDs")
        if chat_id is None:
            chat_id = actor_chat_id
        elif actor_chat_id is not None and chat_id != actor_chat_id:
            raise ValueError("conflicting actor chat IDs")
        user = _id_value("user_id", user_id, allow_none=True)
        chat = _id_value("chat_id", chat_id, allow_none=True)
        current = self._now(now)
        with self._lock:
            return self._check_locked(
                normalized,
                user_id=user,
                chat_id=chat,
                current=current,
                consume=False,
            )

    def consume(
        self,
        operation: str | RateOperation,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        now: float | None = None,
    ) -> RateLimitDecision:
        """Atomically consume one unit from every required scope."""

        normalized = _canonical_operation(operation)
        if user_id is None:
            user_id = actor_user_id
        elif actor_user_id is not None and user_id != actor_user_id:
            raise ValueError("conflicting actor user IDs")
        if chat_id is None:
            chat_id = actor_chat_id
        elif actor_chat_id is not None and chat_id != actor_chat_id:
            raise ValueError("conflicting actor chat IDs")
        user = _id_value("user_id", user_id, allow_none=True)
        chat = _id_value("chat_id", chat_id, allow_none=True)
        current = self._now(now)
        with self._lock:
            return self._check_locked(
                normalized,
                user_id=user,
                chat_id=chat,
                current=current,
                consume=True,
            )

    def allow(
        self,
        operation: str | RateOperation,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        now: float | None = None,
    ) -> bool:
        """Consume one unit and return a simple boolean decision."""

        return bool(
            self.consume(
                operation,
                user_id=user_id,
                chat_id=chat_id,
                actor_user_id=actor_user_id,
                actor_chat_id=actor_chat_id,
                now=now,
            )
        )

    def try_consume(
        self,
        operation: str | RateOperation,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        now: float | None = None,
    ) -> bool:
        return self.allow(
            operation,
            user_id=user_id,
            chat_id=chat_id,
            actor_user_id=actor_user_id,
            actor_chat_id=actor_chat_id,
            now=now,
        )

    def enforce(
        self,
        operation: str | RateOperation,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        now: float | None = None,
    ) -> RateLimitDecision:
        """Consume or raise :class:`RateLimitExceeded`."""

        decision = self.consume(
            operation,
            user_id=user_id,
            chat_id=chat_id,
            actor_user_id=actor_user_id,
            actor_chat_id=actor_chat_id,
            now=now,
        )
        if not decision.allowed:
            raise RateLimitExceeded(
                operation=decision.operation,
                scope=decision.blocked_scope,
                retry_after=decision.retry_after,
            )
        return decision

    require = enforce

    def cleanup(self, *, now: float | None = None) -> int:
        """Drop expired buckets and return the number removed."""

        current = self._now(now)
        with self._lock:
            return self._drop_empty_locked(current)

    def reset(self) -> None:
        """Clear all in-memory counters (primarily useful in tests)."""

        with self._lock:
            self._buckets.clear()

    @property
    def bucket_count(self) -> int:
        with self._lock:
            return len(self._buckets)


RateLimiter = InMemoryRateLimiter
SlidingWindowRateLimiter = InMemoryRateLimiter
RateLimitConfig = RateLimitPolicy
RateLimits = RateLimitPolicy


__all__ = [
    "ADMIN_EXECUTIONS_PER_TEN_MINUTES",
    "ADMIN_EXECUTION_LIMIT",
    "ADMIN_EXECUTION_PER_TEN_MINUTES",
    "ADMIN_EXECUTION_WINDOW_SECONDS",
    "ADMIN_PREVIEWS_PER_MINUTE",
    "ADMIN_PREVIEW_LIMIT",
    "ADMIN_PREVIEW_PER_MINUTE",
    "ADMIN_PREVIEW_WINDOW_SECONDS",
    "DEFAULT_RATE_LIMIT_POLICY",
    "MAX_RATE_BUCKETS",
    "SAFE_REQUESTS_PER_CHAT",
    "SAFE_REQUESTS_PER_GLOBAL",
    "SAFE_REQUESTS_PER_USER",
    "SAFE_REQUEST_CHAT_LIMIT",
    "SAFE_REQUEST_CHAT_PER_TEN_MINUTES",
    "SAFE_REQUEST_GLOBAL_LIMIT",
    "SAFE_REQUEST_USER_LIMIT",
    "SAFE_REQUEST_USER_PER_TEN_MINUTES",
    "SAFE_REQUEST_WINDOW_SECONDS",
    "SHARED_READS_PER_CHAT",
    "SHARED_READS_PER_GLOBAL",
    "SHARED_READS_PER_USER",
    "SHARED_READ_CHAT_LIMIT",
    "SHARED_READ_CHAT_PER_MINUTE",
    "SHARED_READ_GLOBAL_LIMIT",
    "SHARED_READ_GLOBAL_PER_MINUTE",
    "SHARED_READ_USER_LIMIT",
    "SHARED_READ_USER_PER_MINUTE",
    "SHARED_READ_WINDOW_SECONDS",
    "InMemoryRateLimiter",
    "RateLimitConfig",
    "RateLimitDecision",
    "RateLimitError",
    "RateLimitExceeded",
    "RateLimitPolicy",
    "RateLimiter",
    "RateLimits",
    "RateOperation",
    "RateRule",
    "SlidingWindowRateLimiter",
    "TooManyRequests",
]
