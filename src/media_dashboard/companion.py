"""Typed, authenticated client for the private companion dashboard API.

The dashboard deliberately has no knowledge of the companion's SQLite file,
Hermes files, provider APIs, or MCP transport.  This module is the only
network boundary used by :mod:`media_dashboard.app`.  It exposes a closed set
of operation names and signs every request with the dashboard API key.

The wire contract is intentionally small.  A request signature covers the
HTTP method, exact path, operation name, actor, timestamp, nonce, and SHA-256
of the canonical JSON body.  The companion can therefore reject a copied
request, a changed body, an expired timestamp, or an operation outside the
reviewed dashboard surface before doing any work.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import threading
import time
from typing import Final, TypeAlias, cast
from urllib.parse import urlsplit, urlunsplit

import requests

from media_companion.auth import CanonicalizationError, canonical_json
from media_companion.redaction import redact_json, redact_text


# A dashboard operation is deliberately not an MCP tool name.  In particular,
# no generic proxy or provider operation appears in this set.
READ_OPERATIONS: Final[frozenset[str]] = frozenset(
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
MUTATION_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "users.add",
        "users.remove",
        "delivery.retry_once",
        "delivery.mark_abandoned",
        "delivery.assume_sent",
        "delivery.resend_once",
    }
)
ALLOWED_OPERATIONS: Final[frozenset[str]] = READ_OPERATIONS | MUTATION_OPERATIONS
READ_ONLY_OPERATIONS: Final[frozenset[str]] = READ_OPERATIONS
DASHBOARD_READ_OPERATIONS: Final[frozenset[str]] = READ_OPERATIONS
DASHBOARD_MUTATION_OPERATIONS: Final[frozenset[str]] = MUTATION_OPERATIONS
DASHBOARD_OPERATIONS: Final[frozenset[str]] = ALLOWED_OPERATIONS
DASHBOARD_OPERATION_NAMES: Final[tuple[str, ...]] = (
    "health",
    "users",
    "users.resolve",
    "blocked",
    "subscriptions",
    "deliveries",
    "quarantine",
    "oracle",
    "audit",
    "users.add",
    "users.remove",
    "delivery.retry_once",
    "delivery.mark_abandoned",
    "delivery.assume_sent",
    "delivery.resend_once",
)
if (
    len(DASHBOARD_OPERATION_NAMES) != len(set(DASHBOARD_OPERATION_NAMES))
    or frozenset(DASHBOARD_OPERATION_NAMES) != ALLOWED_OPERATIONS
):
    raise RuntimeError("dashboard operation inventory drifted")
RECOVERY_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "delivery.retry_once",
        "delivery.mark_abandoned",
        "delivery.assume_sent",
        "delivery.resend_once",
    }
)

# Friendly constants make the reviewed inventory obvious to deployment and
# contract-test code without making callers duplicate strings.
OP_HEALTH: Final[str] = "health"
OP_USERS: Final[str] = "users"
OP_USERS_RESOLVE: Final[str] = "users.resolve"
OP_BLOCKED: Final[str] = "blocked"
OP_SUBSCRIPTIONS: Final[str] = "subscriptions"
OP_DELIVERIES: Final[str] = "deliveries"
OP_QUARANTINE: Final[str] = "quarantine"
OP_ORACLE: Final[str] = "oracle"
OP_AUDIT: Final[str] = "audit"
OP_USERS_ADD: Final[str] = "users.add"
OP_USERS_REMOVE: Final[str] = "users.remove"
OP_RETRY_ONCE: Final[str] = "delivery.retry_once"
OP_MARK_ABANDONED: Final[str] = "delivery.mark_abandoned"
OP_ASSUME_SENT: Final[str] = "delivery.assume_sent"
OP_RESEND_ONCE: Final[str] = "delivery.resend_once"

COMPANION_API_PREFIX: Final[str] = "/private/dashboard"
SIGNATURE_VERSION: Final[str] = "dashboard-v1"
MAX_REQUEST_BODY_BYTES: Final[int] = 64 * 1024
MAX_RESPONSE_BODY_BYTES: Final[int] = 256 * 1024
MAX_RESPONSE_DEPTH: Final[int] = 16
MAX_RESPONSE_ITEMS: Final[int] = 10_000
MAX_RESPONSE_STRING_BYTES: Final[int] = 64 * 1024
REQUEST_CLOCK_SKEW_SECONDS: Final[int] = 30
REQUEST_LIFETIME_SECONDS: Final[int] = 60
NONCE_BYTES: Final[int] = 32
MAX_CONCURRENT_CALLS: Final[int] = 8
DEFAULT_TOTAL_TIMEOUT_SECONDS: Final[float] = 15.0
BREAKER_FAILURE_THRESHOLD: Final[int] = 5
BREAKER_OPEN_SECONDS: Final[float] = 30.0
COMPANION_SERVICE_HOST: Final[str] = "media-companion"
COMPANION_SERVICE_PORT: Final[int] = 18080
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_IDEMPOTENCY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9:_-]{1,256}$")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"^(0|[1-9][0-9]*)$")
_PRIVATE_SERVICE_NETWORKS: Final[tuple[ipaddress._BaseNetwork, ...]] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)

# Typed dashboard views use a shared safe vocabulary.  Unknown fields are
# discarded before a result can reach the browser; recursive redaction is a
# second line of defence, not the schema boundary.  The set is deliberately
# limited to aggregate/operational fields and contains no provider payload,
# filesystem, credential, or MCP escape hatch.
RESPONSE_FIELD_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "confirmation_required",
        "preview",
        "operation",
        "tool",
        "status",
        "state",
        "healthy",
        "ready",
        "complete",
        "fresh",
        "as_of",
        "updated_at",
        "expires_at",
        "expires_at",
        "expires_at",
        "created_at",
        "first_seen_at",
        "last_seen_at",
        "first_seen",
        "last_seen",
        "added_at",
        "revoked_at",
        "count",
        "total",
        "attempts",
        "attempt_count",
        "active_subscriptions",
        "next_cursor",
        "truncated",
        "items",
        "users",
        "blocked",
        "subscriptions",
        "deliveries",
        "quarantine",
        "audit",
        "data",
        "result",
        "details",
        "dependencies",
        "services",
        "worker",
        "webhook",
        "reconciliation",
        "activation",
        "migration",
        "oracle",
        "partial_errors",
        "errors",
        "message",
        "residual",
        "accounted",
        "unaccounted",
        "id",
        "user_id",
        "chat_id",
        "delivery_id",
        "subscription_id",
        "request_id",
        "record_id",
        "version",
        "generation",
        "role",
        "access",
        "source",
        "name",
        "display_name",
        "username",
        "chat_type",
        "reason",
        "reason_code",
        "outcome",
        "fingerprint",
        "action",
        "policy_version",
        "state_fingerprint",
        "policy_version",
        "target",
        "target_identity",
        "media_type",
        "provider_id",
        "season_number",
        "episode_number",
        "title",
        "year",
        "mode",
        "notification_class",
        "destination_state",
        "possible_duplicate",
        "resend_generation",
        "confirmation_capability",
        "preview_digest",
        "token_hash",
        "candidate",
        "configured_role",
        "is_admin",
        "current",
    }
)

_RESPONSE_TAINT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "raw",
        "raw_payload",
        "payload",
        "provider_response",
        "webhook_payload",
        "message_text",
        "session",
        "document",
        "environment",
        "env",
        "sql",
        "mcp",
        "error",
        "error_text",
        "exception",
        "traceback",
        "raw_error",
        "secret",
        "token",
        "api_key",
        "bot_token",
        "credential",
        "password",
        "path",
    }
)

_COMMON_RESPONSE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "status",
        "state",
        "healthy",
        "ready",
        "complete",
        "fresh",
        "as_of",
        "updated_at",
        "created_at",
        "first_seen_at",
        "last_seen_at",
        "first_seen",
        "last_seen",
        "added_at",
        "revoked_at",
        "count",
        "total",
        "next_cursor",
        "truncated",
        "version",
        "generation",
        "source",
        "reason",
        "reason_code",
        "outcome",
        "fingerprint",
        "state_fingerprint",
        "id",
        "request_id",
        "record_id",
        "result",
        "details",
        "partial_errors",
        "errors",
        "message",
    }
)

OPERATION_RESPONSE_FIELDS: Final[dict[str, frozenset[str]]] = {
    OP_HEALTH: _COMMON_RESPONSE_FIELDS
    | frozenset(
        {
            "dependencies",
            "services",
            "worker",
            "webhook",
            "reconciliation",
            "activation",
            "migration",
            "oracle",
        }
    ),
    OP_USERS: _COMMON_RESPONSE_FIELDS | frozenset({"users", "items"}),
    OP_USERS_RESOLVE: _COMMON_RESPONSE_FIELDS
    | frozenset(
        {
            "candidate",
            "chat_id",
            "user_id",
            "username",
            "display_name",
            "chat_type",
            "role",
            "access",
            "configured_role",
            "is_admin",
            "current",
        }
    ),
    OP_BLOCKED: _COMMON_RESPONSE_FIELDS | frozenset({"blocked", "items"}),
    OP_SUBSCRIPTIONS: _COMMON_RESPONSE_FIELDS
    | frozenset({"subscriptions", "items", "active_subscriptions"}),
    OP_DELIVERIES: _COMMON_RESPONSE_FIELDS
    | frozenset(
        {
            "deliveries",
            "items",
            "delivery_id",
            "attempts",
            "attempt_count",
            "destination_state",
            "media_type",
            "notification_class",
            "possible_duplicate",
            "resend_generation",
        }
    ),
    OP_QUARANTINE: _COMMON_RESPONSE_FIELDS | frozenset({"quarantine", "items"}),
    OP_ORACLE: _COMMON_RESPONSE_FIELDS
    | frozenset({"oracle", "residual", "accounted", "unaccounted"}),
    OP_AUDIT: _COMMON_RESPONSE_FIELDS
    | frozenset({"audit", "items", "action", "policy_version"}),
}
_MUTATION_RESPONSE_FIELDS: Final[frozenset[str]] = _COMMON_RESPONSE_FIELDS | frozenset(
    {
        "operation",
        "confirmation_required",
        "confirmation_capability",
        "preview",
        "preview_digest",
        "expires_at",
        "changed",
        "idempotency_key",
        "user_id",
        "fingerprint",
        "delivery_id",
        "state_fingerprint",
        "policy_version",
        "target",
        "target_identity",
        "token_hash",
    }
)
for _operation_name in MUTATION_OPERATIONS:
    OPERATION_RESPONSE_FIELDS[_operation_name] = _MUTATION_RESPONSE_FIELDS
RESPONSE_ENVELOPE_FIELDS: Final[frozenset[str]] = frozenset({"ok", "operation", "data"})

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


class CompanionClientError(RuntimeError):
    """Base class for safe companion-client failures."""


class CompanionConfigurationError(CompanionClientError, ValueError):
    """The client endpoint or key is not configured safely."""


class CompanionUnavailable(CompanionClientError):
    """The private companion could not be reached or timed out."""


class CompanionProtocolError(CompanionClientError):
    """The companion returned an invalid, oversized, or unsafe response."""


class OperationNotAllowed(CompanionClientError, ValueError):
    """An operation is not in the frozen dashboard allowlist."""


class CompanionRejected(CompanionClientError):
    """The companion rejected a typed operation."""


class DashboardOperation(str, Enum):
    """Closed operation enum used by typed callers."""

    HEALTH = OP_HEALTH
    USERS = OP_USERS
    USERS_RESOLVE = OP_USERS_RESOLVE
    BLOCKED = OP_BLOCKED
    SUBSCRIPTIONS = OP_SUBSCRIPTIONS
    DELIVERIES = OP_DELIVERIES
    QUARANTINE = OP_QUARANTINE
    ORACLE = OP_ORACLE
    AUDIT = OP_AUDIT
    USERS_ADD = OP_USERS_ADD
    USERS_REMOVE = OP_USERS_REMOVE
    RETRY_ONCE = OP_RETRY_ONCE
    MARK_ABANDONED = OP_MARK_ABANDONED
    ASSUME_SENT = OP_ASSUME_SENT
    RESEND_ONCE = OP_RESEND_ONCE


@dataclass(frozen=True, slots=True)
class CompanionResponse:
    """A validated operation result with no transport implementation details."""

    operation: str
    data: dict[str, JsonValue]
    status: int = 200


def _canonical_json(value: object) -> bytes:
    """Encode a bounded JSON object deterministically for signing."""

    try:
        encoded = canonical_json(cast(object, value), max_bytes=MAX_REQUEST_BODY_BYTES)
    except (CanonicalizationError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CompanionConfigurationError(
            "dashboard request is not valid JSON"
        ) from exc
    if len(encoded) > MAX_REQUEST_BODY_BYTES:
        raise CompanionConfigurationError("dashboard request is too large")
    return encoded


def operation_allowlist_fingerprint(
    operations: frozenset[str] | set[str] | tuple[str, ...] = ALLOWED_OPERATIONS,
) -> str:
    """Return the checked-in operation contract fingerprint."""

    if not isinstance(operations, (frozenset, set, tuple)) or any(
        not isinstance(item, str) for item in operations
    ):
        raise CompanionConfigurationError("dashboard operation allowlist is invalid")
    canonical = "\n".join(sorted(operations)).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_operation_allowlist(operations: object) -> None:
    """Fail closed when a companion operation inventory drifts."""

    if not isinstance(operations, (frozenset, set, tuple)):
        raise CompanionConfigurationError("dashboard operation allowlist is invalid")
    if any(not isinstance(item, str) for item in operations):
        raise CompanionConfigurationError("dashboard operation allowlist is invalid")
    if len(operations) != len(set(operations)):
        raise CompanionConfigurationError("dashboard operation allowlist is invalid")
    try:
        candidate = frozenset(operations)
    except TypeError as exc:
        raise CompanionConfigurationError(
            "dashboard operation allowlist is invalid"
        ) from exc
    if candidate != ALLOWED_OPERATIONS:
        raise CompanionConfigurationError("dashboard operation allowlist drifted")


def _json_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _secret_bytes(value: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(value, str):
        try:
            result = value.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise CompanionConfigurationError("dashboard API key is invalid") from exc
    elif isinstance(value, (bytes, bytearray, memoryview)):
        result = bytes(value)
    else:  # pragma: no cover - typing protects this branch
        raise CompanionConfigurationError("dashboard API key is invalid")
    if not 32 <= len(result) <= 4096:
        raise CompanionConfigurationError("dashboard API key is invalid")
    return result


def load_api_key_file(path: str | os.PathLike[str]) -> bytes:
    """Read exactly one dashboard API key file.

    The caller supplies a canonical mounted secret path.  No environment file,
    log, database, or generic directory is ever read here.  Errors intentionally
    omit the path so a misconfiguration cannot disclose host layout.
    """

    try:
        key_path = Path(path)
        if not key_path.is_absolute() or key_path != Path(os.path.normpath(key_path)):
            raise CompanionConfigurationError("dashboard API key is unavailable")
        flags = os.O_RDONLY | os.O_CLOEXEC
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(key_path, flags | nofollow)
        try:
            stat_result = os.fstat(fd)
            if (
                not stat.S_ISREG(stat_result.st_mode)
                or stat_result.st_mode & 0o077
                or stat_result.st_size > 4096
            ):
                raise CompanionConfigurationError("dashboard API key is unavailable")
            raw = os.read(fd, 4097)
            if len(raw) > 4096 or os.read(fd, 1):
                raise CompanionConfigurationError("dashboard API key is unavailable")
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError) as exc:
        raise CompanionConfigurationError("dashboard API key is unavailable") from exc
    # Permit a single trailing newline, as is conventional for mounted secrets,
    # but reject all other surrounding text.  Never accept a weak key by hashing
    # it into one: operators must provide at least 256 bits of key material.
    if raw.endswith(b"\n"):
        raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
    if b"\r" in raw or b"\n" in raw:
        raise CompanionConfigurationError("dashboard API key is unavailable")
    return _secret_bytes(raw)


def _validate_base_url(
    value: str,
    *,
    allowed_hosts: frozenset[str] = frozenset({COMPANION_SERVICE_HOST}),
    allowed_port: int = COMPANION_SERVICE_PORT,
) -> str:
    """Validate the one private companion origin.

    This is intentionally an origin policy, not a generic URL parser.  The
    dashboard never follows a configured URL to an arbitrary LAN/public host,
    metadata endpoint, or userinfo-bearing redirect.  A deployment that uses a
    different private DNS name must add that exact name to the process image's
    policy; callers cannot opt into arbitrary hosts at request time.
    """
    if not isinstance(value, str):
        raise CompanionConfigurationError("companion URL is invalid")
    if (
        allowed_hosts != frozenset({COMPANION_SERVICE_HOST})
        or allowed_port != COMPANION_SERVICE_PORT
    ):
        raise CompanionConfigurationError("companion URL policy is immutable")
    text = value
    if (
        not text
        or text != text.strip()
        or any(ord(char) < 0x20 or char.isspace() for char in text)
    ):
        raise CompanionConfigurationError("companion URL is invalid")
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise CompanionConfigurationError("companion URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CompanionConfigurationError("companion URL is invalid")
    normalized_host = hostname.lower()
    if normalized_host not in allowed_hosts:
        raise CompanionConfigurationError(
            "companion URL is not an allowed service origin"
        )
    if port not in {None, allowed_port}:
        raise CompanionConfigurationError("companion URL has an invalid service port")
    return urlunsplit(("http", f"{normalized_host}:{allowed_port}", "", "", ""))


def _resolve_private_service(
    hostname: str,
    port: int = COMPANION_SERVICE_PORT,
    *,
    resolver: Callable[..., object] = socket.getaddrinfo,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, ...]:
    """Resolve and pin an exact private service name, rejecting rebinding."""

    if deadline is not None and clock() >= deadline:
        raise CompanionUnavailable("companion deadline exceeded")
    try:
        try:
            records = resolver(hostname, port, type=socket.SOCK_STREAM)
        except TypeError:
            # A tiny injected resolver used by tests/policy wrappers may only
            # accept the host and port.  The production resolver above remains
            # explicit about stream sockets.
            records = resolver(hostname, port)
    except (OSError, socket.gaierror, TypeError, ValueError) as exc:
        raise CompanionUnavailable("companion service cannot be resolved") from exc
    if deadline is not None and clock() >= deadline:
        raise CompanionUnavailable("companion deadline exceeded")
    if not isinstance(records, Iterable):
        raise CompanionUnavailable("companion service resolution is invalid")
    addresses: set[str] = set()
    try:
        for record in records:
            sockaddr = (
                record[4] if isinstance(record, tuple) and len(record) >= 5 else record
            )
            raw_address = sockaddr[0] if isinstance(sockaddr, tuple) else sockaddr
            address = ipaddress.ip_address(str(raw_address))
            if deadline is not None and clock() >= deadline:
                raise CompanionUnavailable("companion deadline exceeded")
            mapped = getattr(address, "ipv4_mapped", None)
            policy_address = mapped if mapped is not None else address
            if (
                not any(
                    policy_address in network for network in _PRIVATE_SERVICE_NETWORKS
                )
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or address.is_reserved
            ):
                raise CompanionConfigurationError(
                    "companion resolved outside private service policy"
                )
            addresses.add(str(address))
    except CompanionConfigurationError:
        raise
    except (IndexError, OSError, TypeError, ValueError) as exc:
        raise CompanionUnavailable("companion service resolution is invalid") from exc
    if not addresses:
        raise CompanionUnavailable("companion service cannot be resolved")
    return tuple(sorted(addresses))


def _bounded_actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor or len(actor.encode("utf-8")) > 128:
        raise CompanionConfigurationError("dashboard actor is invalid")
    if any(ord(char) < 0x20 or char.isspace() for char in actor):
        raise CompanionConfigurationError("dashboard actor is invalid")
    return actor


def _nonce() -> str:
    return (
        base64.urlsafe_b64encode(secrets.token_bytes(NONCE_BYTES))
        .rstrip(b"=")
        .decode("ascii")
    )


def signature_message(
    *,
    method: str,
    path: str,
    operation: str,
    actor: str,
    body_digest: str,
    timestamp: int,
    nonce: str,
    expires_at: int | None = None,
    session_digest: str | None = None,
    audit_context: str | None = None,
) -> bytes:
    """Return the exact bytes covered by a dashboard request signature."""

    if not isinstance(method, str) or method not in {"GET", "POST"}:
        raise CompanionConfigurationError("dashboard request method is invalid")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or any(char in path for char in "?#\r\n")
    ):
        raise CompanionConfigurationError("dashboard request path is invalid")
    if not isinstance(operation, str) or operation not in ALLOWED_OPERATIONS:
        raise OperationNotAllowed("dashboard operation is not allowed")
    _bounded_actor(actor)
    if not isinstance(body_digest, str) or not _SHA256_RE.fullmatch(body_digest):
        raise CompanionConfigurationError("dashboard body digest is invalid")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp <= 0:
        raise CompanionConfigurationError("dashboard timestamp is invalid")
    actual_expires = (
        timestamp + REQUEST_LIFETIME_SECONDS if expires_at is None else expires_at
    )
    if (
        not isinstance(actual_expires, int)
        or isinstance(actual_expires, bool)
        or actual_expires < timestamp
        or actual_expires - timestamp > REQUEST_LIFETIME_SECONDS
    ):
        raise CompanionConfigurationError("dashboard expiry is invalid")
    if not isinstance(nonce, str) or not _TOKEN_RE.fullmatch(nonce):
        raise CompanionConfigurationError("dashboard nonce is invalid")
    fields = [
        SIGNATURE_VERSION,
        method,
        path,
        operation,
        actor,
        body_digest,
        str(timestamp),
        str(actual_expires),
        nonce,
    ]
    if session_digest is not None:
        if not isinstance(session_digest, str) or not _SHA256_RE.fullmatch(
            session_digest
        ):
            raise CompanionConfigurationError("dashboard session digest is invalid")
        fields.append(session_digest)
    if audit_context is not None:
        fields.append(_bounded_audit_context(audit_context))
    return "\n".join(fields).encode("utf-8")


def sign_request(
    key: bytes | bytearray | memoryview | str,
    *,
    method: str,
    path: str,
    operation: str,
    actor: str,
    body: bytes,
    timestamp: int | None = None,
    nonce: str | None = None,
    expires_at: int | None = None,
    session_digest: str | None = None,
    audit_context: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Sign a bounded private request and return its signature plus headers."""

    if not isinstance(body, bytes) or len(body) > MAX_REQUEST_BODY_BYTES:
        raise CompanionConfigurationError("dashboard request body is invalid")
    actual_timestamp = int(time.time()) if timestamp is None else timestamp
    if (
        isinstance(actual_timestamp, bool)
        or not isinstance(actual_timestamp, int)
        or actual_timestamp <= 0
    ):
        raise CompanionConfigurationError("dashboard timestamp is invalid")
    actual_nonce = _nonce() if nonce is None else nonce
    actual_expires = (
        actual_timestamp + REQUEST_LIFETIME_SECONDS
        if expires_at is None
        else expires_at
    )
    body_digest = hashlib.sha256(body).hexdigest()
    message = signature_message(
        method=method,
        path=path,
        operation=operation,
        actor=actor,
        body_digest=body_digest,
        timestamp=actual_timestamp,
        nonce=actual_nonce,
        expires_at=actual_expires,
        session_digest=session_digest,
        audit_context=audit_context,
    )
    signature = hmac.new(_secret_bytes(key), message, hashlib.sha256).hexdigest()
    headers = {
        "X-CRBL-Dashboard-Version": SIGNATURE_VERSION,
        "X-CRBL-Dashboard-Operation": operation,
        "X-CRBL-Dashboard-Actor": actor,
        "X-CRBL-Dashboard-Timestamp": str(actual_timestamp),
        "X-CRBL-Dashboard-Expires": str(actual_expires),
        "X-CRBL-Dashboard-Nonce": actual_nonce,
        "X-CRBL-Dashboard-Body-SHA256": body_digest,
        "X-CRBL-Dashboard-Signature": signature,
    }
    if session_digest is not None:
        headers["X-CRBL-Dashboard-Session-Digest"] = session_digest
    if audit_context is not None:
        headers["X-CRBL-Dashboard-Audit"] = _bounded_audit_context(audit_context)
    return signature, headers


