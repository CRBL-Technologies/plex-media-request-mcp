"""Authenticated, actor-bound cursors for bounded normalized snapshots.

Cursor values are intentionally self-contained only as an authorization and
position handle.  They do not contain provider IDs, queue IDs, filesystem
paths, URLs, credentials, or serialized provider responses.  The corresponding
snapshot remains server-side and is addressed by a random opaque identifier.

The wire format is two unpadded base64url components: bounded canonical JSON
claims and an HMAC-SHA-256 signature.  The format mirrors the actor assertion
codec but uses a hard five-minute lifetime and an independent cursor key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast

from .auth import CanonicalizationError, JsonValue, canonical_json, parse_canonical_json

CURSOR_VERSION: Final[int] = 1
CURSOR_TTL_SECONDS: Final[int] = 5 * 60
MAX_CURSOR_TTL_SECONDS: Final[int] = CURSOR_TTL_SECONDS
MAX_SNAPSHOT_ITEMS: Final[int] = 5_000
MAX_CURSOR_BYTES: Final[int] = 16 * 1024
MAX_CURSOR_COMPONENT_BYTES: Final[int] = 8 * 1024
CURSOR_CLOCK_SKEW_SECONDS: Final[int] = 30
# All reviewed shared pagination contracts top out at 250 records.  Keeping
# the ceiling here prevents a signed-but-buggy caller from turning a cursor
# into an unbounded continuation request.
MAX_CURSOR_PAGE_SIZE: Final[int] = 250
MAX_SNAPSHOT_RECORDS: Final[int] = 256
MAX_SNAPSHOT_PARTIAL_ERRORS: Final[int] = 8

_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")
_OPAQUE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_QUERY_UNSET: Final[object] = object()


class CursorError(ValueError):
    """Base class for malformed, expired, or incorrectly bound cursors."""


class InvalidCursor(CursorError):
    """The cursor could not be decoded or its signature was invalid."""


class CursorExpired(CursorError):
    """The cursor is outside its five-minute validity window."""


class CursorBindingError(CursorError):
    """The cursor belongs to another actor, tool, filter, or page shape."""


class CursorSnapshotNotFound(CursorError):
    """The server-side snapshot no longer exists."""


# Friendly aliases retained for adapter code and tests.
ExpiredCursor = CursorExpired
CursorValidationError = InvalidCursor
SnapshotNotFound = CursorSnapshotNotFound


def _key_bytes(key: bytes | str) -> bytes:
    if isinstance(key, str):
        try:
            key = key.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise ValueError("cursor key must be valid UTF-8") from exc
    if not isinstance(key, bytes) or not key:
        raise ValueError("cursor key must be non-empty bytes")
    return key


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str, *, error_type: type[CursorError] = InvalidCursor) -> bytes:
    if not isinstance(value, str) or not value or not _TOKEN_RE.fullmatch(value):
        raise error_type("invalid cursor base64url component")
    if len(value) % 4 == 1:
        raise error_type("invalid cursor base64url padding")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise error_type("invalid cursor base64url component") from exc
    return decoded


def _bounded_text(name: str, value: object, *, max_bytes: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_bytes:
        raise InvalidCursor(f"{name} is not a bounded string")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise InvalidCursor(f"{name} is not valid UTF-8") from exc
    if len(encoded) > max_bytes:
        raise InvalidCursor(f"{name} exceeds the cursor bound")
    if any(ord(char) < 0x20 or char.isspace() for char in value):
        raise InvalidCursor(f"{name} contains controls or whitespace")
    return value


def _user_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidCursor("actor user_id must be a positive integer")
    if value > (1 << 53) - 1:
        raise InvalidCursor("actor user_id exceeds the safe integer range")
    return value


def _chat_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise InvalidCursor("actor chat_id must be a non-zero integer")
    if abs(value) > (1 << 53) - 1:
        raise InvalidCursor("actor chat_id exceeds the safe integer range")
    return value


def _nonnegative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidCursor(f"{name} must be a non-negative integer")
    if value > (1 << 53) - 1:
        raise InvalidCursor(f"{name} exceeds the safe integer range")
    return value


def _hash_text(value: str) -> str:
    try:
        data = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise CursorError("cursor filter must be valid UTF-8") from exc
    if len(data) > 64 * 1024:
        raise CursorError("cursor filter exceeds the binding bound")
    return hashlib.sha256(data).hexdigest()


def binding_hash(value: object) -> str:
    """Return a stable SHA-256 binding for a bounded query/filter value.

    JSON values use the same JCS-compatible encoder as actor assertions.  For
    a non-JSON object, callers must provide a text representation explicitly;
    arbitrary objects are rejected rather than coerced with ``repr``.
    """

    if isinstance(value, str):
        return _hash_text(value)
    try:
        encoded = canonical_json(cast(JsonValue, value), max_bytes=64 * 1024)
    except (CanonicalizationError, TypeError, ValueError) as exc:
        raise CursorError("cursor filter is not a bounded JSON value") from exc
    return hashlib.sha256(encoded).hexdigest()


cursor_binding_hash = binding_hash
filter_hash = binding_hash
query_hash = binding_hash


@dataclass(frozen=True, slots=True)
class CursorClaims:
    """Validated claims carried by a signed cursor."""

    user_id: int
    chat_id: int
    tool: str
    snapshot_id: str
    offset: int
    issued_at: int
    expires_at: int
    filter_hash: str
    kid: str
    version: int = CURSOR_VERSION
    page_size: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _user_id(self.user_id))
        object.__setattr__(self, "chat_id", _chat_id(self.chat_id))
        object.__setattr__(
            self, "tool", _bounded_text("tool", self.tool, max_bytes=128)
        )
        snapshot_id = _bounded_text("snapshot_id", self.snapshot_id, max_bytes=128)
        if not _OPAQUE_ID_RE.fullmatch(snapshot_id):
            raise InvalidCursor("snapshot_id must be an opaque token")
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "offset", _nonnegative_int("offset", self.offset))
        object.__setattr__(
            self, "issued_at", _nonnegative_int("issued_at", self.issued_at)
        )
        object.__setattr__(
            self, "expires_at", _nonnegative_int("expires_at", self.expires_at)
        )
        if self.expires_at < self.issued_at:
            raise InvalidCursor("cursor expires_at precedes issued_at")
        if self.expires_at - self.issued_at > MAX_CURSOR_TTL_SECONDS:
            raise InvalidCursor("cursor lifetime exceeds five-minute ceiling")
        if not isinstance(self.filter_hash, str) or not _SHA256_RE.fullmatch(
            self.filter_hash
        ):
            raise InvalidCursor("filter_hash must be a lowercase SHA-256 digest")
        object.__setattr__(self, "kid", _bounded_text("kid", self.kid, max_bytes=64))
        if self.version != CURSOR_VERSION:
            raise InvalidCursor("unsupported cursor version")
        if self.page_size is not None:
            page_size = self.page_size
            if (
                isinstance(page_size, bool)
                or not isinstance(page_size, int)
                or page_size <= 0
            ):
                raise InvalidCursor("page_size must be a positive integer")
            if page_size > MAX_CURSOR_PAGE_SIZE:
                raise InvalidCursor("page_size exceeds the reviewed pagination ceiling")
            object.__setattr__(self, "page_size", page_size)

    @property
    def actor(self) -> tuple[int, int]:
        return (self.user_id, self.chat_id)

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "v": self.version,
            "kid": self.kid,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "tool": self.tool,
            "snapshot": self.snapshot_id,
            "offset": self.offset,
            "filter_hash": self.filter_hash,
            "iat": self.issued_at,
            "exp": self.expires_at,
        }
        if self.page_size is not None:
            result["page_size"] = self.page_size
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CursorClaims:
        if not isinstance(value, Mapping):
            raise InvalidCursor("cursor claims must be an object")
        allowed = {
            "v",
            "kid",
            "user_id",
            "chat_id",
            "tool",
            "snapshot",
            "offset",
            "filter_hash",
            "iat",
            "exp",
            "page_size",
        }
        if any(not isinstance(key, str) for key in value):
            raise InvalidCursor("cursor claim names must be strings")
        unknown = set(value) - allowed
        if unknown:
            raise InvalidCursor("unknown cursor claim")
        required = allowed - {"page_size"}
        missing = required - set(value)
        if missing:
            raise InvalidCursor("missing cursor claim")
        try:
            return cls(
                version=value["v"],
                kid=value["kid"],
                user_id=value["user_id"],
                chat_id=value["chat_id"],
                tool=value["tool"],
                snapshot_id=value["snapshot"],
                offset=value["offset"],
                filter_hash=value["filter_hash"],
                issued_at=value["iat"],
                expires_at=value["exp"],
                page_size=value.get("page_size"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, InvalidCursor):
                raise
            raise InvalidCursor("invalid cursor claims") from exc


# Alternate descriptive spelling.
SignedCursorClaims = CursorClaims


class CursorSigner:
    """Issue and verify signed five-minute actor-bound cursors."""

    def __init__(
        self,
        key: bytes | str | None = None,
        *,
        kid: str = "current",
        keys: Mapping[str, bytes | str] | None = None,
        ttl: int = CURSOR_TTL_SECONDS,
        clock: object = time.time,
        clock_skew: int = CURSOR_CLOCK_SKEW_SECONDS,
    ) -> None:
        if key is not None and keys is not None:
            raise ValueError("pass key or keys, not both")
        if keys is None:
            if key is None:
                raise ValueError("a cursor HMAC key is required")
            keys = {kid: key}
        if not keys:
            raise ValueError("at least one cursor key is required")
        if (
            not isinstance(kid, str)
            or not kid
            or len(kid) > 64
            or any(char.isspace() for char in kid)
        ):
            raise ValueError("kid must be a bounded non-whitespace string")
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, int)
            or ttl <= 0
            or ttl > MAX_CURSOR_TTL_SECONDS
        ):
            raise ValueError("ttl must be between 1 and 300 seconds")
        if (
            isinstance(clock_skew, bool)
            or not isinstance(clock_skew, int)
            or clock_skew < 0
            or clock_skew > CURSOR_CLOCK_SKEW_SECONDS
        ):
            raise ValueError("clock_skew must be between 0 and 30 seconds")
        if not callable(clock):
            raise TypeError("clock must be callable")
        normalized_keys = {
            selector: _key_bytes(secret) for selector, secret in keys.items()
        }
        if kid not in normalized_keys:
            raise ValueError("active kid has no cursor key")
        self.kid = kid
        self.keys = normalized_keys
        self.ttl = ttl
        self.clock = clock
        self.clock_skew = clock_skew

    def _now(self, now: float | None) -> int:
        current = float(self.clock() if now is None else now)  # type: ignore[operator]
        if not math.isfinite(current) or current < 0:
            raise ValueError("cursor time must be a finite non-negative number")
        return int(current)

    @staticmethod
    def _snapshot_id(value: str | None) -> str:
        candidate = secrets.token_urlsafe(24) if value is None else value
        if not isinstance(candidate, str) or not _OPAQUE_ID_RE.fullmatch(candidate):
            raise ValueError("snapshot_id must be an opaque token")
        return candidate

    def issue(
        self,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        tool: str,
        snapshot_id: str | None = None,
        offset: int = 0,
        filter_hash: str | None = None,
        query: object | None = None,
        page_size: int | None = None,
        now: float | None = None,
    ) -> str:
        """Create a signed cursor for a normalized snapshot position."""

        if user_id is None:
            user_id = actor_user_id
        elif actor_user_id is not None and user_id != actor_user_id:
            raise ValueError("conflicting actor user IDs")
        if chat_id is None:
            chat_id = actor_chat_id
        elif actor_chat_id is not None and chat_id != actor_chat_id:
            raise ValueError("conflicting actor chat IDs")
        if user_id is None or chat_id is None:
            raise ValueError("actor user_id and chat_id are required")
        if filter_hash is None:
            filter_hash = binding_hash(None if query is None else query)
        current = self._now(now)
        claims = CursorClaims(
            user_id=_user_id(user_id),
            chat_id=_chat_id(chat_id),
            tool=tool,
            snapshot_id=self._snapshot_id(snapshot_id),
            offset=offset,
            filter_hash=filter_hash,
            issued_at=current,
            expires_at=current + self.ttl,
            kid=self.kid,
            page_size=page_size,
        )
        return self.encode(claims)

    create = issue
    mint = issue

    def encode(self, claims: CursorClaims) -> str:
        if not isinstance(claims, CursorClaims):
            raise TypeError("claims must be CursorClaims")
        if claims.kid != self.kid:
            raise InvalidCursor("claims kid does not match active cursor key")
        body = canonical_json(
            cast(JsonValue, claims.to_dict()), max_bytes=MAX_CURSOR_COMPONENT_BYTES
        )
        signature = hmac.new(
            self.keys.get(self.kid, b""), body, hashlib.sha256
        ).digest()
        token = f"{_b64encode(body)}.{_b64encode(signature)}"
        if len(token.encode("ascii")) > MAX_CURSOR_BYTES:
            raise InvalidCursor("cursor exceeds the size bound")
        return token

    sign = encode

    @staticmethod
    def decode(token: str) -> tuple[CursorClaims, bytes, bytes]:
        if not isinstance(token, str) or token.count(".") != 1:
            raise InvalidCursor("malformed cursor")
        try:
            token_bytes = token.encode("ascii", "strict")
        except UnicodeEncodeError as exc:
            raise InvalidCursor("cursor must use ASCII base64url components") from exc
        if len(token_bytes) > MAX_CURSOR_BYTES:
            raise InvalidCursor("cursor exceeds the size bound")
        body_text, signature_text = token.split(".", 1)
        body = _b64decode(body_text)
        signature = _b64decode(signature_text)
        if len(body) > MAX_CURSOR_COMPONENT_BYTES:
            raise InvalidCursor("cursor claims exceed the size bound")
        if len(signature) != hashlib.sha256().digest_size:
            raise InvalidCursor("invalid cursor signature length")
        try:
            parsed = parse_canonical_json(body)
        except CanonicalizationError as exc:
            raise InvalidCursor("cursor claims are not canonical JSON") from exc
        if not isinstance(parsed, dict):
            raise InvalidCursor("cursor claims must be an object")
        return CursorClaims.from_mapping(parsed), body, signature

    def verify(
        self,
        token: str,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        expected_tool: str | None = None,
        tool: str | None = None,
        expected_filter_hash: str | None = None,
        filter_hash: str | None = None,
        query: object = _QUERY_UNSET,
        expected_page_size: int | None = None,
        now: float | None = None,
    ) -> CursorClaims:
        """Verify a cursor against one complete continuation context.

        A cursor is a bearer capability only after it has been checked against
        the current actor, tool, and filter/query.  Requiring all three here
        keeps lower-level callers from accidentally treating a signed cursor
        as sufficient authorization on its own.
        """

        claims, body, signature = self.decode(token)
        key = self.keys.get(claims.kid)
        if key is None or not hmac.compare_digest(
            hmac.new(key, body, hashlib.sha256).digest(), signature
        ):
            raise InvalidCursor("invalid cursor signature")
        current = self._now(now)
        if claims.issued_at > current + self.clock_skew:
            raise CursorExpired("cursor is not yet valid")
        if claims.expires_at <= current:
            raise CursorExpired("cursor has expired")
        if claims.expires_at - claims.issued_at > self.ttl:
            raise InvalidCursor("cursor lifetime exceeds verifier limit")

        if (
            user_id is not None
            and actor_user_id is not None
            and user_id != actor_user_id
        ):
            raise CursorBindingError("conflicting cursor actor user IDs")
        if (
            chat_id is not None
            and actor_chat_id is not None
            and chat_id != actor_chat_id
        ):
            raise CursorBindingError("conflicting cursor actor chat IDs")
        expected_user = user_id if user_id is not None else actor_user_id
        expected_chat = chat_id if chat_id is not None else actor_chat_id
        if expected_user is None or expected_chat is None:
            raise CursorBindingError("cursor actor binding is required")
        if claims.user_id != _user_id(expected_user):
            raise CursorBindingError("cursor actor user does not match")
        if claims.chat_id != _chat_id(expected_chat):
            raise CursorBindingError("cursor actor chat does not match")
        if expected_tool is not None and tool is not None and expected_tool != tool:
            raise CursorBindingError("conflicting cursor tool bindings")
        requested_tool = expected_tool if expected_tool is not None else tool
        if requested_tool is None:
            raise CursorBindingError("cursor tool binding is required")
        if (
            expected_filter_hash is not None
            and filter_hash is not None
            and expected_filter_hash != filter_hash
        ):
            raise CursorBindingError("conflicting cursor filter bindings")
        if requested_tool is not None and claims.tool != requested_tool:
            raise CursorBindingError("cursor tool does not match")
        requested_filter = (
            expected_filter_hash if expected_filter_hash is not None else filter_hash
        )
        if requested_filter is not None and (
            not isinstance(requested_filter, str)
            or not _SHA256_RE.fullmatch(requested_filter)
        ):
            raise CursorBindingError("expected cursor filter is not a SHA-256 digest")
        if requested_filter is None:
            if query is _QUERY_UNSET:
                raise CursorBindingError("cursor filter binding is required")
            requested_filter = binding_hash(query)
        elif query is not _QUERY_UNSET and not hmac.compare_digest(
            requested_filter, binding_hash(query)
        ):
            raise CursorBindingError("conflicting cursor filter bindings")
        if requested_filter is None:
            raise CursorBindingError("cursor filter binding is required")
        if not hmac.compare_digest(claims.filter_hash, requested_filter):
            raise CursorBindingError("cursor filter does not match")
        if expected_page_size is not None:
            if (
                isinstance(expected_page_size, bool)
                or not isinstance(expected_page_size, int)
                or expected_page_size <= 0
            ):
                raise CursorBindingError("expected cursor page size is invalid")
            if claims.page_size is not None and claims.page_size != expected_page_size:
                raise CursorBindingError("cursor page size does not match")
        return claims

    validate = verify

    def add_key(self, kid: str, key: bytes | str) -> None:
        if (
            not isinstance(kid, str)
            or not kid
            or len(kid) > 64
            or any(char.isspace() for char in kid)
        ):
            raise ValueError("kid must be a bounded non-whitespace string")
        self.keys[kid] = _key_bytes(key)

    def remove_key(self, kid: str) -> None:
        if kid == self.kid:
            raise ValueError("cannot remove the active cursor key")
        self.keys.pop(kid, None)


SignedCursor = CursorSigner
CursorCodec = CursorSigner


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    """Server-side bounded normalized snapshot addressed by a cursor."""

    snapshot_id: str
    user_id: int
    chat_id: int
    tool: str
    filter_hash: str
    issued_at: int
    expires_at: int
    items: tuple[Any, ...] = field(default_factory=tuple)
    truncated: bool = False
    # Safe-view metadata is stored with the snapshot so continuation pages
    # cannot report a different point-in-time or partial-error set.
    as_of: Any | None = None
    total_count: int | None = None
    partial_errors: tuple[Any, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Reuse claim validation for actor/tool/filter/clock fields without
        # retaining a signed token in the snapshot itself.
        CursorClaims(
            user_id=self.user_id,
            chat_id=self.chat_id,
            tool=self.tool,
            snapshot_id=self.snapshot_id,
            offset=0,
            filter_hash=self.filter_hash,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            kid="snapshot",
        )
        selected: list[Any] = []
        for item in self.items:
            if len(selected) >= MAX_SNAPSHOT_ITEMS:
                raise ValueError("snapshot exceeds 5,000-item cap")
            selected.append(item)
        object.__setattr__(self, "items", tuple(selected))
        if not isinstance(self.truncated, bool):
            raise TypeError("snapshot truncated flag must be boolean")
        if self.total_count is not None:
            if (
                isinstance(self.total_count, bool)
                or not isinstance(self.total_count, int)
                or self.total_count < 0
            ):
                raise ValueError("snapshot total must be a non-negative integer")
            if self.total_count > MAX_SNAPSHOT_ITEMS:
                object.__setattr__(self, "total_count", MAX_SNAPSHOT_ITEMS)
                object.__setattr__(self, "truncated", True)
            if self.total_count < len(self.items):
                object.__setattr__(self, "total_count", len(self.items))
            if self.total_count > len(self.items):
                object.__setattr__(self, "truncated", True)
        errors: list[Any] = []
        for error in self.partial_errors:
            if len(errors) >= MAX_SNAPSHOT_PARTIAL_ERRORS:
                raise ValueError("snapshot contains too many partial errors")
            errors.append(error)
        object.__setattr__(self, "partial_errors", tuple(errors))

    @property
    def total(self) -> int:
        return len(self.items) if self.total_count is None else self.total_count


Snapshot = SnapshotRecord


class SnapshotStore:
    """Bounded in-memory snapshot registry used by safe views and tests."""

    def __init__(
        self,
        signer: CursorSigner,
        *,
        max_items: int = MAX_SNAPSHOT_ITEMS,
        max_records: int = MAX_SNAPSHOT_RECORDS,
        clock: object = time.time,
    ) -> None:
        if not isinstance(signer, CursorSigner):
            raise TypeError("signer must be CursorSigner")
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or max_items <= 0
            or max_items > MAX_SNAPSHOT_ITEMS
        ):
            raise ValueError("max_items must be between 1 and 5,000")
        if (
            isinstance(max_records, bool)
            or not isinstance(max_records, int)
            or max_records <= 0
            or max_records > MAX_SNAPSHOT_RECORDS
        ):
            raise ValueError(
                f"max_records must be between 1 and {MAX_SNAPSHOT_RECORDS}"
            )
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.signer = signer
        self.max_items = max_items
        self.max_records = max_records
        self.clock = clock
        self._records: dict[str, SnapshotRecord] = {}
        self._lock = threading.Lock()

    def _now(self, now: float | None) -> int:
        current = float(self.clock() if now is None else now)  # type: ignore[operator]
        if not math.isfinite(current) or current < 0:
            raise ValueError("snapshot time must be a finite non-negative number")
        return int(current)

    def create(
        self,
        items: Iterable[Any],
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        tool: str,
        filter_hash: str | None = None,
        query: object | None = None,
        as_of: Any | None = None,
        total: int | None = None,
        partial_errors: Iterable[Any] = (),
        truncated: bool = False,
        now: float | None = None,
    ) -> SnapshotRecord:
        if user_id is None:
            user_id = actor_user_id
        elif actor_user_id is not None and user_id != actor_user_id:
            raise ValueError("conflicting actor user IDs")
        if chat_id is None:
            chat_id = actor_chat_id
        elif actor_chat_id is not None and chat_id != actor_chat_id:
            raise ValueError("conflicting actor chat IDs")
        if user_id is None or chat_id is None:
            raise ValueError("actor user_id and chat_id are required")
        if filter_hash is None:
            filter_hash = binding_hash(None if query is None else query)
        current = self._now(now)
        # Consume one item beyond the cap only to prove truncation; do not
        # accumulate an unbounded provider response.
        if not isinstance(truncated, bool):
            raise TypeError("snapshot truncated flag must be boolean")
        selected: list[Any] = []
        selected_truncated = truncated
        for item in items:
            if len(selected) >= self.max_items:
                selected_truncated = True
                break
            selected.append(item)
        reported_total = len(selected) if total is None else total
        if (
            isinstance(reported_total, bool)
            or not isinstance(reported_total, int)
            or reported_total < 0
        ):
            raise ValueError("snapshot total must be a non-negative integer")
        if reported_total > MAX_SNAPSHOT_ITEMS:
            reported_total = MAX_SNAPSHOT_ITEMS
            selected_truncated = True
        if reported_total < len(selected):
            reported_total = len(selected)
        if reported_total > len(selected):
            selected_truncated = True
        bounded_errors: list[Any] = []
        for error in partial_errors:
            if len(bounded_errors) >= MAX_SNAPSHOT_PARTIAL_ERRORS:
                raise ValueError("snapshot contains too many partial errors")
            bounded_errors.append(error)
        snapshot = SnapshotRecord(
            snapshot_id=self.signer._snapshot_id(None),
            user_id=_user_id(user_id),
            chat_id=_chat_id(chat_id),
            tool=tool,
            filter_hash=filter_hash,
            issued_at=current,
            expires_at=current + self.signer.ttl,
            items=tuple(selected),
            truncated=selected_truncated,
            as_of=as_of,
            total_count=reported_total,
            partial_errors=tuple(bounded_errors),
        )
        with self._lock:
            # Expire old rows eagerly and evict the oldest live row when the
            # bounded registry is full.  First-page requests cannot grow the
            # process indefinitely between explicit cleanup calls.
            expired = [
                key
                for key, record in self._records.items()
                if record.expires_at <= current
            ]
            for key in expired:
                del self._records[key]
            while len(self._records) >= self.max_records:
                oldest = next(iter(self._records), None)
                if oldest is None:
                    break
                del self._records[oldest]
            self._records[snapshot.snapshot_id] = snapshot
        return snapshot

    create_snapshot = create

    def cursor(
        self,
        snapshot: SnapshotRecord,
        *,
        offset: int = 0,
        page_size: int | None = None,
        now: float | None = None,
    ) -> str:
        if not isinstance(snapshot, SnapshotRecord):
            raise TypeError("snapshot must be SnapshotRecord")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or offset > len(snapshot.items)
        ):
            raise CursorBindingError("cursor offset is outside the snapshot")
        current = self._now(now)
        if snapshot.expires_at <= current:
            raise CursorExpired("snapshot has expired")
        return self.signer.issue(
            user_id=snapshot.user_id,
            chat_id=snapshot.chat_id,
            tool=snapshot.tool,
            snapshot_id=snapshot.snapshot_id,
            offset=offset,
            filter_hash=snapshot.filter_hash,
            page_size=page_size,
            now=current,
        )

    issue = cursor
    make_cursor = cursor

    def get(
        self,
        token: str,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        expected_tool: str | None = None,
        expected_filter_hash: str | None = None,
        query: object = _QUERY_UNSET,
        expected_page_size: int | None = None,
        now: float | None = None,
    ) -> SnapshotRecord:
        claims = self.signer.verify(
            token,
            user_id=user_id,
            chat_id=chat_id,
            actor_user_id=actor_user_id,
            actor_chat_id=actor_chat_id,
            expected_tool=expected_tool,
            expected_filter_hash=expected_filter_hash,
            query=query,
            expected_page_size=expected_page_size,
            now=now,
        )
        current = self._now(now)
        with self._lock:
            record = self._records.get(claims.snapshot_id)
        if record is None:
            raise CursorSnapshotNotFound("cursor snapshot is unavailable")
        if record.expires_at <= current:
            with self._lock:
                self._records.pop(record.snapshot_id, None)
            raise CursorExpired("snapshot has expired")
        if record.user_id != claims.user_id or record.chat_id != claims.chat_id:
            raise CursorBindingError("snapshot actor does not match cursor")
        if record.tool != claims.tool or record.filter_hash != claims.filter_hash:
            raise CursorBindingError("snapshot binding does not match cursor")
        if claims.offset > len(record.items):
            raise CursorBindingError("cursor offset is outside the snapshot")
        return record

    resolve = get

    def delete(self, snapshot_id: str) -> None:
        with self._lock:
            self._records.pop(snapshot_id, None)

    def cleanup(self, *, now: float | None = None) -> int:
        current = self._now(now)
        with self._lock:
            expired = [
                key
                for key, record in self._records.items()
                if record.expires_at <= current
            ]
            for key in expired:
                del self._records[key]
        return len(expired)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


InMemorySnapshotStore = SnapshotStore
CursorSnapshotStore = SnapshotStore


__all__ = [
    "CURSOR_CLOCK_SKEW_SECONDS",
    "CURSOR_TTL_SECONDS",
    "CURSOR_VERSION",
    "MAX_CURSOR_BYTES",
    "MAX_CURSOR_COMPONENT_BYTES",
    "MAX_CURSOR_PAGE_SIZE",
    "MAX_CURSOR_TTL_SECONDS",
    "MAX_SNAPSHOT_PARTIAL_ERRORS",
    "MAX_SNAPSHOT_ITEMS",
    "MAX_SNAPSHOT_RECORDS",
    "CursorBindingError",
    "CursorCodec",
    "CursorError",
    "CursorExpired",
    "CursorSigner",
    "CursorSnapshotNotFound",
    "CursorSnapshotStore",
    "CursorValidationError",
    "ExpiredCursor",
    "InMemorySnapshotStore",
    "InvalidCursor",
    "SignedCursor",
    "SignedCursorClaims",
    "Snapshot",
    "SnapshotNotFound",
    "SnapshotRecord",
    "SnapshotStore",
    "binding_hash",
    "cursor_binding_hash",
    "filter_hash",
    "query_hash",
]
