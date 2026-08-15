"""Authentication primitives for the media companion.

This module deliberately contains no HTTP or MCP code.  It provides the small
set of deterministic, transport-independent operations that those adapters
will need later:

* bounded RFC 8785/JCS-compatible JSON and SHA-256 argument hashes;
* short-lived HMAC-SHA-256 actor assertions with key rotation;
* an atomic in-memory nonce replay store for tests (the protocol is suitable
  for a database-backed implementation in production);
* strict handling of a single actor header; and
* a five-minute, one-time, hash-only confirmation capability lifecycle.

The wire representation of an actor assertion is intentionally not JWT.  It
is two unpadded base64url components: the canonical claims JSON and its
HMAC-SHA-256 signature.  ``kid`` and the protocol version are claims, so the
signature covers the key selector as well as every authorization binding.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, NoReturn, Protocol, Self, TypeAlias, cast

# ---------------------------------------------------------------------------
# Constants and errors
# ---------------------------------------------------------------------------

ACTOR_HEADER: Final[str] = "X-CRBL-Actor"
ACTOR_ASSERTION_VERSION: Final[int] = 1
ACTOR_ASSERTION_LIFETIME_SECONDS: Final[int] = 60
ACTOR_ASSERTION_CLOCK_SKEW_SECONDS: Final[int] = 30
CONFIRMATION_TOKEN_TTL_SECONDS: Final[int] = 5 * 60
CONFIRMATION_TOKEN_BYTES: Final[int] = 32
CONFIRMATION_CALLBACK_PREFIX: Final[str] = "crblc:"
# Backwards-compatible title-case spelling used by a few callers.
ConfirmationCallbackPrefix: Final[str] = CONFIRMATION_CALLBACK_PREFIX

# Numeric protocol: Python ``int`` values are restricted to the ECMAScript safe
# integer range, while ``float`` values must be finite IEEE-754 doubles and are
# serialized with the ECMAScript/JCS number algorithm below.  Callers that need
# larger exact integers must send them as strings.  This explicit boundary keeps
# actor IDs, timestamps, and ordinary tool arguments stable across Python and
# JavaScript implementations instead of silently rounding a Python integer.
JCS_MAX_SAFE_INTEGER: Final[int] = (1 << 53) - 1

# These are deliberately conservative.  Auth material is small, and bounded
# canonicalization is important because callers may pass model-controlled
# arguments.
MAX_CANONICAL_DEPTH: Final[int] = 16
MAX_CANONICAL_CONTAINER_ITEMS: Final[int] = 256
MAX_CANONICAL_STRING_BYTES: Final[int] = 16 * 1024
MAX_CANONICAL_BYTES: Final[int] = 64 * 1024

_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class AuthError(ValueError):
    """Base class for malformed or unauthorized authentication material."""


class CanonicalizationError(AuthError):
    """Raised when a value is outside the bounded JSON subset."""


class DuplicateJsonKeyError(CanonicalizationError):
    """Raised when a JSON document contains duplicate object member names."""


class InvalidAssertion(AuthError):
    """Raised when an actor assertion cannot be verified."""


class MissingHeaderError(InvalidAssertion):
    """Raised when the actor header is absent."""


class DuplicateHeaderError(InvalidAssertion):
    """Raised when more than one actor header was received."""


class AssertionExpired(InvalidAssertion):
    """Raised when an actor assertion is outside its accepted time window."""


class ReplayError(InvalidAssertion):
    """Raised when an assertion or confirmation nonce has already been used."""


class ConfirmationError(AuthError):
    """Raised when a confirmation capability is missing or no longer valid."""


class ConfirmationExpired(ConfirmationError):
    """Raised when a confirmation capability has expired."""


class ConfirmationReplayError(ConfirmationError):
    """Raised when a consumed confirmation capability is presented again."""


class ConfirmationBindingError(ConfirmationError):
    """Raised when actor, message, preview, arguments, or target drifted."""


# The alias is useful to callers that prefer a generic expiration exception.
ExpiredAssertion = AssertionExpired


# ---------------------------------------------------------------------------
# Bounded canonical JSON
# ---------------------------------------------------------------------------


JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


def _utf8(value: str) -> bytes:
    """Encode a string while rejecting lone UTF-16 surrogate code points."""

    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError("strings must contain valid Unicode") from exc
    if len(encoded) > MAX_CANONICAL_STRING_BYTES:
        raise CanonicalizationError("string exceeds the canonicalization bound")
    return encoded


def _validate_json(
    value: Any,
    *,
    depth: int = 0,
    item_count: list[int],
    max_depth: int = MAX_CANONICAL_DEPTH,
) -> None:
    if depth > max_depth:
        raise CanonicalizationError("JSON nesting exceeds the canonicalization bound")

    if value is None or isinstance(value, bool):
        return

    # bool is an int subclass and must be checked before this branch.
    if isinstance(value, int):
        if abs(value) > JCS_MAX_SAFE_INTEGER:
            raise CanonicalizationError("integer is outside the JCS safe-number bound")
        return

    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("non-finite numbers are not valid JSON")
        return

    if isinstance(value, str):
        _utf8(value)
        return

    if isinstance(value, list):
        if len(value) > MAX_CANONICAL_CONTAINER_ITEMS:
            raise CanonicalizationError("array exceeds the canonicalization bound")
        item_count[0] += len(value)
        if item_count[0] > MAX_CANONICAL_CONTAINER_ITEMS * (MAX_CANONICAL_DEPTH + 1):
            raise CanonicalizationError("JSON contains too many values")
        for child in value:
            _validate_json(
                child,
                depth=depth + 1,
                item_count=item_count,
                max_depth=max_depth,
            )
        return

    if isinstance(value, dict):
        if len(value) > MAX_CANONICAL_CONTAINER_ITEMS:
            raise CanonicalizationError("object exceeds the canonicalization bound")
        item_count[0] += len(value)
        if item_count[0] > MAX_CANONICAL_CONTAINER_ITEMS * (MAX_CANONICAL_DEPTH + 1):
            raise CanonicalizationError("JSON contains too many values")
        for key, child in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("object keys must be strings")
            _utf8(key)
            _validate_json(
                child,
                depth=depth + 1,
                item_count=item_count,
                max_depth=max_depth,
            )
        return

    raise CanonicalizationError(f"unsupported JSON value type: {type(value).__name__}")


def _utf16_sort_key(value: str) -> bytes:
    """Return the RFC 8785 UTF-16 code-unit ordering key for an object key."""

    # Surrogates were rejected by ``_utf8``.  UTF-16BE therefore gives the
    # exact code-unit ordering required by JCS, including supplementary chars.
    return value.encode("utf-16-be", "strict")


def _canonical_number(value: float) -> str:
    if isinstance(value, int):
        return str(value)

    if value == 0.0:
        # JCS/ECMAScript canonicalizes both +0 and -0 as ``0``.
        return "0"

    text = repr(value).lower()
    if "e" not in text:
        if text.endswith(".0"):
            return text[:-2]
        return text

    mantissa, exponent_text = text.split("e", 1)
    exponent = int(exponent_text)
    sign = ""
    if mantissa.startswith("-"):
        sign, mantissa = "-", mantissa[1:]
    if "." in mantissa:
        before, after = mantissa.split(".", 1)
    else:
        before, after = mantissa, ""
    digits = before + after
    # repr() is shortest-roundtrip, so only an insignificant .0 can be
    # removed.  Removing all trailing zeros would change a significant digit
    # for values such as 1.2300 represented in a non-shortest implementation.
    digits = digits.rstrip("0") or "0"
    decimal_exponent = exponent + len(before) - 1

    # ECMAScript's Number::toString uses fixed notation for -6 <= n < 21.
    if -6 <= decimal_exponent < 21:
        decimal_position = decimal_exponent + 1
        if decimal_position <= 0:
            body = "0." + ("0" * (-decimal_position)) + digits
        elif decimal_position >= len(digits):
            body = digits + ("0" * (decimal_position - len(digits)))
        else:
            body = digits[:decimal_position] + "." + digits[decimal_position:]
        return sign + body

    coefficient = digits[0]
    if len(digits) > 1:
        coefficient += "." + digits[1:]
    exponent_sign = "+" if decimal_exponent >= 0 else "-"
    return f"{sign}{coefficient}e{exponent_sign}{abs(decimal_exponent)}"


def _canonical_encode(value: JsonValue) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _canonical_number(value)
    if isinstance(value, str):
        # ensure_ascii=False is required: JCS preserves Unicode code points
        # and emits UTF-8 instead of turning every non-ASCII character into an
        # alternate escaped representation.
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical_encode(child) for child in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: _utf16_sort_key(item[0]))
        return (
            "{"
            + ",".join(
                json.dumps(key, ensure_ascii=False, separators=(",", ":"))
                + ":"
                + _canonical_encode(child)
                for key, child in items
            )
            + "}"
        )
    # Validation should make this unreachable, but keep the serializer
    # defensive if a future caller bypasses it.
    raise CanonicalizationError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_json(
    value: Any,
    *,
    max_depth: int = MAX_CANONICAL_DEPTH,
    max_bytes: int = MAX_CANONICAL_BYTES,
) -> bytes:
    """Serialize a bounded Python JSON value using JCS-compatible rules.

    Supported values are ``None``, booleans, safe integers, finite floats,
    strings, lists, and dictionaries with string keys.  Unicode is preserved
    without normalization: composed and decomposed spellings intentionally
    hash differently.  ``bytes`` and JSON-ish Python objects such as tuples,
    sets, and ``Decimal`` are rejected rather than coerced.
    """

    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    # The public bound can be lowered per call, never raised above the module
    # safety ceiling.
    if max_depth > MAX_CANONICAL_DEPTH or max_bytes > MAX_CANONICAL_BYTES:
        raise ValueError("canonicalization bounds cannot be increased")
    _validate_json(value, depth=0, item_count=[0], max_depth=max_depth)
    encoded = _canonical_encode(value).encode("utf-8", "strict")
    if len(encoded) > max_bytes:
        raise CanonicalizationError("canonical JSON exceeds the byte bound")
    return encoded


def canonical_json_text(value: Any, **kwargs: int) -> str:
    """Return :func:`canonical_json` as UTF-8 text."""

    return canonical_json(value, **kwargs).decode("utf-8")


def _reject_json_constant(value: str) -> NoReturn:
    raise CanonicalizationError(f"non-finite JSON number: {value}")


def _object_pairs_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def parse_json(data: str | bytes | bytearray | memoryview) -> JsonValue:
    """Parse a JSON document with duplicate-key and UTF-8 rejection."""

    if isinstance(data, str):
        document = data
    else:
        try:
            document = bytes(data).decode("utf-8", "strict")
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise CanonicalizationError("JSON input must be valid UTF-8") from exc
    try:
        value = json.loads(
            document,
            object_pairs_hook=_object_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except DuplicateJsonKeyError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CanonicalizationError("invalid JSON document") from exc
    _validate_json(value, item_count=[0])
    return cast(JsonValue, value)


def parse_canonical_json(data: str | bytes | bytearray | memoryview) -> JsonValue:
    """Parse JSON and require that its bytes are already canonical JCS bytes."""

    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    value = parse_json(raw)
    if canonical_json(value) != raw:
        raise CanonicalizationError("JSON document is not canonical")
    return value


def canonical_json_hash(value: Any) -> str:
    """Return the lowercase SHA-256 hash of canonical JSON bytes."""

    return hashlib.sha256(canonical_json(value)).hexdigest()


def canonical_argument_hash(arguments: Any) -> str:
    """Hash the exact bounded tool arguments that were received."""

    return canonical_json_hash(arguments)


# Common short names used by callers and fixtures.
canonicalize_json = canonical_json
canonical_hash = canonical_json_hash
argument_hash = canonical_argument_hash
canonical_json_bytes = canonical_json
sha256_json = canonical_json_hash


# ---------------------------------------------------------------------------
# Actor assertion claims and signing
# ---------------------------------------------------------------------------


def _require_text(name: str, value: Any, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise InvalidAssertion(f"{name} must be a non-empty bounded string")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise InvalidAssertion(f"{name} must be valid UTF-8") from exc
    return value


def _require_int(name: str, value: Any, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidAssertion(f"{name} must be an integer")
    if abs(value) > JCS_MAX_SAFE_INTEGER:
        raise InvalidAssertion(f"{name} is outside the safe integer range")
    if minimum is not None and value < minimum:
        raise InvalidAssertion(f"{name} must be at least {minimum}")
    return value


_CLAIM_ALIASES: Final[dict[str, str]] = {
    "version": "v",
    "audience": "aud",
    "argument_hash": "args_hash",
    "arguments_hash": "args_hash",
    "arg_hash": "args_hash",
    "args_sha256": "args_hash",
    "issued_at": "iat",
    "expires_at": "exp",
    "telegram_user_id": "user_id",
    "telegram_chat_id": "chat_id",
}

_REQUIRED_CLAIMS: Final[frozenset[str]] = frozenset(
    {
        "v",
        "kid",
        "platform",
        "aud",
        "user_id",
        "chat_id",
        "chat_type",
        "role",
        "update_id",
        "update_type",
        "tool",
        "args_hash",
        "iat",
        "exp",
        "nonce",
    }
)
_OPTIONAL_CLAIMS: Final[frozenset[str]] = frozenset(
    {
        "allowlist_version",
        "allowlist_fingerprint",
        "message_id",
        "session_id",
        "callback_query_id",
        "capability_hash",
        "target_hash",
    }
)
_ALLOWED_CLAIMS: Final[frozenset[str]] = _REQUIRED_CLAIMS | _OPTIONAL_CLAIMS


@dataclass(frozen=True)
class ActorClaims(Mapping[str, Any]):
    """Validated actor assertion claims.

    The attribute names are descriptive Python names, while ``to_dict`` uses
    compact, stable wire names.  Mapping access accepts both spellings for
    the common aliases (for example ``claims["audience"]`` and
    ``claims["aud"]``).
    """

    version: int
    kid: str
    platform: str
    audience: str
    user_id: int
    chat_id: int
    chat_type: str
    role: str
    update_id: int
    update_type: str
    tool: str
    argument_hash: str
    issued_at: int
    expires_at: int
    nonce: str
    message_id: int | None = None
    session_id: str | None = None
    callback_query_id: str | None = None
    capability_hash: str | None = None
    target_hash: str | None = None
    allowlist_version: str | None = None
    allowlist_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version != ACTOR_ASSERTION_VERSION
        ):
            raise InvalidAssertion("unsupported actor assertion version")
        _require_text("kid", self.kid, max_length=64)
        _require_text("platform", self.platform, max_length=64)
        _require_text("aud", self.audience, max_length=128)
        _require_int("user_id", self.user_id, minimum=1)
        _require_int("chat_id", self.chat_id)
        if self.chat_id == 0:
            raise InvalidAssertion("chat_id cannot be zero")
        if self.chat_type not in {"private", "group", "supergroup", "channel"}:
            raise InvalidAssertion("unsupported chat_type")
        if self.role not in {"user", "admin"}:
            raise InvalidAssertion("role must be user or admin")
        _require_int("update_id", self.update_id, minimum=0)
        _require_text("update_type", self.update_type, max_length=64)
        _require_text("tool", self.tool, max_length=128)
        if not isinstance(self.argument_hash, str) or not _SHA256_RE.fullmatch(
            self.argument_hash
        ):
            raise InvalidAssertion(
                "argument_hash must be a lowercase SHA-256 hex digest"
            )
        _require_int("issued_at", self.issued_at)
        _require_int("expires_at", self.expires_at)
        if self.expires_at <= self.issued_at:
            raise InvalidAssertion("expires_at must follow issued_at")
        if self.expires_at - self.issued_at > ACTOR_ASSERTION_LIFETIME_SECONDS:
            raise InvalidAssertion("actor assertion lifetime exceeds 60 seconds")
        _require_text("nonce", self.nonce, max_length=128)
        if self.message_id is not None:
            _require_int("message_id", self.message_id, minimum=1)
        for name in ("session_id", "callback_query_id"):
            value = getattr(self, name)
            if value is not None:
                _require_text(name, value, max_length=256)
        for name in ("allowlist_version", "allowlist_fingerprint"):
            value = getattr(self, name)
            if value is not None:
                _require_text(name, value, max_length=256)
        for name in ("capability_hash", "target_hash"):
            value = getattr(self, name)
            if value is not None and not _SHA256_RE.fullmatch(value):
                raise InvalidAssertion(f"{name} must be a lowercase SHA-256 hex digest")

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "v": self.version,
            "kid": self.kid,
            "platform": self.platform,
            "aud": self.audience,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "role": self.role,
            "update_id": self.update_id,
            "update_type": self.update_type,
            "tool": self.tool,
            "args_hash": self.argument_hash,
            "iat": self.issued_at,
            "exp": self.expires_at,
            "nonce": self.nonce,
        }
        optional: dict[str, JsonValue | None] = {
            "message_id": self.message_id,
            "session_id": self.session_id,
            "callback_query_id": self.callback_query_id,
            "capability_hash": self.capability_hash,
            "target_hash": self.target_hash,
            "allowlist_version": self.allowlist_version,
            "allowlist_fingerprint": self.allowlist_fingerprint,
        }
        result.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        return result

    # Compact wire-name properties keep integrations that work directly with
    # claim names readable without making the dataclass itself mutable.
    @property
    def v(self) -> int:
        return self.version

    @property
    def aud(self) -> str:
        return self.audience

    @property
    def args_hash(self) -> str:
        return self.argument_hash

    @property
    def arg_hash(self) -> str:
        return self.argument_hash

    @property
    def iat(self) -> int:
        return self.issued_at

    @property
    def exp(self) -> int:
        return self.expires_at

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        default_kid: str | None = None,
        default_version: int = ACTOR_ASSERTION_VERSION,
    ) -> ActorClaims:
        if not isinstance(value, Mapping):
            raise InvalidAssertion("actor claims must be an object")
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise InvalidAssertion("actor claim names must be strings")
            key = _CLAIM_ALIASES.get(raw_key, raw_key)
            if key not in _ALLOWED_CLAIMS:
                raise InvalidAssertion(f"unknown actor claim: {raw_key}")
            if key in normalized:
                raise InvalidAssertion(f"duplicate actor claim alias: {raw_key}")
            normalized[key] = raw_value
        if "kid" not in normalized and default_kid is not None:
            normalized["kid"] = default_kid
        if "v" not in normalized:
            normalized["v"] = default_version
        missing = _REQUIRED_CLAIMS - normalized.keys()
        if missing:
            raise InvalidAssertion(
                "missing actor claims: " + ", ".join(sorted(missing))
            )
        return cls(
            version=normalized["v"],
            kid=normalized["kid"],
            platform=normalized["platform"],
            audience=normalized["aud"],
            user_id=normalized["user_id"],
            chat_id=normalized["chat_id"],
            chat_type=normalized["chat_type"],
            role=normalized["role"],
            update_id=normalized["update_id"],
            update_type=normalized["update_type"],
            tool=normalized["tool"],
            argument_hash=normalized["args_hash"],
            issued_at=normalized["iat"],
            expires_at=normalized["exp"],
            nonce=normalized["nonce"],
            message_id=normalized.get("message_id"),
            session_id=normalized.get("session_id"),
            callback_query_id=normalized.get("callback_query_id"),
            capability_hash=normalized.get("capability_hash"),
            target_hash=normalized.get("target_hash"),
            allowlist_version=normalized.get("allowlist_version"),
            allowlist_fingerprint=normalized.get("allowlist_fingerprint"),
        )

    # Mapping protocol -----------------------------------------------------
    def _mapping_key(self, key: str) -> str:
        if key in _ALLOWED_CLAIMS:
            return key
        alias = _CLAIM_ALIASES.get(key)
        if alias is not None:
            return alias
        raise KeyError(key)

    def __getitem__(self, key: str) -> Any:
        wire_key = self._mapping_key(key)
        return self.to_dict()[wire_key]

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


ActorAssertionClaims = ActorClaims
ActorAssertion = ActorClaims


def _key_bytes(key: bytes | str) -> bytes:
    if isinstance(key, str):
        key = key.encode("utf-8", "strict")
    if not isinstance(key, bytes) or not key:
        raise ValueError("HMAC key must be non-empty bytes")
    return key


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or not value
        or not re.fullmatch(r"[A-Za-z0-9_-]+", value)
    ):
        raise InvalidAssertion("invalid base64url component")
    if len(value) % 4 == 1:
        raise InvalidAssertion("invalid base64url padding")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise InvalidAssertion("invalid base64url component") from exc
    if _b64url_encode(decoded) != value:
        raise InvalidAssertion("non-canonical base64url component")
    return decoded


def _now_seconds(clock: Callable[[], float] | None = None) -> float:
    try:
        return _finite_seconds((clock or time.time)())
    except (TypeError, ValueError) as exc:
        raise ValueError("clock must return a finite number") from exc


def _finite_seconds(value: Any) -> float:
    """Coerce an explicit timestamp while rejecting NaN and infinities."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("timestamp must be a finite number")
    try:
        current = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("timestamp must be a finite number") from exc
    if not math.isfinite(current):
        raise ValueError("timestamp must be a finite number")
    return current