def _sanitize_response(
    value: object,
    *,
    depth: int = 0,
    count: list[int] | None = None,
    allowed_keys: frozenset[str] = RESPONSE_FIELD_ALLOWLIST,
    strict: bool = False,
    strict_top_level: bool = False,
) -> JsonValue:
    """Defensively retain only bounded, redacted JSON response values.

    Legacy callers receive the original drop-unknown behavior.  The dashboard
    HTTP client uses ``strict`` for the operation envelope and rejects a taint
    field instead of silently hiding a companion contract violation.
    """

    if depth > MAX_RESPONSE_DEPTH:
        raise CompanionProtocolError("companion response is too deep")
    counter = [0] if count is None else count
    counter[0] += 1
    if counter[0] > MAX_RESPONSE_ITEMS:
        raise CompanionProtocolError("companion response is too large")
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 256:
                raise CompanionProtocolError(
                    "companion response contains an invalid key"
                )
            lowered = key.lower()
            # These fields are explicit raw-material escape hatches and are not
            # part of any dashboard typed view, even if a buggy companion sends
            # them.  This is stricter than generic recursive redaction.
            if lowered in _RESPONSE_TAINT_KEYS:
                if strict:
                    raise CompanionProtocolError(
                        "companion response contains tainted data"
                    )
                continue
            if key not in allowed_keys:
                if strict and (not strict_top_level or depth == 0):
                    raise CompanionProtocolError(
                        "companion response contains an unknown field"
                    )
                continue
            result[key] = _sanitize_response(
                item,
                depth=depth + 1,
                count=counter,
                allowed_keys=allowed_keys,
                strict=strict,
                strict_top_level=strict_top_level,
            )
        try:
            return cast(JsonValue, redact_json(result))
        except (TypeError, ValueError) as exc:
            raise CompanionProtocolError("companion response is not safe JSON") from exc
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_RESPONSE_ITEMS:
            raise CompanionProtocolError("companion response contains too many items")
        return [
            _sanitize_response(
                item,
                depth=depth + 1,
                count=counter,
                allowed_keys=allowed_keys,
                strict=strict,
                strict_top_level=strict_top_level,
            )
            for item in value
        ]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > (1 << 53) - 1:
            raise CompanionProtocolError("companion response contains an unsafe number")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CompanionProtocolError(
                "companion response contains an invalid number"
            )
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_RESPONSE_STRING_BYTES:
            raise CompanionProtocolError("companion response contains oversized text")
        return value
    raise CompanionProtocolError("companion response is not JSON")