def _protocol_second(value: Any) -> int:
    """Convert a timestamp to a bounded whole protocol second."""

    current = _finite_seconds(value)
    if abs(current) > JCS_MAX_SAFE_INTEGER:
        raise ValueError("timestamp is outside the safe integer range")
    return int(current)


class ActorAssertionSigner:
    """Create short-lived JCS/HMAC actor assertions."""

    def __init__(
        self,
        key: bytes | str | Mapping[str, bytes | str] | None = None,
        *,
        kid: str = "current",
        active_kid: str | None = None,
        keys: Mapping[str, bytes | str] | None = None,
        lifetime: int = ACTOR_ASSERTION_LIFETIME_SECONDS,
        clock: Any = time.time,
    ) -> None:
        if active_kid is not None:
            if kid != "current" and kid != active_kid:
                raise ValueError("kid and active_kid disagree")
            kid = active_kid
        if not isinstance(kid, str) or not kid or len(kid) > 64:
            raise ValueError("kid must be a non-empty bounded string")
        if not isinstance(lifetime, int) or isinstance(lifetime, bool):
            raise TypeError("lifetime must be an integer")
        if lifetime <= 0 or lifetime > ACTOR_ASSERTION_LIFETIME_SECONDS:
            raise ValueError("lifetime must be between 1 and 60 seconds")
        if isinstance(key, Mapping):
            if keys is not None:
                raise ValueError("pass key mapping or keys, not both")
            keys = key
            key = None
        if keys is not None and key is not None:
            raise ValueError("pass key or keys, not both")
        if keys is None:
            if key is None:
                raise ValueError("an HMAC key is required")
            keys = {kid: key}
        if kid not in keys:
            raise ValueError("active kid has no signing key")
        self.kid = kid
        self.lifetime = lifetime
        self.clock = clock
        self._key = _key_bytes(keys[kid])

    def sign(
        self,
        claims: ActorClaims | Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> str:
        """Sign already assembled claims, requiring the active ``kid``."""

        current = _protocol_second(_now_seconds(self.clock) if now is None else now)
        if isinstance(claims, ActorClaims):
            normalized = claims
        else:
            data = dict(claims)
            data.setdefault("kid", self.kid)
            data.setdefault("v", ACTOR_ASSERTION_VERSION)
            data.setdefault("iat", current)
            data.setdefault("exp", current + self.lifetime)
            normalized = ActorClaims.from_mapping(data, default_kid=self.kid)
        if normalized.kid != self.kid:
            raise InvalidAssertion("claims kid does not match active signer key")
        if normalized.expires_at - normalized.issued_at > self.lifetime:
            raise InvalidAssertion("claim lifetime exceeds signer lifetime")
        body = canonical_json(normalized.to_dict())
        signature = hmac.new(self._key, body, hashlib.sha256).digest()
        return f"{_b64url_encode(body)}.{_b64url_encode(signature)}"

    def issue(
        self,
        *,
        audience: str,
        tool: str,
        arguments: Any,
        user_id: int,
        chat_id: int,
        chat_type: str,
        role: str,
        update_id: int,
        update_type: str,
        message_id: int | None = None,
        session_id: str | None = None,
        callback_query_id: str | None = None,
        capability_hash: str | None = None,
        target_hash: str | None = None,
        allowlist_version: str | None = None,
        allowlist_fingerprint: str | None = None,
        nonce: str | None = None,
        now: float | None = None,
    ) -> str:
        current = _protocol_second(_now_seconds(self.clock) if now is None else now)
        generated_nonce = nonce or _b64url_encode(secrets.token_bytes(16))
        claims = ActorClaims(
            version=ACTOR_ASSERTION_VERSION,
            kid=self.kid,
            platform="telegram",
            audience=audience,
            user_id=user_id,
            chat_id=chat_id,
            chat_type=chat_type,
            role=role,
            update_id=update_id,
            update_type=update_type,
            tool=tool,
            argument_hash=canonical_argument_hash(arguments),
            issued_at=current,
            expires_at=current + self.lifetime,
            nonce=generated_nonce,
            message_id=message_id,
            session_id=session_id,
            callback_query_id=callback_query_id,
            capability_hash=capability_hash,
            target_hash=target_hash,
            allowlist_version=allowlist_version,
            allowlist_fingerprint=allowlist_fingerprint,
        )
        return self.sign(claims, now=current)

    create = issue
    mint = issue
    create_assertion = issue
    mint_assertion = issue


class NonceReplayStore(Protocol):
    """Atomic nonce store protocol used by assertion verifiers."""

    def consume(self, nonce: str, expires_at: int, *, now: float | None = None) -> bool:
        """Record ``nonce`` and return false if it was already recorded."""

    def cleanup(self, *, now: float | None = None) -> int:
        """Remove expired nonce entries and return the number removed."""


class InMemoryNonceReplayStore:
    """Thread-safe nonce replay store for tests and local development."""

    def __init__(self, *, clock: Any = time.time) -> None:
        self.clock = clock
        self._values: dict[str, float] = {}
        self._lock = threading.Lock()

    def _purge_locked(self, now: float) -> int:
        expired = [nonce for nonce, expiry in self._values.items() if expiry <= now]
        for nonce in expired:
            del self._values[nonce]
        return len(expired)

    def consume(self, nonce: str, expires_at: int, *, now: float | None = None) -> bool:
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("nonce must be a non-empty string")
        if (
            not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or abs(expires_at) > JCS_MAX_SAFE_INTEGER
        ):
            raise TypeError("expires_at must be a safe integer")
        current = _now_seconds(self.clock) if now is None else _finite_seconds(now)
        if expires_at <= current:
            return False
        with self._lock:
            self._purge_locked(current)
            if nonce in self._values:
                return False
            self._values[nonce] = float(expires_at)
            return True

    # Names commonly used by database-backed implementations.
    check_and_store = consume
    reserve = consume
    put_if_absent = consume
    consume_once = consume

    def seen(self, nonce: str, *, now: float | None = None) -> bool:
        current = _now_seconds(self.clock) if now is None else _finite_seconds(now)
        with self._lock:
            self._purge_locked(current)
            return nonce in self._values

    def cleanup(self, *, now: float | None = None) -> int:
        current = _now_seconds(self.clock) if now is None else _finite_seconds(now)
        with self._lock:
            return self._purge_locked(current)

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)


NonceStore = InMemoryNonceReplayStore
NonceReplayStoreProtocol = NonceReplayStore


_MISSING = object()


class ActorAssertionVerifier:
    """Verify actor assertions with mandatory replay protection.

    ``verify`` is an authorization operation, not merely a signature parser:
    every successful call consumes a nonce from the injected replay store and
    requires a tool plus exact argument binding.  Use :meth:`verify_bound` at
    call sites where the expected audience is not already configured on this
    verifier.
    """

    def __init__(
        self,
        key: bytes | str | Mapping[str, bytes | str] | None = None,
        *,
        keys: Mapping[str, bytes | str] | None = None,
        expected_audience: str | None = None,
        expected_platform: str = "telegram",
        expected_allowlist_version: str | None = None,
        lifetime: int = ACTOR_ASSERTION_LIFETIME_SECONDS,
        clock_skew: int = ACTOR_ASSERTION_CLOCK_SKEW_SECONDS,
        nonce_store: NonceReplayStore,
        clock: Any = time.time,
    ) -> None:
        if isinstance(key, Mapping):
            if keys is not None:
                raise ValueError("pass key mapping or keys, not both")
            keys = key
            key = None
        if key is not None and keys is not None:
            raise ValueError("pass key or keys, not both")
        if keys is None:
            if key is None:
                raise ValueError("an HMAC key is required")
            keys = {"current": key}
        if not keys:
            raise ValueError("at least one verifier key is required")
        if nonce_store is None:
            raise ValueError("nonce_store is required for authorization verification")
        self.keys = {kid: _key_bytes(secret) for kid, secret in keys.items()}
        if expected_audience is not None:
            expected_audience = _require_text(
                "expected_audience", expected_audience, max_length=128
            )
        self.expected_audience = expected_audience
        self.expected_platform = expected_platform
        self.expected_allowlist_version = expected_allowlist_version
        if not isinstance(lifetime, int) or isinstance(lifetime, bool):
            raise TypeError("lifetime must be an integer")
        if lifetime <= 0 or lifetime > ACTOR_ASSERTION_LIFETIME_SECONDS:
            raise ValueError("lifetime must be between 1 and 60 seconds")
        if not isinstance(clock_skew, int) or isinstance(clock_skew, bool):
            raise TypeError("clock_skew must be an integer")
        if clock_skew < 0 or clock_skew > ACTOR_ASSERTION_CLOCK_SKEW_SECONDS:
            raise ValueError("clock_skew must be between 0 and 30 seconds")
        self.lifetime = lifetime
        self.clock_skew = clock_skew
        self.nonce_store = nonce_store
        self.clock = clock

    def add_key(self, kid: str, key: bytes | str) -> None:
        if not kid:
            raise ValueError("kid must be non-empty")
        self.keys[kid] = _key_bytes(key)

    def remove_key(self, kid: str) -> None:
        self.keys.pop(kid, None)

    def verify(
        self,
        token: str,
        *,
        expected_audience: str | None = None,
        expected_tool: str | None = None,
        arguments: JsonValue | object = _MISSING,
        expected_argument_hash: str | None = None,
        now: float | None = None,
        consume_nonce: bool = True,
    ) -> ActorClaims:
        if not consume_nonce:
            raise InvalidAssertion(
                "actor assertion nonce consumption cannot be disabled"
            )
        audience = expected_audience
        if audience is None:
            audience = self.expected_audience
        if audience is None:
            raise InvalidAssertion("expected actor assertion audience is required")
        audience = _require_text("expected_audience", audience, max_length=128)
        if expected_tool is None:
            raise InvalidAssertion("expected actor assertion tool is required")
        expected_tool = _require_text("expected_tool", expected_tool, max_length=128)
        if arguments is _MISSING and expected_argument_hash is None:
            raise InvalidAssertion(
                "exact actor assertion arguments or their hash are required"
            )

        claims, body, signature = self._decode(token)
        key = self.keys.get(claims.kid)
        if key is None:
            raise InvalidAssertion("unknown actor assertion key id")
        expected_signature = hmac.new(key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_signature, signature):
            raise InvalidAssertion("invalid actor assertion signature")

        current = _now_seconds(self.clock) if now is None else _finite_seconds(now)
        if claims.platform != self.expected_platform:
            raise InvalidAssertion("unexpected actor assertion platform")
        if (
            self.expected_allowlist_version is not None
            and claims.allowlist_version != self.expected_allowlist_version
        ):
            raise InvalidAssertion("actor assertion allowlist binding mismatch")
        if claims.audience != audience:
            raise InvalidAssertion("unexpected actor assertion audience")
        if claims.tool != expected_tool:
            raise InvalidAssertion("actor assertion tool binding mismatch")
        if claims.expires_at - claims.issued_at > self.lifetime:
            raise InvalidAssertion("actor assertion lifetime exceeds verifier limit")
        if claims.issued_at > current + self.clock_skew:
            raise AssertionExpired("actor assertion is not yet valid")
        # The accepted window is half-open: expiration at ``exp - skew`` is
        # denied, and the replay reservation uses ``exp + skew`` so every
        # token accepted here is covered by the atomic nonce store.
        if claims.expires_at <= current - self.clock_skew:
            raise AssertionExpired("actor assertion has expired")

        if expected_argument_hash is not None:
            if not isinstance(expected_argument_hash, str) or not _SHA256_RE.fullmatch(
                expected_argument_hash
            ):
                raise InvalidAssertion("expected_argument_hash is not SHA-256 hex")
            if not hmac.compare_digest(claims.argument_hash, expected_argument_hash):
                raise InvalidAssertion("actor assertion argument binding mismatch")
        if arguments is not _MISSING:
            computed = canonical_argument_hash(cast(JsonValue, arguments))
            if not hmac.compare_digest(claims.argument_hash, computed):
                raise InvalidAssertion("actor assertion argument binding mismatch")

        try:
            fresh = self.nonce_store.consume(
                claims.nonce,
                claims.expires_at + self.clock_skew,
                now=current,
            )
        except (TypeError, ValueError) as exc:
            raise InvalidAssertion("nonce store rejected assertion nonce") from exc
        if not fresh:
            raise ReplayError("actor assertion nonce has already been consumed")
        return claims

    def verify_bound(
        self,
        token: str,
        *,
        expected_audience: str,
        expected_tool: str,
        arguments: JsonValue,
        now: float | None = None,
    ) -> ActorClaims:
        """Verify one request with all authorization bindings supplied.

        This deliberately requires the audience, raw tool name, and exact
        argument value at the call site.  It is the preferred entry point for
        every companion tool and callback route.
        """

        return self.verify(
            token,
            expected_audience=expected_audience,
            expected_tool=expected_tool,
            arguments=arguments,
            now=now,
        )

    verify_authorized = verify_bound

    def verify_headers(
        self,
        headers: Mapping[str, Any] | Sequence[tuple[str, Any]],
        **kwargs: Any,
    ) -> ActorClaims:
        token = require_single_header(headers)
        return self.verify(token, **kwargs)

    verify_assertion = verify

    @staticmethod
    def _decode(token: str) -> tuple[ActorClaims, bytes, bytes]:
        if not isinstance(token, str) or token.count(".") != 1:
            raise InvalidAssertion("malformed actor assertion")
        body_text, signature_text = token.split(".", 1)
        body = _b64url_decode(body_text)
        signature = _b64url_decode(signature_text)
        if len(signature) != hashlib.sha256().digest_size:
            raise InvalidAssertion("invalid actor assertion signature length")
        try:
            parsed = parse_canonical_json(body)
        except CanonicalizationError as exc:
            raise InvalidAssertion(
                "actor assertion claims are not canonical JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise InvalidAssertion("actor assertion claims must be an object")
        # Aliases are accepted at the Python API boundary for ergonomic
        # construction, but the wire grammar is deliberately singular.  A
        # verifier must reject alternate claim names/encodings so two stacks
        # cannot sign different bytes for the same semantic assertion.
        unknown = set(parsed) - _ALLOWED_CLAIMS
        if unknown:
            raise InvalidAssertion("unknown actor claim: " + ", ".join(sorted(unknown)))
        missing = _REQUIRED_CLAIMS - parsed.keys()
        if missing:
            raise InvalidAssertion(
                "missing actor claims: " + ", ".join(sorted(missing))
            )
        null_optional = {
            key for key in _OPTIONAL_CLAIMS if key in parsed and parsed[key] is None
        }
        if null_optional:
            raise InvalidAssertion(
                "optional actor claims cannot be null: "
                + ", ".join(sorted(null_optional))
            )
        claims = ActorClaims.from_mapping(parsed)
        if canonical_json(claims.to_dict()) != body:
            raise InvalidAssertion(
                "actor assertion claims use a non-canonical encoding"
            )
        return claims, body, signature


ActorAssertionVerifierType = ActorAssertionVerifier
HMACActorAssertionSigner = ActorAssertionSigner
HMACActorAssertionVerifier = ActorAssertionVerifier


def sign_actor_assertion(
    key: bytes | str,
    claims: ActorClaims | Mapping[str, Any],
    *,
    kid: str = "current",
    now: float | None = None,
) -> str:
    """Functional convenience wrapper around :class:`ActorAssertionSigner`."""

    return ActorAssertionSigner(key, kid=kid).sign(claims, now=now)


def verify_actor_assertion(
    token: str,
    key: bytes | str | None = None,
    *,
    keys: Mapping[str, bytes | str] | None = None,
    expected_audience: str,
    expected_tool: str,
    arguments: JsonValue,
    nonce_store: NonceReplayStore,
    now: float | None = None,
    consume_nonce: bool = True,
) -> ActorClaims:
    """Verify one fully bound actor assertion.

    The audience, raw tool name, exact arguments, and durable replay store are
    intentionally required so this convenience API cannot silently become an
    unbound signature check.
    """

    if not consume_nonce:
        raise InvalidAssertion("actor assertion nonce consumption cannot be disabled")

    return ActorAssertionVerifier(
        key,
        keys=keys,
        expected_audience=expected_audience,
        nonce_store=nonce_store,
    ).verify_bound(
        token,
        expected_audience=expected_audience,
        expected_tool=expected_tool,
        arguments=arguments,
        now=now,
    )


verify_bound_actor_assertion = verify_actor_assertion


# ---------------------------------------------------------------------------
# Duplicate actor-header handling
# ---------------------------------------------------------------------------


def _header_values(
    headers: Mapping[str, Any] | Sequence[tuple[str, Any]],
    name: str,
) -> list[Any]:
    wanted = name.casefold()
    values: list[Any] = []
    entries: Iterable[tuple[Any, Any]]
    if isinstance(headers, Mapping):
        entries = headers.items()
    else:
        entries = headers
    for entry in entries:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise InvalidAssertion("malformed header collection")
        key, value = entry
        if isinstance(key, str) and key.casefold() == wanted:
            if isinstance(value, (list, tuple)):
                values.extend(value)
            else:
                values.append(value)
    return values


def has_duplicate_header(
    headers: Mapping[str, Any] | Sequence[tuple[str, Any]],
    name: str = ACTOR_HEADER,
) -> bool:
    """Return true when an actor header occurs more than once."""

    return len(_header_values(headers, name)) > 1


def require_single_header(
    headers: Mapping[str, Any] | Sequence[tuple[str, Any]],
    name: str = ACTOR_HEADER,
) -> str:
    """Require exactly one non-empty actor header, case-insensitively."""

    values = _header_values(headers, name)
    if not values:
        raise MissingHeaderError(f"missing {name} header")
    if len(values) != 1:
        raise DuplicateHeaderError(f"duplicate {name} headers are denied")
    value = values[0]
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise InvalidAssertion("actor header must be ASCII") from exc
    if not isinstance(value, str):
        raise InvalidAssertion("actor header must be text")
    value = value.strip()
    if not value or "," in value:
        raise DuplicateHeaderError("combined actor header values are denied")
    return value


def deny_duplicate_header(
    headers: Mapping[str, Any] | Sequence[tuple[str, Any]],
    name: str = ACTOR_HEADER,
) -> str:
    """Compatibility spelling for :func:`require_single_header`."""

    return require_single_header(headers, name)


single_actor_header = require_single_header
validate_actor_header = require_single_header
get_single_header = require_single_header
reject_duplicate_header = require_single_header


# ---------------------------------------------------------------------------
# Hash-only confirmation token lifecycle
# ---------------------------------------------------------------------------


def hash_confirmation_token(token: str | ConfirmationToken) -> str:
    """Hash an opaque confirmation token without retaining the token itself."""

    if isinstance(token, ConfirmationToken):
        token = token.value
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise ConfirmationError("confirmation token must be 256-bit base64url")
    return hashlib.sha256(token.encode("ascii", "strict")).hexdigest()


def confirmation_token_hash(token: str | ConfirmationToken) -> str:
    return hash_confirmation_token(token)


def _preview_hash(preview: str | bytes) -> str:
    if isinstance(preview, str):
        try:
            preview_bytes = preview.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise ConfirmationError("preview must be valid UTF-8") from exc
    elif isinstance(preview, bytes):
        preview_bytes = preview
    else:
        raise ConfirmationError("preview must be text or bytes")
    if len(preview_bytes) > MAX_CANONICAL_BYTES:
        raise ConfirmationError("preview exceeds the confirmation bound")
    return hashlib.sha256(preview_bytes).hexdigest()


def _binding_text(name: str, value: str | int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConfirmationError(f"{name} must be text or integer")
    result = str(value)
    if not result or len(result) > 512:
        raise ConfirmationError(f"{name} is empty or too long")
    try:
        result.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ConfirmationError(f"{name} must be valid UTF-8") from exc
    return result


def _positive_id(name: str, value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfirmationError(f"{name} must be a positive integer")
    if value > JCS_MAX_SAFE_INTEGER:
        raise ConfirmationError(f"{name} exceeds the safe integer range")
    return value


class ConfirmationToken(str):
    """The one-time token returned to trusted extension code.

    ``value`` is intentionally available only in this transient return value;
    :class:`ConfirmationRecord` stores ``token_hash`` and never this field.
    """

    token_hash: str
    issued_at: int
    expires_at: int

    def __new__(
        cls,
        value: str,
        token_hash: str,
        issued_at: int,
        expires_at: int,
    ) -> Self:
        if not _TOKEN_RE.fullmatch(value):
            raise ConfirmationError("confirmation token must be 256-bit base64url")
        instance = str.__new__(cls, value)
        instance.token_hash = token_hash
        instance.issued_at = issued_at
        instance.expires_at = expires_at
        return instance

    @property
    def value(self) -> str:
        return str(self)

    @property
    def token(self) -> str:
        """Alias for callers that name the transient value ``token``."""

        return str(self)

    @property
    def digest(self) -> str:
        return self.token_hash


@dataclass(frozen=True)
class ConfirmationRecord:
    """Hash-only state for one confirmation capability."""

    token_hash: str
    actor_user_id: int
    actor_chat_id: int
    tool: str
    argument_hash: str
    target_identity: str
    state_fingerprint: str
    preview_hash: str
    policy_version: str
    nonce: str
    issued_at: int
    expires_at: int
    state: str = "pending_bind"
    bound_chat_id: int | None = None
    bound_message_id: int | None = None
    consumed_at: int | None = None

    @property
    def status(self) -> str:
        return self.state

    @property
    def token_digest(self) -> str:
        return self.token_hash

    @property
    def preview_digest(self) -> str:
        return self.preview_hash


class ConfirmationTokenStore(Protocol):
    """Lifecycle protocol for a durable confirmation store."""

    def create(
        self,
        *,
        actor_user_id: int,
        actor_chat_id: int,
        tool: str,
        argument_hash: str,
        target_identity: str,
        state_fingerprint: str,
        preview: str | bytes,
        policy_version: str,
        now: float | None = None,
    ) -> ConfirmationToken:
        """Create a pending-bind capability."""

    def bind(
        self,
        token: str,
        *,
        chat_id: int,
        message_id: int,
        preview: str | bytes,
        now: float | None = None,
    ) -> ConfirmationRecord:
        """Bind exact rendered preview bytes to its Telegram message."""

    def consume(
        self,
        token: str,
        *,
        actor_user_id: int,
        actor_chat_id: int,
        tool: str,
        argument_hash: str,
        target_identity: str,
        state_fingerprint: str,
        policy_version: str,
        chat_id: int,
        message_id: int,
        now: float | None = None,
    ) -> ConfirmationRecord:
        """Atomically consume a bound capability after revalidation."""


class InMemoryConfirmationTokenStore:
    """Thread-safe hash-only confirmation lifecycle for tests.

    The implementation intentionally stores only a SHA-256 token hash.  The
    plaintext token returned by ``create`` is not recoverable from the store,
    including through ``records`` or ``get``.
    """

    def __init__(
        self,
        *,
        ttl: int = CONFIRMATION_TOKEN_TTL_SECONDS,
        policy_version: str = "1",
        clock: Any = time.time,
    ) -> None:
        if not isinstance(ttl, int) or isinstance(ttl, bool):
            raise TypeError("ttl must be an integer")
        if ttl <= 0 or ttl > CONFIRMATION_TOKEN_TTL_SECONDS:
            raise ValueError("ttl must be between 1 and 300 seconds")
        self.ttl = ttl
        self.policy_version = _binding_text("policy_version", policy_version)
        self.clock = clock
        self._records: dict[str, ConfirmationRecord] = {}
        self._lock = threading.Lock()

    def _current(self, now: float | None) -> int:
        return _protocol_second(_now_seconds(self.clock) if now is None else now)

    def create(
        self,
        *,
        actor_user_id: int,
        actor_chat_id: int,
        tool: str,
        argument_hash: str,
        target_identity: str,
        state_fingerprint: str,
        preview: str | bytes,
        policy_version: str | None = None,
        now: float | None = None,
        nonce: str | None = None,
    ) -> ConfirmationToken:
        actor_user_id = _positive_id("actor_user_id", actor_user_id)
        actor_chat_id = _positive_id("actor_chat_id", actor_chat_id)
        tool = _binding_text("tool", tool)
        if not _SHA256_RE.fullmatch(argument_hash):
            raise ConfirmationError("argument_hash must be a lowercase SHA-256 digest")
        target_identity = _binding_text("target_identity", target_identity)
        state_fingerprint = _binding_text("state_fingerprint", state_fingerprint)
        version = (
            self.policy_version
            if policy_version is None
            else _binding_text("policy_version", policy_version)
        )
        if version != self.policy_version:
            raise ConfirmationBindingError("confirmation policy version is not current")
        record_nonce = (
            _binding_text("nonce", nonce)
            if nonce is not None
            else _b64url_encode(secrets.token_bytes(16))
        )
        issued_at = self._current(now)
        expires_at = issued_at + self.ttl
        value = _b64url_encode(secrets.token_bytes(CONFIRMATION_TOKEN_BYTES))
        # token_bytes(32) always encodes to 43 unpadded base64url characters.
        token_hash = hash_confirmation_token(value)
        record = ConfirmationRecord(
            token_hash=token_hash,
            actor_user_id=actor_user_id,
            actor_chat_id=actor_chat_id,
            tool=tool,
            argument_hash=argument_hash,
            target_identity=target_identity,
            state_fingerprint=state_fingerprint,
            preview_hash=_preview_hash(preview),
            policy_version=version,
            nonce=record_nonce,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        with self._lock:
            if version != self.policy_version:
                raise ConfirmationBindingError(
                    "confirmation policy version is not current"
                )
            self._records[token_hash] = record
        return ConfirmationToken(value, token_hash, issued_at, expires_at)

    issue = create
    mint = create

    def _record_for(self, token: str) -> ConfirmationRecord:
        token_hash = hash_confirmation_token(token)
        record = self._records.get(token_hash)
        if record is None:
            raise ConfirmationError("unknown confirmation token")
        return record

    def _expire_if_needed(
        self, token_hash: str, record: ConfirmationRecord, now: int
    ) -> None:
        if record.expires_at <= now and record.state not in {
            "consumed",
            "revoked",
            "expired",
        }:
            self._records[token_hash] = replace(record, state="expired")
            raise ConfirmationExpired("confirmation token has expired")

    def bind(
        self,
        token: str | ConfirmationToken,
        *,
        chat_id: int,
        message_id: int,
        preview: str | bytes,
        now: float | None = None,
    ) -> ConfirmationRecord:
        token_value = token.value if isinstance(token, ConfirmationToken) else token
        chat_id = _positive_id("chat_id", chat_id)
        message_id = _positive_id("message_id", message_id)
        current = self._current(now)
        token_hash = hash_confirmation_token(token_value)
        with self._lock:
            record = self._records.get(token_hash)
            if record is None:
                raise ConfirmationError("unknown confirmation token")
            self._expire_if_needed(token_hash, record, current)
            if record.state == "consumed":
                raise ConfirmationReplayError("confirmation token was already consumed")
            if record.state == "armed":
                if (
                    record.bound_chat_id == chat_id
                    and record.bound_message_id == message_id
                    and hmac.compare_digest(record.preview_hash, _preview_hash(preview))
                ):
                    # A transport retry of the same bind is harmless and
                    # returns the already armed record.  Any changed message
                    # or preview remains a binding failure below.
                    return record
                raise ConfirmationBindingError("confirmation token is already bound")
            if record.state != "pending_bind":
                raise ConfirmationBindingError(
                    "confirmation token is not awaiting bind"
                )
            if chat_id != record.actor_chat_id:
                raise ConfirmationBindingError(
                    "confirmation chat does not match actor chat"
                )
            if not hmac.compare_digest(record.preview_hash, _preview_hash(preview)):
                raise ConfirmationBindingError(
                    "preview text does not match exact server preview"
                )
            bound = replace(
                record,
                state="armed",
                bound_chat_id=chat_id,
                bound_message_id=message_id,
            )
            self._records[token_hash] = bound
            return bound

    bind_message = bind

    def consume(
        self,
        token: str | ConfirmationToken,
        *,
        actor_user_id: int,
        actor_chat_id: int,
        tool: str,
        argument_hash: str,
        target_identity: str,
        state_fingerprint: str,
        policy_version: str,
        chat_id: int,
        message_id: int,
        now: float | None = None,
    ) -> ConfirmationRecord:
        token_value = token.value if isinstance(token, ConfirmationToken) else token
        actor_user_id = _positive_id("actor_user_id", actor_user_id)
        actor_chat_id = _positive_id("actor_chat_id", actor_chat_id)
        tool = _binding_text("tool", tool)
        if not _SHA256_RE.fullmatch(argument_hash):
            raise ConfirmationBindingError(
                "argument_hash must be a lowercase SHA-256 digest"
            )
        target_identity = _binding_text("target_identity", target_identity)
        state_fingerprint = _binding_text("state_fingerprint", state_fingerprint)
        policy_version = _binding_text("policy_version", policy_version)
        chat_id = _positive_id("chat_id", chat_id)
        message_id = _positive_id("message_id", message_id)
        current = self._current(now)
        token_hash = hash_confirmation_token(token_value)
        with self._lock:
            record = self._records.get(token_hash)
            if record is None:
                raise ConfirmationError("unknown confirmation token")
            if record.state == "consumed":
                raise ConfirmationReplayError("confirmation token was already consumed")
            self._expire_if_needed(token_hash, record, current)
            if record.state != "armed":
                raise ConfirmationBindingError("confirmation token is not armed")
            if (
                actor_user_id != record.actor_user_id
                or actor_chat_id != record.actor_chat_id
                or tool != record.tool
                or not hmac.compare_digest(argument_hash, record.argument_hash)
                or target_identity != record.target_identity
                or state_fingerprint != record.state_fingerprint
                or policy_version != record.policy_version
            ):
                raise ConfirmationBindingError("confirmation binding changed")
            if chat_id != record.bound_chat_id:
                raise ConfirmationBindingError("confirmation message chat changed")
            if message_id != record.bound_message_id:
                raise ConfirmationBindingError("confirmation message changed")
            consumed = replace(record, state="consumed", consumed_at=current)
            self._records[token_hash] = consumed
            return consumed

    consume_token = consume

    def get(self, token: str | ConfirmationToken) -> ConfirmationRecord:
        token_value = token.value if isinstance(token, ConfirmationToken) else token
        with self._lock:
            return self._record_for(token_value)

    def revoke(self, token: str | ConfirmationToken) -> None:
        token_value = token.value if isinstance(token, ConfirmationToken) else token
        token_hash = hash_confirmation_token(token_value)
        with self._lock:
            record = self._records.get(token_hash)
            if record is not None and record.state not in {"consumed", "expired"}:
                self._records[token_hash] = replace(record, state="revoked")

    def revoke_policy(self, policy_version: str) -> int:
        """Invalidate outstanding records from older policy versions."""

        version = _binding_text("policy_version", policy_version)
        changed = 0
        with self._lock:
            self.policy_version = version
            for token_hash, record in tuple(self._records.items()):
                if (
                    record.state in {"pending_bind", "armed"}
                    and record.policy_version != version
                ):
                    self._records[token_hash] = replace(record, state="revoked")
                    changed += 1
        return changed

    rotate_policy = revoke_policy

    def cleanup(self, *, now: float | None = None) -> int:
        current = self._current(now)
        removed = 0
        with self._lock:
            for token_hash, record in tuple(self._records.items()):
                if record.expires_at <= current or record.state in {
                    "expired",
                    "revoked",
                    "consumed",
                }:
                    del self._records[token_hash]
                    removed += 1
        return removed

    @property
    def records(self) -> tuple[ConfirmationRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


ConfirmationStore = InMemoryConfirmationTokenStore
ConfirmationTokenManager = InMemoryConfirmationTokenStore
ConfirmationStoreProtocol = ConfirmationTokenStore


def confirmation_callback_data(token: str | ConfirmationToken) -> str:
    """Encode only the opaque token in Telegram callback data."""

    value = token.value if isinstance(token, ConfirmationToken) else token
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise ConfirmationError("invalid confirmation token")
    result = CONFIRMATION_CALLBACK_PREFIX + value
    if len(result.encode("ascii")) > 64:
        raise ConfirmationError("callback data exceeds Telegram's 64-byte limit")
    return result


def parse_confirmation_callback_data(value: str) -> str:
    if not isinstance(value, str) or not value.startswith(CONFIRMATION_CALLBACK_PREFIX):
        raise ConfirmationError("invalid confirmation callback prefix")
    token = value[len(CONFIRMATION_CALLBACK_PREFIX) :]
    if not _TOKEN_RE.fullmatch(token):
        raise ConfirmationError("invalid confirmation callback token")
    return token


make_confirmation_callback_data = confirmation_callback_data
parse_callback_data = parse_confirmation_callback_data


def issue_confirmation_token(
    store: InMemoryConfirmationTokenStore,
    **kwargs: Any,
) -> ConfirmationToken:
    return store.create(**kwargs)


def bind_confirmation_token(
    store: InMemoryConfirmationTokenStore,
    token: str | ConfirmationToken,
    **kwargs: Any,
) -> ConfirmationRecord:
    return store.bind(token, **kwargs)


def consume_confirmation_token(
    store: InMemoryConfirmationTokenStore,
    token: str | ConfirmationToken,
    **kwargs: Any,
) -> ConfirmationRecord:
    return store.consume(token, **kwargs)


__all__ = [
    "ACTOR_ASSERTION_CLOCK_SKEW_SECONDS",
    "ACTOR_ASSERTION_LIFETIME_SECONDS",
    "ACTOR_ASSERTION_VERSION",
    "ACTOR_HEADER",
    "CONFIRMATION_CALLBACK_PREFIX",
    "CONFIRMATION_TOKEN_BYTES",
    "CONFIRMATION_TOKEN_TTL_SECONDS",
    "JCS_MAX_SAFE_INTEGER",
    "MAX_CANONICAL_BYTES",
    "MAX_CANONICAL_CONTAINER_ITEMS",
    "MAX_CANONICAL_DEPTH",
    "MAX_CANONICAL_STRING_BYTES",
    "ActorAssertion",
    "ActorAssertionClaims",
    "ActorAssertionSigner",
    "ActorAssertionVerifier",
    "ActorClaims",
    "AssertionExpired",
    "AuthError",
    "CanonicalizationError",
    "ConfirmationBindingError",
    "ConfirmationCallbackPrefix",
    "ConfirmationError",
    "ConfirmationExpired",
    "ConfirmationRecord",
    "ConfirmationReplayError",
    "ConfirmationStore",
    "ConfirmationStoreProtocol",
    "ConfirmationToken",
    "ConfirmationTokenManager",
    "ConfirmationTokenStore",
    "DuplicateHeaderError",
    "DuplicateJsonKeyError",
    "ExpiredAssertion",
    "HMACActorAssertionSigner",
    "HMACActorAssertionVerifier",
    "InMemoryConfirmationTokenStore",
    "InMemoryNonceReplayStore",
    "InvalidAssertion",
    "MissingHeaderError",
    "NonceReplayStore",
    "NonceReplayStoreProtocol",
    "NonceStore",
    "ReplayError",
    "argument_hash",
    "bind_confirmation_token",
    "canonical_argument_hash",
    "canonical_hash",
    "canonical_json",
    "canonical_json_bytes",
    "canonical_json_hash",
    "canonical_json_text",
    "canonicalize_json",
    "confirmation_callback_data",
    "confirmation_token_hash",
    "consume_confirmation_token",
    "deny_duplicate_header",
    "get_single_header",
    "has_duplicate_header",
    "hash_confirmation_token",
    "issue_confirmation_token",
    "make_confirmation_callback_data",
    "parse_callback_data",
    "parse_canonical_json",
    "parse_confirmation_callback_data",
    "parse_json",
    "reject_duplicate_header",
    "require_single_header",
    "sha256_json",
    "sign_actor_assertion",
    "single_actor_header",
    "validate_actor_header",
    "verify_actor_assertion",
    "verify_bound_actor_assertion",
]