def _bounded_audit_context(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise CompanionConfigurationError("dashboard audit context is invalid")
    if any(ord(char) < 0x20 or char in "\r\n" for char in value):
        raise CompanionConfigurationError("dashboard audit context is invalid")
    return value


def _sanitize_operation_response(operation: str, value: object) -> dict[str, JsonValue]:
    """Validate the signed companion envelope and operation-specific payload."""

    fields = OPERATION_RESPONSE_FIELDS.get(operation)
    if fields is None:
        raise OperationNotAllowed("dashboard operation is not allowed")
    if not isinstance(value, Mapping):
        raise CompanionProtocolError("companion response must be an object")
    # The companion's normal response is {ok, operation, data}.  Accepting a
    # direct typed object keeps lightweight test doubles useful, but never
    # weakens validation: it is still checked against this operation schema.
    if "data" in value:
        if set(value) != RESPONSE_ENVELOPE_FIELDS:
            raise CompanionProtocolError("companion envelope is invalid")
        if value.get("operation") != operation or not isinstance(
            value.get("data"), Mapping
        ):
            raise CompanionProtocolError("companion operation envelope is invalid")
        typed = _sanitize_response(
            value["data"],
            allowed_keys=fields,
            strict=True,
            strict_top_level=True,
        )
        if not isinstance(typed, dict):
            raise CompanionProtocolError("companion operation data is invalid")
        _check_exact_preview(value["data"], typed)
        if value.get("ok") is not True:
            raise CompanionProtocolError("companion operation envelope is invalid")
        return {"ok": True, "operation": operation, "data": typed}
    typed = _sanitize_response(
        value, allowed_keys=fields, strict=True, strict_top_level=True
    )
    if not isinstance(typed, dict):
        raise CompanionProtocolError("companion operation data is invalid")
    _check_exact_preview(value, typed)
    return cast(dict[str, JsonValue], typed)


def _check_exact_preview(original: object, typed: dict[str, JsonValue]) -> None:
    """Do not rewrite a preview whose digest is bound to exact rendered bytes."""

    if not isinstance(original, Mapping) or "preview" not in original:
        return
    preview = original["preview"]
    if (
        not isinstance(preview, str)
        or len(preview.encode("utf-8")) > 64 * 1024
        or any(ord(char) < 0x20 and char not in "\t\n\r" for char in preview)
    ):
        raise CompanionProtocolError("companion preview is invalid")
    # A preview must already be provider/path/credential-safe.  Silently
    # redacting it would make the browser display bytes different from the
    # digest/capability that the companion issued.
    if redact_text(preview) != preview:
        raise CompanionProtocolError("companion preview contains tainted data")
    typed["preview"] = preview


def _read_response_bytes(
    response: requests.Response,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    """Read a requests response with a hard byte ceiling."""

    headers = getattr(response, "headers", {})
    if not isinstance(headers, Mapping):
        raise CompanionProtocolError("companion response headers are invalid")
    content_length = headers.get("Content-Length")
    declared: int | None = None
    if content_length is not None:
        if not isinstance(content_length, str) or not _DECIMAL_RE.fullmatch(
            content_length
        ):
            raise CompanionProtocolError("companion response length is invalid")
        declared = int(content_length)
        if declared < 0 or declared > MAX_RESPONSE_BODY_BYTES:
            raise CompanionProtocolError("companion response is too large")
    chunks: list[bytes] = []
    total = 0
    iterator_factory = getattr(response, "iter_content", None)
    if callable(iterator_factory):
        try:
            iterator = iterator_factory(chunk_size=16 * 1024)
            for chunk in iterator:
                if deadline is not None and clock() >= deadline:
                    raise CompanionUnavailable("companion deadline exceeded")
                if not chunk:
                    continue
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise CompanionProtocolError(
                        "companion response contains invalid bytes"
                    )
                chunk_bytes = bytes(chunk)
                total += len(chunk_bytes)
                if total > MAX_RESPONSE_BODY_BYTES:
                    raise CompanionProtocolError("companion response is too large")
                chunks.append(chunk_bytes)
                if deadline is not None and clock() >= deadline:
                    raise CompanionUnavailable("companion deadline exceeded")
        except CompanionProtocolError:
            raise
        except (OSError, TimeoutError, requests.RequestException) as exc:
            raise CompanionUnavailable("companion is unavailable") from exc
    # Lightweight fake responses in tests may not implement iter_content.
    if not chunks and hasattr(response, "content"):
        if deadline is not None and clock() >= deadline:
            raise CompanionUnavailable("companion deadline exceeded")
        raw = response.content
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise CompanionProtocolError("companion response contains invalid bytes")
        raw = bytes(raw)
        if deadline is not None and clock() >= deadline:
            raise CompanionUnavailable("companion deadline exceeded")
        if len(raw) > MAX_RESPONSE_BODY_BYTES:
            raise CompanionProtocolError("companion response is too large")
        if declared is not None and declared != len(raw):
            raise CompanionProtocolError("companion response length is invalid")
        return raw
    if declared is not None and declared != total:
        raise CompanionProtocolError("companion response length is invalid")
    return b"".join(chunks)


class _CircuitBreaker:
    """Small process-local breaker for the single private dependency."""

    def __init__(
        self,
        *,
        threshold: int = BREAKER_FAILURE_THRESHOLD,
        open_seconds: float = BREAKER_OPEN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.open_seconds = open_seconds
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            return self._clock() >= self._open_until

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._open_until = self._clock() + self.open_seconds
                self._failures = 0

    def success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0

    @property
    def open_until(self) -> float:
        with self._lock:
            return self._open_until


class DashboardCompanionClient:
    """Typed HTTP client for the closed dashboard operation surface."""

    def __init__(
        self,
        base_url: str,
        api_key: bytes | bytearray | memoryview | str,
        *,
        timeout: tuple[float, float] = (3.0, 15.0),
        actor: str = "dashboard-admin",
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] = _nonce,
        resolver: Callable[..., object] = socket.getaddrinfo,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self._key = _secret_bytes(api_key)
        if (
            not isinstance(timeout, tuple)
            or len(timeout) != 2
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                or float(item) <= 0
                for item in timeout
            )
            or timeout[0] > 3.0
            or timeout[1] > 15.0
            or timeout[0] > timeout[1]
        ):
            raise CompanionConfigurationError("dashboard HTTP timeout is invalid")
        self.timeout = (float(timeout[0]), float(timeout[1]))
        self.actor = _bounded_actor(actor)
        self._session = session or requests.Session()
        # The client must never follow a companion redirect to an attacker or a
        # provider URL.  ``allow_redirects=False`` is also passed per request.
        self._session.trust_env = False
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._resolver = resolver
        self._monotonic = monotonic
        self._resolved_addresses: tuple[str, ...] | None = None
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
        self._breaker = _CircuitBreaker(clock=monotonic)

    @classmethod
    def from_key_file(
        cls,
        base_url: str,
        key_file: str | os.PathLike[str],
        **kwargs: object,
    ) -> "DashboardCompanionClient":
        """Construct a client from the dedicated mounted dashboard key."""

        return cls(base_url, load_api_key_file(key_file), **kwargs)  # type: ignore[arg-type]

    @property
    def allowed_operations(self) -> frozenset[str]:
        return ALLOWED_OPERATIONS

    def ready(self) -> bool:
        """Return dependency readiness without issuing an operation."""

        if not self._breaker.allow():
            return False
        try:
            resolved = _resolve_private_service(
                COMPANION_SERVICE_HOST,
                COMPANION_SERVICE_PORT,
                resolver=self._resolver,
                deadline=self._monotonic() + self.timeout[0],
                clock=self._monotonic,
            )
        except CompanionClientError:
            return False
        if self._resolved_addresses is None:
            self._resolved_addresses = resolved
        return self._resolved_addresses == resolved

    def call(
        self,
        operation: str | DashboardOperation,
        payload: Mapping[str, object] | None = None,
        *,
        session_actor: str | None = None,
        session_digest: str | None = None,
        audit_context: str | None = None,
    ) -> CompanionResponse:
        """Call one exact operation after validating its JSON-shaped payload."""

        operation_text = (
            operation.value if isinstance(operation, DashboardOperation) else operation
        )
        if (
            not isinstance(operation_text, str)
            or operation_text not in ALLOWED_OPERATIONS
        ):
            raise OperationNotAllowed("dashboard operation is not allowed")
        if payload is None:
            body_value: Mapping[str, object] = {}
        elif isinstance(payload, Mapping):
            body_value = payload
        else:
            raise CompanionConfigurationError("dashboard operation payload is invalid")
        body = _canonical_json(body_value)
        path = f"{COMPANION_API_PREFIX}/{operation_text}"
        actor = self.actor if session_actor is None else _bounded_actor(session_actor)
        if session_digest is not None and (
            not isinstance(session_digest, str)
            or not _SHA256_RE.fullmatch(session_digest)
        ):
            raise CompanionConfigurationError("dashboard session digest is invalid")
        if audit_context is not None:
            audit_context = _bounded_audit_context(audit_context)
        if not self._breaker.allow():
            raise CompanionUnavailable("companion circuit is open")
        deadline = self._monotonic() + self.timeout[1]
        remaining = max(0.001, deadline - self._monotonic())
        if not self._slots.acquire(timeout=remaining):
            raise CompanionUnavailable("companion concurrency limit reached")
        response: requests.Response | None = None
        try:
            resolved = _resolve_private_service(
                COMPANION_SERVICE_HOST,
                COMPANION_SERVICE_PORT,
                resolver=self._resolver,
                deadline=deadline,
                clock=self._monotonic,
            )
            if self._resolved_addresses is None:
                self._resolved_addresses = resolved
            elif self._resolved_addresses != resolved:
                raise CompanionUnavailable("companion service resolution changed")
            _signature, auth_headers = sign_request(
                self._key,
                method="POST",
                path=path,
                operation=operation_text,
                actor=actor,
                body=body,
                timestamp=int(self._clock()),
                nonce=self._nonce_factory(),
                session_digest=session_digest,
                audit_context=audit_context,
            )
            headers = {
                **auth_headers,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
            }
            if session_digest is not None:
                headers["X-CRBL-Dashboard-Session-Digest"] = session_digest
            if audit_context is not None:
                headers["X-CRBL-Dashboard-Audit"] = audit_context
            url = f"{self.base_url}{path}"
            remaining = max(0.001, deadline - self._monotonic())
            connect_timeout = max(0.001, min(self.timeout[0], remaining))
            response = self._session.post(
                url,
                data=body,
                headers=headers,
                timeout=(connect_timeout, min(self.timeout[1], remaining)),
                allow_redirects=False,
                stream=True,
            )
            if bool(getattr(response, "is_redirect", False)) or bool(
                getattr(response, "is_permanent_redirect", False)
            ):
                raise CompanionProtocolError("companion returned a redirect")
            raw = _read_response_bytes(
                response, deadline=deadline, clock=self._monotonic
            )
            status_code = getattr(response, "status_code", None)
            if (
                isinstance(status_code, bool)
                or not isinstance(status_code, int)
                or status_code < 100
                or status_code > 599
            ):
                raise CompanionProtocolError("companion response status is invalid")
            if status_code < 200 or status_code >= 300:
                if status_code in {401, 403}:
                    raise CompanionRejected("companion rejected the dashboard request")
                raise CompanionUnavailable("companion operation failed")
            response_headers = getattr(response, "headers", {})
            raw_content_type = (
                response_headers.get("Content-Type", "")
                if isinstance(response_headers, Mapping)
                else None
            )
            if not isinstance(raw_content_type, str):
                raise CompanionProtocolError("companion response headers are invalid")
            content_type = raw_content_type.split(";", 1)[0].strip().lower()
            if content_type not in {"application/json", "application/problem+json"}:
                raise CompanionProtocolError("companion response type is invalid")
            try:
                decoded = json.loads(
                    raw.decode("utf-8"), object_pairs_hook=_json_object_pairs
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise CompanionProtocolError("companion response is invalid") from exc
            safe = _sanitize_operation_response(operation_text, decoded)
            self._breaker.success()
            return CompanionResponse(
                operation=operation_text, data=safe, status=status_code
            )
        except CompanionRejected:
            self._breaker.success()
            raise
        except (CompanionUnavailable, CompanionProtocolError):
            self._breaker.failure()
            raise
        except (requests.RequestException, OSError, TimeoutError) as exc:
            self._breaker.failure()
            raise CompanionUnavailable("companion is unavailable") from exc
        finally:
            if response is not None:
                response.close()
            self._slots.release()

    # Explicit methods are intentionally boring: a future caller cannot typo a
    # generic operation and accidentally gain a broader companion surface.
    def health(self) -> CompanionResponse:
        return self.call(OP_HEALTH)

    def users(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> CompanionResponse:
        return self.call(OP_USERS, _page_payload(limit=limit, cursor=cursor))

    def resolve_user(
        self,
        *,
        user_id: int,
        fingerprint: str | None = None,
        version: int | None = None,
        session_actor: str | None = None,
        session_digest: str | None = None,
        audit_context: str | None = None,
    ) -> CompanionResponse:
        payload: dict[str, object] = {
            "user_id": _bounded_positive_id(user_id, "user_id")
        }
        if fingerprint is not None:
            payload["fingerprint"] = _bounded_token(
                fingerprint, "fingerprint", max_bytes=256
            )
        if version is not None:
            payload["version"] = _bounded_version(version)
        return self.call(
            OP_USERS_RESOLVE,
            payload,
            session_actor=session_actor,
            session_digest=session_digest,
            audit_context=audit_context,
        )

    def blocked(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> CompanionResponse:
        return self.call(OP_BLOCKED, _page_payload(limit=limit, cursor=cursor))

    def subscriptions(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> CompanionResponse:
        return self.call(OP_SUBSCRIPTIONS, _page_payload(limit=limit, cursor=cursor))

    def deliveries(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        status: str | None = None,
    ) -> CompanionResponse:
        payload = _page_payload(limit=limit, cursor=cursor)
        if status is not None:
            payload["status"] = _bounded_filter(status)
        return self.call(OP_DELIVERIES, payload)

    def quarantine(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> CompanionResponse:
        return self.call(OP_QUARANTINE, _page_payload(limit=limit, cursor=cursor))

    def oracle(self) -> CompanionResponse:
        return self.call(OP_ORACLE)

    def audit(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> CompanionResponse:
        return self.call(OP_AUDIT, _page_payload(limit=limit, cursor=cursor))

    def add_user(
        self,
        *,
        user_id: int,
        fingerprint: str,
        idempotency_key: str,
        version: int,
        confirmation: str | None = None,
        state_fingerprint: str | None = None,
        preview_digest: str | None = None,
        session_actor: str | None = None,
        session_digest: str | None = None,
        audit_context: str | None = None,
    ) -> CompanionResponse:
        return self.call(
            OP_USERS_ADD,
            _mutation_payload(
                user_id=user_id,
                fingerprint=fingerprint,
                idempotency_key=idempotency_key,
                confirmation=confirmation,
                version=version,
                state_fingerprint=state_fingerprint,
                preview_digest=preview_digest,
            ),
            session_actor=session_actor,
            session_digest=session_digest,
            audit_context=audit_context,
        )

    def remove_user(
        self,
        *,
        user_id: int,
        fingerprint: str,
        idempotency_key: str,
        version: int,
        confirmation: str | None = None,
        state_fingerprint: str | None = None,
        preview_digest: str | None = None,
        session_actor: str | None = None,
        session_digest: str | None = None,
        audit_context: str | None = None,
    ) -> CompanionResponse:
        return self.call(
            OP_USERS_REMOVE,
            _mutation_payload(
                user_id=user_id,
                fingerprint=fingerprint,
                idempotency_key=idempotency_key,
                confirmation=confirmation,
                version=version,
                state_fingerprint=state_fingerprint,
                preview_digest=preview_digest,
            ),
            session_actor=session_actor,
            session_digest=session_digest,
            audit_context=audit_context,
        )

    def preview_user_add(self, **kwargs: object) -> CompanionResponse:
        kwargs.pop("confirmation", None)
        return self.add_user(**kwargs)  # type: ignore[arg-type]

    def preview_user_remove(self, **kwargs: object) -> CompanionResponse:
        kwargs.pop("confirmation", None)
        return self.remove_user(**kwargs)  # type: ignore[arg-type]

    def retry_once(
        self,
        *,
        delivery_id: int,
        idempotency_key: str,
        confirmation: str | None = None,
        preview_digest: str | None = None,
        session_actor: str | None = None,
        session_digest: str | None = None,
        audit_context: str | None = None,
    ) -> CompanionResponse:
        return self.call(
            OP_RETRY_ONCE,
            _recovery_payload(
                delivery_id, idempotency_key, confirmation, preview_digest
            ),
            session_actor=session_actor,
            session_digest=session_digest,
            audit_context=audit_context,
        )

    def mark_abandoned(
        self,
        *,
        delivery_id: int,
        idempotency_key: str,
        confirmation: str | None = None,
        preview_digest: str | None = None,
        session_actor: str | None = None,
        session_digest: str | None = None,
        audit_context: str | None = None,
    ) -> CompanionResponse:
        return self.call(
            OP_MARK_ABANDONED,
            _recovery_payload(
                delivery_id, idempotency_key, confirmation, preview_digest
            ),
            session_actor=session_actor,
            session_digest=session_digest,
            audit_context=audit_context,
        )

    def assume_sent(
        self,
        *,
        delivery_id: int,
        idempotency_key: str,
        confirmation: str | None = None,
        preview_digest: str | None = None,
        session_actor: str | None = None,
        session_digest: str | None = None,
        audit_context: str | None = None,
    ) -> CompanionResponse:
        return self.call(
            OP_ASSUME_SENT,
            _recovery_payload(
                delivery_id, idempotency_key, confirmation, preview_digest
            ),
            session_actor=session_actor,
            session_digest=session_digest,
            audit_context=audit_context,
        )

    def resend_once(
        self,
        *,
        delivery_id: int,
        idempotency_key: str,
        confirmation: str | None = None,
        preview_digest: str | None = None,
        session_actor: str | None = None,
        session_digest: str | None = None,
        audit_context: str | None = None,
    ) -> CompanionResponse:
        return self.call(
            OP_RESEND_ONCE,
            _recovery_payload(
                delivery_id, idempotency_key, confirmation, preview_digest
            ),
            session_actor=session_actor,
            session_digest=session_digest,
            audit_context=audit_context,
        )

    def preview_retry_once(self, **kwargs: object) -> CompanionResponse:
        kwargs.pop("confirmation", None)
        return self.retry_once(**kwargs)  # type: ignore[arg-type]

    def preview_mark_abandoned(self, **kwargs: object) -> CompanionResponse:
        kwargs.pop("confirmation", None)
        return self.mark_abandoned(**kwargs)  # type: ignore[arg-type]

    def preview_assume_sent(self, **kwargs: object) -> CompanionResponse:
        kwargs.pop("confirmation", None)
        return self.assume_sent(**kwargs)  # type: ignore[arg-type]

    def preview_resend_once(self, **kwargs: object) -> CompanionResponse:
        kwargs.pop("confirmation", None)
        return self.resend_once(**kwargs)  # type: ignore[arg-type]

    # Naming aliases retain the operation vocabulary used in the deployment
    # manifest while keeping the explicit methods above as the primary API.
    user_add = add_user
    user_remove = remove_user
    delivery_retry_once = retry_once
    delivery_mark_abandoned = mark_abandoned
    delivery_assume_sent = assume_sent
    delivery_resend_once = resend_once


def _page_payload(*, limit: int | None, cursor: str | None) -> dict[str, object]:
    result: dict[str, object] = {}
    if limit is not None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 250
        ):
            raise CompanionConfigurationError("dashboard page limit is invalid")
        result["limit"] = limit
    if cursor is not None:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
            raise CompanionConfigurationError("dashboard cursor is invalid")
        if any(ord(char) < 0x20 or char.isspace() for char in cursor):
            raise CompanionConfigurationError("dashboard cursor is invalid")
        result["cursor"] = cursor
    return result


def _bounded_filter(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise CompanionConfigurationError("dashboard filter is invalid")
    if any(ord(char) < 0x20 or char.isspace() for char in value):
        raise CompanionConfigurationError("dashboard filter is invalid")
    return value


def _bounded_positive_id(value: int, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > (1 << 53) - 1
    ):
        raise CompanionConfigurationError(f"dashboard {field_name} is invalid")
    return value


def _bounded_version(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > (1 << 53) - 1
    ):
        raise CompanionConfigurationError("dashboard version is invalid")
    return value


def _bounded_token(value: str, field_name: str, *, max_bytes: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > max_bytes
    ):
        raise CompanionConfigurationError(f"dashboard {field_name} is invalid")
    if any(ord(char) < 0x20 or char.isspace() for char in value):
        raise CompanionConfigurationError(f"dashboard {field_name} is invalid")
    if field_name == "idempotency_key" and not _IDEMPOTENCY_RE.fullmatch(value):
        raise CompanionConfigurationError(f"dashboard {field_name} is invalid")
    return value


def _bounded_confirmation(value: str) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= 1024:
        raise CompanionConfigurationError("dashboard confirmation is invalid")
    if any(ord(char) < 0x20 or char.isspace() for char in value):
        raise CompanionConfigurationError("dashboard confirmation is invalid")
    if not _TOKEN_RE.fullmatch(value):
        raise CompanionConfigurationError("dashboard confirmation is invalid")
    return value


def _mutation_payload(
    *,
    user_id: int,
    fingerprint: str,
    idempotency_key: str,
    confirmation: str | None = None,
    version: int,
    state_fingerprint: str | None = None,
    preview_digest: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "user_id": _bounded_positive_id(user_id, "user_id"),
        "fingerprint": _bounded_token(fingerprint, "fingerprint", max_bytes=256),
        "idempotency_key": _bounded_token(idempotency_key, "idempotency_key"),
    }
    result["version"] = _bounded_version(version)
    if state_fingerprint is not None:
        result["state_fingerprint"] = _bounded_token(
            state_fingerprint, "state_fingerprint", max_bytes=256
        )
    if preview_digest is not None:
        if not _SHA256_RE.fullmatch(preview_digest):
            raise CompanionConfigurationError("dashboard preview digest is invalid")
        result["preview_digest"] = preview_digest
    if confirmation is not None:
        result["confirmation"] = _bounded_confirmation(confirmation)
    return result


def _recovery_payload(
    delivery_id: int,
    idempotency_key: str,
    confirmation: str | None = None,
    preview_digest: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "delivery_id": _bounded_positive_id(delivery_id, "delivery_id"),
        "idempotency_key": _bounded_token(idempotency_key, "idempotency_key"),
    }
    if preview_digest is not None:
        if not _SHA256_RE.fullmatch(preview_digest):
            raise CompanionConfigurationError("dashboard preview digest is invalid")
        result["preview_digest"] = preview_digest
    if confirmation is not None:
        result["confirmation"] = _bounded_confirmation(confirmation)
    return result


# A concise alias is useful to callers while preserving the descriptive class
# name in tracebacks and documentation.
CompanionClient = DashboardCompanionClient
CompanionOperationsClient = DashboardCompanionClient
DashboardOperationsClient = DashboardCompanionClient


__all__ = [
    "ALLOWED_OPERATIONS",
    "COMPANION_API_PREFIX",
    "CompanionClient",
    "CompanionOperationsClient",
    "CompanionClientError",
    "CompanionConfigurationError",
    "CompanionProtocolError",
    "CompanionRejected",
    "CompanionResponse",
    "CompanionUnavailable",
    "DashboardCompanionClient",
    "DashboardOperationsClient",
    "DashboardOperation",
    "DASHBOARD_MUTATION_OPERATIONS",
    "DASHBOARD_OPERATION_NAMES",
    "DASHBOARD_OPERATIONS",
    "DASHBOARD_READ_OPERATIONS",
    "MUTATION_OPERATIONS",
    "OP_AUDIT",
    "OP_ASSUME_SENT",
    "OP_BLOCKED",
    "OP_DELIVERIES",
    "OP_HEALTH",
    "OP_MARK_ABANDONED",
    "OP_ORACLE",
    "OP_QUARANTINE",
    "OP_RESEND_ONCE",
    "OP_RETRY_ONCE",
    "OP_SUBSCRIPTIONS",
    "OP_USERS",
    "OP_USERS_RESOLVE",
    "OP_USERS_ADD",
    "OP_USERS_REMOVE",
    "READ_OPERATIONS",
    "OPERATION_RESPONSE_FIELDS",
    "RESPONSE_FIELD_ALLOWLIST",
    "RECOVERY_OPERATIONS",
    "SIGNATURE_VERSION",
    "load_api_key_file",
    "operation_allowlist_fingerprint",
    "sign_request",
    "signature_message",
    "validate_operation_allowlist",
]
