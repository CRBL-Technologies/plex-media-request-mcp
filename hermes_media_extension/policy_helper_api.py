"""Typed client for the narrow Hermes policy-helper boundary.

The helper is the only runtime source used by the media companion for current
Telegram membership and role checks.  It intentionally has no generic file,
log, shell, or Docker operation.  Responses are parsed into small immutable
records; unknown fields are ignored and raw response objects are never exposed
to the model or to callers.

The transport is injectable so tests can run without Hermes, an HTTP server, or
the ``requests`` package.  A production instance should use the private helper
endpoint and a mounted key; the key itself is never included in ``repr`` or
exceptions.
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import hmac
import os
import re
import signal
import stat
import subprocess
import threading
from datetime import datetime, timezone
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 64 * 1024
MAX_BLOCKED_CONTACTS = 256
MAX_POLICY_USERS = 256
MAX_NOTIFICATION_TEXT_BYTES = 4096
MAX_IDENTITY_LABEL_BYTES = 256
MAX_NOTIFICATION_RETRY_AFTER_SECONDS = 86_400
# TelegramClient already uses a bounded transport timeout, but the helper
# boundary also owns a deadline so an injected/native client cannot hold an
# authenticated request open indefinitely.
NOTIFICATION_TIMEOUT_SECONDS = 15.0
NOTIFICATION_STATUSES = frozenset(
    {
        "sent",
        "retryable-pretransmission",
        # Accept the short spelling from older injected clients while the
        # server emits the explicit pre-transmission classification.
        "retryable",
        "ambiguous",
        "permanent",
    }
)


class PolicyHelperError(RuntimeError):
    """Base class for typed helper failures."""


class PolicyHelperUnavailable(PolicyHelperError):
    """The helper could not be reached or did not return a valid response."""


class PolicyHelperDenied(PolicyHelperError):
    """The helper answered with a deny decision."""


class PolicyHelperResponseError(PolicyHelperError):
    """The helper returned an invalid or oversized typed response."""


POLICY_KEY_HEADER = "X-CRBL-Policy-Key"
MAX_HELPER_REQUEST_BYTES = 16 * 1024
MAX_HELPER_RESPONSE_BYTES = 256 * 1024
DEFAULT_HELPER_VERSION = "media-policy-helper-v1"
HERMES_GATEWAY_SERVICE = "/run/service/gateway-default"
S6_SVC_COMMAND = "/command/s6-svc"


class PolicyHelperServerError(PolicyHelperError):
    """A narrow policy-helper server request failed safely."""


class _HelperHTTPError(PolicyHelperServerError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


Transport = Callable[..., object]


def _safe_text(
    value: object, *, name: str, max_bytes: int = MAX_IDENTITY_LABEL_BYTES
) -> str:
    if not isinstance(value, str):
        raise PolicyHelperResponseError(f"policy helper field {name} is invalid")
    value = value.strip()
    if not value or len(value.encode("utf-8", "strict")) > max_bytes:
        raise PolicyHelperResponseError(f"policy helper field {name} is invalid")
    return value


def _id(value: object, *, name: str, allow_negative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyHelperResponseError(f"policy helper field {name} is invalid")
    if value == 0 or (not allow_negative and value < 0):
        raise PolicyHelperResponseError(f"policy helper field {name} is invalid")
    return value


def _fingerprint(value: object) -> str:
    return _safe_text(value, name="fingerprint", max_bytes=128)


@dataclass(frozen=True, slots=True)
class PolicyMembership:
    """Current membership and role for one native Telegram identity."""

    user_id: int
    chat_id: int
    allowed: bool
    role: str
    fingerprint: str
    version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _id(self.user_id, name="user_id"))
        object.__setattr__(
            self, "chat_id", _id(self.chat_id, name="chat_id", allow_negative=True)
        )
        if not isinstance(self.allowed, bool):
            raise PolicyHelperResponseError("policy helper field allowed is invalid")
        role = _safe_text(self.role, name="role", max_bytes=64).lower()
        if role not in {"user", "admin", "unknown"}:
            raise PolicyHelperResponseError("policy helper role is invalid")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "fingerprint", _fingerprint(self.fingerprint))
        if self.version:
            object.__setattr__(
                self, "version", _safe_text(self.version, name="version", max_bytes=128)
            )

    @property
    def is_admin(self) -> bool:
        return self.allowed and self.role == "admin"

    @property
    def is_authorized(self) -> bool:
        return self.allowed and self.role in {"user", "admin"}


@dataclass(frozen=True, slots=True)
class BlockedContact:
    """Sanitized retained unauthorized-contact summary."""

    user_id: int
    chat_id: int
    observed_at: str
    source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _id(self.user_id, name="user_id"))
        object.__setattr__(
            self, "chat_id", _id(self.chat_id, name="chat_id", allow_negative=True)
        )
        object.__setattr__(
            self, "observed_at", _safe_text(self.observed_at, name="observed_at")
        )
        if self.source is not None:
            object.__setattr__(self, "source", _safe_text(self.source, name="source"))


@dataclass(frozen=True, slots=True)
class ResolvedIdentity:
    """Typed result of a selected numeric Telegram ``getChat`` lookup."""

    user_id: int
    chat_id: int
    display_name: str | None = None
    username: str | None = None
    chat_type: str = "private"

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _id(self.user_id, name="user_id"))
        object.__setattr__(
            self, "chat_id", _id(self.chat_id, name="chat_id", allow_negative=True)
        )
        if self.display_name is not None:
            object.__setattr__(
                self, "display_name", _safe_text(self.display_name, name="display_name")
            )
        if self.username is not None:
            object.__setattr__(
                self, "username", _safe_text(self.username, name="username")
            )
        chat_type = _safe_text(self.chat_type, name="chat_type", max_bytes=32).lower()
        if chat_type not in {"private", "group", "supergroup", "channel"}:
            raise PolicyHelperResponseError("policy helper chat type is invalid")
        object.__setattr__(self, "chat_type", chat_type)


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Minimal helper health/readiness result."""

    ready: bool
    allowlist_fingerprint: str
    version: str
    source: str = "native"

    def __post_init__(self) -> None:
        if not isinstance(self.ready, bool):
            raise PolicyHelperResponseError("policy helper ready flag is invalid")
        object.__setattr__(
            self, "allowlist_fingerprint", _fingerprint(self.allowlist_fingerprint)
        )
        object.__setattr__(
            self, "version", _safe_text(self.version, name="version", max_bytes=128)
        )
        object.__setattr__(
            self, "source", _safe_text(self.source, name="source", max_bytes=64)
        )


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """One configured Telegram allowlist member and its configured role."""

    user_id: int
    role: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _id(self.user_id, name="user_id"))
        role = _safe_text(self.role, name="role", max_bytes=16).lower()
        if role not in {"user", "admin"}:
            raise PolicyHelperResponseError(
                "policy helper current-user role is invalid"
            )
        object.__setattr__(self, "role", role)


@dataclass(frozen=True, slots=True)
class CurrentUsers:
    """Bounded current allowlist view for dashboard/runtime consumers.

    The helper deliberately returns only numeric IDs, their configured role,
    and the policy fingerprint/version.  It never returns the canonical file,
    bot token, or any other Hermes configuration.
    """

    users: tuple[CurrentUser, ...]
    fingerprint: str
    version: str

    def __post_init__(self) -> None:
        if not isinstance(self.users, tuple) or len(self.users) > MAX_POLICY_USERS:
            raise PolicyHelperResponseError("policy helper current users are invalid")
        if any(not isinstance(user, CurrentUser) for user in self.users):
            raise PolicyHelperResponseError("policy helper current user is invalid")
        ids = tuple(user.user_id for user in self.users)
        if len(ids) != len(set(ids)):
            raise PolicyHelperResponseError(
                "policy helper current users are duplicated"
            )
        object.__setattr__(self, "fingerprint", _fingerprint(self.fingerprint))
        object.__setattr__(
            self, "version", _safe_text(self.version, name="version", max_bytes=128)
        )


PolicyUser = CurrentUser
PolicyUsers = CurrentUsers


@dataclass(frozen=True, slots=True)
class NotificationDelivery:
    """Classified result of one helper-owned admin Telegram delivery."""

    chat_id: int
    status: str
    message_id: int | None = None
    retry_after: int | None = None
    transmitted: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "chat_id", _id(self.chat_id, name="chat_id", allow_negative=True)
        )
        status = _safe_text(self.status, name="status", max_bytes=32).lower()
        if status not in NOTIFICATION_STATUSES:
            raise PolicyHelperResponseError(
                "policy helper notification status is invalid"
            )
        object.__setattr__(self, "status", status)
        if self.message_id is not None:
            object.__setattr__(
                self, "message_id", _id(self.message_id, name="message_id")
            )
        if status == "sent" and self.message_id is None:
            raise PolicyHelperResponseError(
                "sent policy helper notification has no message identifier"
            )
        if status != "sent" and self.message_id is not None:
            raise PolicyHelperResponseError(
                "unconfirmed policy helper notification has a message identifier"
            )
        if self.retry_after is not None:
            if (
                isinstance(self.retry_after, bool)
                or not isinstance(self.retry_after, int)
                or not 0 <= self.retry_after <= MAX_NOTIFICATION_RETRY_AFTER_SECONDS
            ):
                raise PolicyHelperResponseError(
                    "policy helper notification retry interval is invalid"
                )
        if self.transmitted is not None and not isinstance(self.transmitted, bool):
            raise PolicyHelperResponseError(
                "policy helper notification transmission flag is invalid"
            )

    @property
    def pre_transmission_retry(self) -> bool:
        return self.status in {"retryable-pretransmission", "retryable"}


AdminNotification = NotificationDelivery


PolicySnapshot = PolicyMembership
Membership = PolicyMembership
BlockedUser = BlockedContact
IdentityResolution = ResolvedIdentity


def _json_object(response: object) -> dict[str, Any]:
    if isinstance(response, Mapping):
        payload = dict(response)
    else:
        status = getattr(response, "status_code", 200)
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            status_int = 500
        if status_int < 200 or status_int >= 300:
            raise PolicyHelperUnavailable("policy helper request was denied")
        body = getattr(response, "content", None)
        if body is None:
            body = getattr(response, "text", None)
        if isinstance(body, str):
            body = body.encode("utf-8", "strict")
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise PolicyHelperResponseError("policy helper response body is invalid")
        raw = bytes(body)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PolicyHelperResponseError("policy helper response is too large")
        try:
            parsed = json.loads(raw.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise PolicyHelperResponseError(
                "policy helper response is not JSON"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise PolicyHelperResponseError("policy helper response is not an object")
        payload = dict(parsed)
    return cast(dict[str, Any], payload)


def _ok(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if payload.get("ok", True) is False:
        raise PolicyHelperDenied("policy helper denied the operation")
    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        raise PolicyHelperResponseError("policy helper response data is invalid")
    return data


class PolicyHelperAPI:
    """Small typed policy-helper client; no generic endpoint is exposed."""

    # Public route names are intentionally fixed.  Callers cannot pass an
    # arbitrary path or operation name through this object.
    ROUTES: ClassVar[dict[str, str]] = {
        "membership": "/v1/policy/membership",
        "current_users": "/v1/policy/current-users",
        "notify_admin": "/v1/policy/notify-admin",
        "blocked_contacts": "/v1/policy/blocked-contacts",
        "resolve_identity": "/v1/policy/resolve-identity",
        "allowlist_mutate": "/v1/policy/allowlist/mutate",
        "runtime_status": "/v1/policy/status",
    }

    def __init__(
        self,
        base_url: str | None = None,
        *,
        key: str | bytes | None = None,
        key_file: str | os.PathLike[str] | None = None,
        transport: object | None = None,
        timeout: tuple[float, float] = (3.0, 15.0),
    ) -> None:
        if base_url is not None:
            if not isinstance(base_url, str) or not base_url.strip():
                raise ValueError("policy helper URL is invalid")
            parsed = urlsplit(base_url.strip())
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("policy helper URL is invalid")
            # ``urlsplit`` defers malformed/out-of-range port validation until
            # this property is read.  Force that check before retaining the
            # endpoint so a bad deployment cannot fail only on its first call.
            try:
                _ = parsed.port
            except ValueError as exc:
                raise ValueError("policy helper URL is invalid") from exc
            base_url = base_url.rstrip("/")
        if key is not None and key_file is not None:
            raise ValueError("provide one policy helper key source")
        if isinstance(key, bytes):
            key_value = key.decode("utf-8", "strict")
        elif key is None:
            key_value = None
        elif isinstance(key, str):
            key_value = key
        else:
            raise ValueError("policy helper key is invalid")
        self._base_url = base_url
        self._key = key_value.strip() if key_value is not None else None
        self._key_file = Path(key_file) if key_file is not None else None
        self._transport = transport
        if (
            len(timeout) != 2
            or timeout[0] <= 0
            or timeout[1] <= 0
            or timeout[0] > 3
            or timeout[1] > 15
            or timeout[0] > timeout[1]
        ):
            raise ValueError("policy helper timeout is invalid")
        self.timeout = (float(timeout[0]), float(timeout[1]))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(configured={bool(self._base_url or self._transport)})"

    @property
    def configured(self) -> bool:
        return bool(self._transport is not None or self._base_url)

    def _credential(self) -> str:
        if self._key is not None:
            if not self._key:
                raise PolicyHelperUnavailable("policy helper key is empty")
            return self._key
        if self._key_file is None:
            raise PolicyHelperUnavailable("policy helper key is not configured")
        try:
            info = self._key_file.stat()
            if (
                self._key_file.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size <= 0
                or info.st_size > 16 * 1024
            ):
                raise PolicyHelperUnavailable("policy helper key is invalid")
            value = self._key_file.read_bytes()
        except OSError as exc:
            raise PolicyHelperUnavailable("policy helper key is unavailable") from exc
        if not value or len(value) > 16 * 1024:
            raise PolicyHelperUnavailable("policy helper key is invalid")
        try:
            result = value.decode("utf-8", "strict").strip()
        except UnicodeDecodeError as exc:
            raise PolicyHelperUnavailable("policy helper key is invalid") from exc
        if not result:
            raise PolicyHelperUnavailable("policy helper key is empty")
        return result

    def _request(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        route = self.ROUTES.get(operation)
        if route is None:
            raise PolicyHelperUnavailable("unsupported policy helper operation")
        if self._transport is None and not self._base_url:
            raise PolicyHelperUnavailable("policy helper is not configured")
        body = json.dumps(
            dict(payload), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(body) > 16 * 1024:
            raise PolicyHelperResponseError("policy helper request is too large")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-CRBL-Policy-Key": self._credential(),
        }
        target = getattr(self._transport, "request", self._transport)
        url = (self._base_url or "") + route
        try:
            if callable(target):
                try:
                    response = target(
                        "POST", url, headers=headers, data=body, timeout=self.timeout
                    )
                except TypeError:
                    response = target(
                        method="POST",
                        url=url,
                        headers=headers,
                        data=body,
                        timeout=self.timeout,
                    )
            else:
                import requests

                response = requests.post(
                    url,
                    headers=headers,
                    data=body,
                    timeout=self.timeout,
                )
        except Exception as exc:
            raise PolicyHelperUnavailable("policy helper request failed") from exc
        return _json_object(response)

    def membership(self, *, user_id: int, chat_id: int) -> PolicyMembership:
        payload = _ok(
            self._request(
                "membership",
                {
                    "user_id": _id(user_id, name="user_id"),
                    "chat_id": _id(chat_id, name="chat_id", allow_negative=True),
                },
            )
        )
        return PolicyMembership(
            user_id=payload.get("user_id", user_id),
            chat_id=payload.get("chat_id", chat_id),
            allowed=payload.get("allowed", False),
            role=payload.get("role", "unknown"),
            fingerprint=payload.get("fingerprint", ""),
            version=payload.get("version", ""),
        )

    # Names used by companion code and dashboard adapters.
    get_membership = membership
    current_membership = membership

    def current_users(self) -> CurrentUsers:
        """Return the bounded configured allowlist and role view."""

        payload = _ok(self._request("current_users", {}))
        rows = payload.get("users", payload.get("items", []))
        if not isinstance(rows, list) or len(rows) > MAX_POLICY_USERS:
            raise PolicyHelperResponseError("policy helper current users are invalid")
        users: list[CurrentUser] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise PolicyHelperResponseError("policy helper current user is invalid")
            typed_row = cast(Mapping[str, object], row)
            users.append(
                CurrentUser(
                    user_id=_id(typed_row.get("user_id"), name="user_id"),
                    role=_safe_text(typed_row.get("role"), name="role", max_bytes=16),
                )
            )
        return CurrentUsers(
            users=tuple(users),
            fingerprint=_fingerprint(payload.get("fingerprint")),
            version=_safe_text(payload.get("version"), name="version", max_bytes=128),
        )

    get_current_users = current_users

    def notify_admin(
        self, *, chat_id: int, text: str, parse_mode: str = ""
    ) -> NotificationDelivery:
        """Deliver one bounded message through Hermes' native Telegram bot.

        This is deliberately not a generic Telegram proxy: the helper server
        accepts only configured administrator chat IDs and the fixed text/
        parse-mode fields below.
        """

        checked_chat_id = _id(chat_id, name="chat_id", allow_negative=True)
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text.encode("utf-8", "strict")) > MAX_NOTIFICATION_TEXT_BYTES
            or any(
                ord(character) < 0x20 and character not in {"\n", "\t"}
                for character in text
            )
        ):
            raise ValueError("notification text is invalid")
        if parse_mode not in {"", "HTML"}:
            raise ValueError("notification parse mode is invalid")
        payload = _ok(
            self._request(
                "notify_admin",
                {
                    "chat_id": checked_chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                },
            )
        )
        return NotificationDelivery(
            chat_id=_id(
                payload.get("chat_id", checked_chat_id),
                name="chat_id",
                allow_negative=True,
            ),
            status=_safe_text(payload.get("status", ""), name="status", max_bytes=32),
            message_id=(
                None
                if payload.get("message_id") is None
                else _id(payload.get("message_id"), name="message_id")
            ),
            retry_after=payload.get("retry_after"),
            transmitted=payload.get("transmitted"),
        )

    # The worker-facing name is intentionally singular and fixed.  The
    # aliases below are retained only for older injected test clients; no
    # arbitrary route/method name is accepted by the helper server.
    send_notification = notify_admin
    send_admin_notification = notify_admin
    notify_admins = notify_admin

    def authorize(
        self, *, user_id: int, chat_id: int, require_admin: bool = False
    ) -> PolicyMembership:
        membership = self.membership(user_id=user_id, chat_id=chat_id)
        if not membership.is_authorized or (require_admin and not membership.is_admin):
            raise PolicyHelperDenied("Telegram identity is not authorized")
        return membership

    def blocked_contacts(self, *, limit: int = 50) -> tuple[BlockedContact, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > MAX_BLOCKED_CONTACTS
        ):
            raise ValueError("blocked contact limit is invalid")
        payload = _ok(self._request("blocked_contacts", {"limit": limit}))
        rows = payload.get("contacts", payload.get("items", []))
        if not isinstance(rows, list) or len(rows) > MAX_BLOCKED_CONTACTS:
            raise PolicyHelperResponseError(
                "policy helper blocked contacts are invalid"
            )
        contacts: list[BlockedContact] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise PolicyHelperResponseError(
                    "policy helper blocked contact is invalid"
                )
            typed_row = cast(Mapping[str, object], row)
            source = typed_row.get("source")
            contacts.append(
                BlockedContact(
                    user_id=_id(typed_row.get("user_id"), name="user_id"),
                    chat_id=_id(
                        typed_row.get("chat_id"),
                        name="chat_id",
                        allow_negative=True,
                    ),
                    observed_at=_safe_text(
                        typed_row.get("observed_at"), name="observed_at"
                    ),
                    source=None
                    if source is None
                    else _safe_text(source, name="source"),
                )
            )
        return tuple(contacts)

    get_blocked_contacts = blocked_contacts

    def resolve_identity(self, *, user_id: int) -> ResolvedIdentity:
        payload = _ok(
            self._request("resolve_identity", {"user_id": _id(user_id, name="user_id")})
        )
        return ResolvedIdentity(
            user_id=payload.get("user_id", user_id),
            chat_id=payload.get("chat_id", user_id),
            display_name=payload.get("display_name"),
            username=payload.get("username"),
            chat_type=payload.get("chat_type", "private"),
        )

    resolve_user = resolve_identity

    def mutate_allowlist(
        self,
        *,
        operation: str,
        user_id: int,
        expected_fingerprint: str,
    ) -> PolicyMembership:
        if operation not in {"add", "remove"}:
            raise ValueError("allowlist operation is invalid")
        payload = _ok(
            self._request(
                "allowlist_mutate",
                {
                    "operation": operation,
                    "user_id": _id(user_id, name="user_id"),
                    "expected_fingerprint": _fingerprint(expected_fingerprint),
                },
            )
        )
        # Mutation response is deliberately the current narrow membership
        # snapshot, never file bytes or an expanded allowlist.
        return PolicyMembership(
            user_id=payload.get("user_id", user_id),
            chat_id=payload.get("chat_id", user_id),
            allowed=payload.get("allowed", operation == "add"),
            role=payload.get("role", "user"),
            fingerprint=payload.get("fingerprint", expected_fingerprint),
            version=payload.get("version", ""),
        )

    def add_user(self, **kwargs: Any) -> PolicyMembership:
        return self.mutate_allowlist(operation="add", **kwargs)

    def remove_user(self, **kwargs: Any) -> PolicyMembership:
        return self.mutate_allowlist(operation="remove", **kwargs)

    def runtime_status(self) -> RuntimeStatus:
        payload = _ok(self._request("runtime_status", {}))
        return RuntimeStatus(
            ready=payload.get("ready", False),
            allowlist_fingerprint=payload.get("fingerprint", ""),
            version=payload.get("version", ""),
            source=payload.get("source", "native"),
        )

    status = runtime_status


def _helper_key_bytes(
    *, key: str | bytes | None, key_file: str | os.PathLike[str] | None
) -> bytes:
    if (key is None) == (key_file is None):
        raise ValueError("policy helper server requires one key source")
    if key is not None:
        value = key.encode("utf-8", "strict") if isinstance(key, str) else key
        if not isinstance(value, bytes) or not value or len(value) > 16 * 1024:
            raise ValueError("policy helper server key is invalid")
        raw_value = value
    else:
        path = Path(cast(str | os.PathLike[str], key_file))
        try:
            info = path.stat()
            if (
                path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size <= 0
                or info.st_size > 16 * 1024
            ):
                raise ValueError("policy helper server key file is unsafe")
            raw_value = path.read_bytes()
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError("policy helper server key file is unavailable") from exc
        if not raw_value:
            raise ValueError("policy helper server key file is empty")
    try:
        normalized = raw_value.decode("utf-8", "strict").strip().encode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("policy helper server key is invalid") from exc
    if not normalized:
        raise ValueError("policy helper server key is empty")
    return normalized


def _helper_id(value: object, *, allow_negative: bool = False) -> int:
    if isinstance(value, str):
        try:
            value = int(value.strip(), 10)
        except (TypeError, ValueError) as exc:
            raise _HelperHTTPError(400, "numeric ID is invalid") from exc
    return _id(value, name="id", allow_negative=allow_negative)


def _bounded_retry_after(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_NOTIFICATION_RETRY_AFTER_SECONDS
    ):
        return None
    return value


def _notification_message_id(value: object) -> object:
    if isinstance(value, Mapping):
        message_id = value.get("message_id")
        nested = value.get("result")
        if message_id is None and isinstance(nested, Mapping):
            message_id = nested.get("message_id")
        return message_id
    return getattr(value, "message_id", None)


def _notification_outcome(
    value: object, *, exception: bool = False
) -> tuple[str, int | None, int | None, bool]:
    """Reduce a native Telegram result/error to the fixed delivery envelope.

    Error descriptions and provider payloads are deliberately ignored.  A
    retry classification is emitted only for an explicitly known
    pre-transmission failure; an unknown outcome is ambiguous and must not be
    retried automatically.
    """

    if isinstance(value, Mapping):
        raw_classification = value.get("error_class", value.get("classification"))
        raw_transmitted = value.get("transmitted", False)
        raw_pre_transmission = value.get("pre_transmission", False)
        raw_retry_after = value.get("retry_after")
        raw_ok = value.get("ok")
    else:
        raw_classification = getattr(
            value, "error_class", getattr(value, "classification", None)
        )
        raw_transmitted = getattr(value, "transmitted", False)
        raw_pre_transmission = getattr(value, "pre_transmission", False)
        raw_retry_after = getattr(value, "retry_after", None)
        raw_ok = getattr(value, "ok", None)
    classification_value = getattr(raw_classification, "value", raw_classification)
    classification = (
        classification_value.strip().lower()
        if isinstance(classification_value, str)
        else ""
    )
    transmitted = bool(raw_transmitted)
    retry_after = _bounded_retry_after(raw_retry_after)
    message_id = _notification_message_id(value)
    if not exception and (
        raw_ok is True or (raw_ok is None and message_id is not None)
    ):
        try:
            checked_message_id = _helper_id(message_id)
        except _HelperHTTPError:
            # A successful provider response without an ID cannot be safely
            # replayed: the send may already have happened.
            return "ambiguous", None, None, True
        return "sent", checked_message_id, None, True
    if not transmitted and (
        bool(raw_pre_transmission) or classification in {"retryable", "rate_limited"}
    ):
        return "retryable-pretransmission", None, retry_after, False
    if classification in {"terminal_recipient", "authentication", "application"}:
        return "permanent", None, None, transmitted
    if transmitted or classification == "ambiguous":
        return "ambiguous", None, None, True
    # An unknown exception/result is not evidence that the request was never
    # sent.  Preserve the durable worker's unknown-delivery recovery path.
    return "ambiguous", None, None, transmitted


class PolicyHelperServer:
    """Hermes-hosted, authenticated, file-narrow policy helper service.

    The server intentionally exposes only :attr:`PolicyHelperAPI.ROUTES`.  It
    reads the canonical allowlist/log through ``whitelist_helper`` and accepts
    an optional native Telegram bot object solely for one selected ``get_chat``
    lookup.  It is bound to loopback by default; callers that place it on a
    private container network must still avoid publishing its port publicly.
    """

    def __init__(
        self,
        *,
        policy_path: str | os.PathLike[str],
        key: str | bytes | None = None,
        key_file: str | os.PathLike[str] | None = None,
        log_paths: Sequence[str | os.PathLike[str]] = (),
        bot: object | None = None,
        admin_ids: Sequence[int] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        version: str = DEFAULT_HELPER_VERSION,
        recycle_callback: Callable[[], None] | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "::1", "0.0.0.0", "::"}:
            raise ValueError("policy helper server host must be loopback/private")
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 <= port <= 65_535
        ):
            raise ValueError("policy helper server port is invalid")
        if not isinstance(version, str) or not version.strip() or len(version) > 128:
            raise ValueError("policy helper server version is invalid")
        self.policy_path = Path(policy_path)
        self.log_paths = tuple(Path(path) for path in log_paths)
        self.bot = bot
        self.host = host
        self.port = port
        self.version = version.strip()
        self._key = _helper_key_bytes(key=key, key_file=key_file)
        try:
            from .whitelist_helper import parse_allowed_users

            parse_allowed_users(self.policy_path)
        except Exception as exc:
            raise ValueError("policy helper policy file is unavailable") from exc
        configured_admin_ids = (
            _policy_admin_ids(self.policy_path) if admin_ids is None else admin_ids
        )
        self._admin_ids = frozenset(_helper_id(value) for value in configured_admin_ids)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._recycle_callback = recycle_callback or self._recycle_gateway

    @staticmethod
    def _recycle_gateway() -> None:
        """Ask s6 to recycle only Hermes' native gateway service.

        The helper is deliberately unable to address Docker, Plex, or any
        other service.  The fixed s6 control path is available in the pinned
        image and the command is issued only after a successful response has
        been flushed to the mutating caller.  A missing control path is normal
        in unit tests and in non-gateway helper probes, so it is a no-op.
        """

        service = Path(HERMES_GATEWAY_SERVICE)
        if service.is_dir():
            try:
                result = subprocess.run(
                    (S6_SVC_COMMAND, "-r", HERMES_GATEWAY_SERVICE),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5.0,
                )
                if result.returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                # Fall through to the native foreground-gateway signal path.
                pass

        # ``gateway run`` is the /init main program in v2026.8.3, not always
        # an s6 service.  Use Hermes' own PID/runtime identity checks before a
        # same-UID SIGTERM; this cannot target a stale arbitrary process and
        # never invokes a Docker or Plex lifecycle command.
        try:
            status_module = importlib.import_module("gateway.status")
            get_running_pid = getattr(status_module, "get_running_pid", None)
            read_runtime_status = getattr(status_module, "read_runtime_status", None)
            if not callable(get_running_pid):
                return
            pid_value = get_running_pid(cleanup_stale=False)
            pid = pid_value if isinstance(pid_value, int) and pid_value > 0 else None
            if pid is None:
                if not callable(read_runtime_status):
                    return
                record = read_runtime_status()
                get_runtime_pid = getattr(
                    status_module, "get_runtime_status_running_pid", None
                )
                if isinstance(record, dict) and callable(get_runtime_pid):
                    runtime_pid = get_runtime_pid(record)
                    if isinstance(runtime_pid, int) and runtime_pid > 0:
                        pid = runtime_pid
        except Exception:  # noqa: BLE001
            return
        if pid is None or pid == os.getpid():
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            # The atomic policy write remains valid if the process exited or
            # a non-Linux runtime cannot signal its gateway PID.
            return

    @property
    def address(self) -> tuple[str, int] | None:
        if self._httpd is None:
            return None
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def running(self) -> bool:
        return self._httpd is not None and self._thread is not None

    def _snapshot(self) -> Any:
        try:
            from .whitelist_helper import parse_allowed_users

            return parse_allowed_users(self.policy_path)
        except Exception as exc:
            raise _HelperHTTPError(
                503, "policy helper allowlist is unavailable"
            ) from exc

    def _membership(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        user_id = _helper_id(payload.get("user_id"))
        chat_id = _helper_id(payload.get("chat_id"), allow_negative=True)
        snapshot = self._snapshot()
        allowed = user_id in snapshot.user_ids
        role = (
            "admin"
            if allowed and user_id in self._admin_ids
            else "user"
            if allowed
            else "unknown"
        )
        return {
            "user_id": user_id,
            "chat_id": chat_id,
            "allowed": allowed,
            "role": role,
            "fingerprint": snapshot.fingerprint,
            "version": self.version,
        }

    def _current_users(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        users = [
            {
                "user_id": user_id,
                "role": "admin" if user_id in self._admin_ids else "user",
            }
            for user_id in snapshot.user_ids
        ]
        if len(users) > MAX_POLICY_USERS:
            raise _HelperHTTPError(503, "policy helper allowlist is too large")
        return {
            "users": users,
            "fingerprint": snapshot.fingerprint,
            "version": self.version,
        }

    def _notify_admin(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if set(payload) != {"chat_id", "text", "parse_mode"}:
            raise _HelperHTTPError(400, "policy helper notification fields are invalid")
        chat_id = _helper_id(payload.get("chat_id"), allow_negative=True)
        text = payload.get("text")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text.encode("utf-8", "strict")) > MAX_NOTIFICATION_TEXT_BYTES
            or any(
                ord(character) < 0x20 and character not in {"\n", "\t"}
                for character in text
            )
        ):
            raise _HelperHTTPError(400, "policy helper notification text is invalid")
        parse_mode = payload.get("parse_mode")
        if not isinstance(parse_mode, str) or parse_mode not in {"", "HTML"}:
            raise _HelperHTTPError(
                400, "policy helper notification parse mode is invalid"
            )
        snapshot = self._snapshot()
        if chat_id not in snapshot.user_ids or chat_id not in self._admin_ids:
            raise _HelperHTTPError(
                403, "policy helper notification recipient is invalid"
            )
        send_message = getattr(self.bot, "send_message", None)
        if not callable(send_message):
            return {
                "chat_id": chat_id,
                "status": "permanent",
                "transmitted": False,
            }

        result_box: list[object] = []
        error_box: list[Exception] = []
        completed = threading.Event()

        def _send() -> None:
            try:
                result_box.append(send_message(chat_id, text, parse_mode=parse_mode))
            except Exception as exc:  # noqa: BLE001
                error_box.append(exc)
            finally:
                completed.set()

        # The native Telegram client has its own finite HTTP deadline.  This
        # second bound protects the authenticated helper route from an
        # injected/native implementation that violates that contract.
        thread = threading.Thread(target=_send, name="crbl-policy-notify", daemon=True)
        thread.start()
        if not completed.wait(NOTIFICATION_TIMEOUT_SECONDS):
            return {
                "chat_id": chat_id,
                "status": "ambiguous",
                "transmitted": True,
            }
        if error_box:
            status, message_id, retry_after, transmitted = _notification_outcome(
                error_box[0], exception=True
            )
        elif result_box:
            status, message_id, retry_after, transmitted = _notification_outcome(
                result_box[0]
            )
        else:
            status, message_id, retry_after, transmitted = (
                "ambiguous",
                None,
                None,
                True,
            )
        result: dict[str, Any] = {
            "chat_id": chat_id,
            "status": status,
            "transmitted": transmitted,
        }
        if message_id is not None:
            result["message_id"] = message_id
        if retry_after is not None:
            result["retry_after"] = retry_after
        return result

    def _blocked_contacts(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        limit = payload.get("limit", 50)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 < limit <= MAX_BLOCKED_CONTACTS
        ):
            raise _HelperHTTPError(400, "blocked contact limit is invalid")
        try:
            from .whitelist_helper import parse_blocked_user_logs

            events = parse_blocked_user_logs(
                self.log_paths,
                max_records=limit,
                include_source=False,
            )
        except Exception as exc:
            raise _HelperHTTPError(503, "policy helper logs are unavailable") from exc
        observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return {
            "contacts": [
                {
                    "user_id": event.user_id,
                    "chat_id": event.chat_id,
                    "observed_at": observed_at,
                }
                for event in events
            ]
        }

    def _resolve_identity(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        user_id = _helper_id(payload.get("user_id"))
        snapshot = self._snapshot()
        if user_id not in snapshot.user_ids:
            raise _HelperHTTPError(403, "Telegram identity is not in the allowlist")
        bot = self.bot
        get_chat = getattr(bot, "get_chat", None) if bot is not None else None
        if not callable(get_chat):
            raise _HelperHTTPError(503, "Telegram identity lookup is unavailable")
        try:
            chat = get_chat(user_id)
        except Exception as exc:
            raise _HelperHTTPError(503, "Telegram identity lookup failed") from exc
        if isinstance(chat, Mapping):
            chat_id = chat.get("id")
            display_name = chat.get("full_name", chat.get("title"))
            username = chat.get("username")
            chat_type = chat.get("type", "private")
        else:
            chat_id = getattr(chat, "id", getattr(chat, "chat_id", None))
            display_name = (
                getattr(chat, "full_name", None)
                or getattr(chat, "display_name", None)
                or getattr(chat, "title", None)
            )
            username = getattr(chat, "username", None)
            chat_type = getattr(chat, "type", getattr(chat, "chat_type", "private"))
        chat_id = _helper_id(chat_id, allow_negative=True)
        if chat_id != user_id:
            raise _HelperHTTPError(
                503, "Telegram identity lookup changed the selected ID"
            )
        if display_name is not None and not isinstance(display_name, str):
            display_name = None
        if username is not None and not isinstance(username, str):
            username = None
        if not isinstance(chat_type, str) or chat_type not in {
            "private",
            "group",
            "supergroup",
            "channel",
        }:
            chat_type = "private"
        return {
            "user_id": user_id,
            "chat_id": chat_id,
            "display_name": display_name,
            "username": username,
            "chat_type": chat_type,
        }

    def _mutate_allowlist(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation = payload.get("operation")
        user_id = _helper_id(payload.get("user_id"))
        expected = payload.get("expected_fingerprint")
        if not isinstance(operation, str) or operation not in {"add", "remove"}:
            raise _HelperHTTPError(400, "allowlist operation is invalid")
        if not isinstance(expected, str):
            raise _HelperHTTPError(400, "allowlist fingerprint is required")
        try:
            expected = _fingerprint(expected)
        except PolicyHelperResponseError as exc:
            raise _HelperHTTPError(400, "allowlist fingerprint is invalid") from exc
        try:
            from .whitelist_helper import mutate_allowlist

            result = mutate_allowlist(
                self.policy_path,
                user_id,
                operation=operation,
                expected_fingerprint=expected,
                admin_ids=self._admin_ids,
            )
        except Exception as exc:
            status = (
                409
                if type(exc).__name__ in {"FingerprintMismatch", "AdminRemovalDenied"}
                else 503
            )
            raise _HelperHTTPError(
                status, "allowlist mutation was not applied"
            ) from exc
        return {
            "user_id": result.user_id,
            "chat_id": result.user_id,
            "allowed": result.user_id in result.snapshot.user_ids,
            "role": "admin" if result.user_id in self._admin_ids else "user",
            "fingerprint": result.snapshot.fingerprint,
            "version": self.version,
            "operation": result.operation,
            "changed": result.changed,
            "status": result.status,
        }

    def _status(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        return {
            "ready": True,
            "fingerprint": snapshot.fingerprint,
            "version": self.version,
            "source": "hermes-native",
        }

    def handle_request(self, route: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch one already-authenticated, bounded typed request."""

        if route == PolicyHelperAPI.ROUTES["membership"]:
            return self._membership(payload)
        if route == PolicyHelperAPI.ROUTES["current_users"]:
            return self._current_users()
        if route == PolicyHelperAPI.ROUTES["notify_admin"]:
            return self._notify_admin(payload)
        if route == PolicyHelperAPI.ROUTES["blocked_contacts"]:
            return self._blocked_contacts(payload)
        if route == PolicyHelperAPI.ROUTES["resolve_identity"]:
            return self._resolve_identity(payload)
        if route == PolicyHelperAPI.ROUTES["allowlist_mutate"]:
            return self._mutate_allowlist(payload)
        if route == PolicyHelperAPI.ROUTES["runtime_status"]:
            return self._status()
        raise _HelperHTTPError(404, "policy helper route is unavailable")

    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        values = handler.headers.get_all(POLICY_KEY_HEADER, [])
        return len(values) == 1 and hmac.compare_digest(values[0].encode(), self._key)

    def _serve_request(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.command != "POST":
            raise _HelperHTTPError(405, "method is not supported")
        if not self._authorized(handler):
            raise _HelperHTTPError(401, "policy helper authorization failed")
        content_types = handler.headers.get_all("Content-Type", [])
        if (
            len(content_types) != 1
            or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise _HelperHTTPError(400, "request content type is invalid")
        length_values = handler.headers.get_all("Content-Length", [])
        if len(length_values) != 1:
            raise _HelperHTTPError(400, "request body length is invalid")
        raw_length = length_values[0]
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise _HelperHTTPError(400, "request body is invalid")
        if length < 0 or length > MAX_HELPER_REQUEST_BYTES:
            raise _HelperHTTPError(413, "request body is too large")
        body = handler.rfile.read(length)
        if len(body) != length:
            raise _HelperHTTPError(400, "request body is incomplete")
        try:
            payload = json.loads(body.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise _HelperHTTPError(400, "request body is not JSON") from exc
        if not isinstance(payload, Mapping):
            raise _HelperHTTPError(400, "request body is not an object")
        if "?" in handler.path or "#" in handler.path:
            raise _HelperHTTPError(404, "policy helper route is unavailable")
        route = handler.path
        result = self.handle_request(route, cast(Mapping[str, Any], payload))
        output = json.dumps(
            {"ok": True, "data": result}, separators=(",", ":")
        ).encode()
        if len(output) > MAX_HELPER_RESPONSE_BYTES:
            raise _HelperHTTPError(503, "policy helper response is too large")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(output)))
        handler.end_headers()
        handler.wfile.write(output)
        handler.wfile.flush()
        if (
            route == PolicyHelperAPI.ROUTES["allowlist_mutate"]
            and result.get("changed") is True
        ):
            try:
                self._recycle_callback()
            except Exception:  # noqa: BLE001
                # Never turn an already-completed mutation into a transport
                # error because a local s6 recycle hook failed.
                return

    def start(self) -> tuple[str, int]:
        if self._httpd is not None:
            address = self.address
            if address is None:
                raise RuntimeError("policy helper server address is unavailable")
            return address
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CRBLPolicyHelper/1"

            def do_POST(self) -> None:  # noqa: N802
                try:
                    owner._serve_request(self)
                except _HelperHTTPError as exc:
                    body = b'{"ok":false,"error":"policy helper request failed"}'
                    self.send_response(exc.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:  # noqa: BLE001
                    body = b'{"ok":false,"error":"policy helper unavailable"}'
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return None

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="crbl-policy-helper",
            daemon=True,
        )
        self._thread.start()
        address = self.address
        if address is None:
            raise RuntimeError("policy helper server failed to start")
        return address

    def serve_forever(self) -> None:
        if self._httpd is None:
            self.start()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)


def probe_policy_helper(
    *,
    host: str,
    port: int,
    key_file: str | os.PathLike[str],
    timeout: tuple[float, float] = (1.0, 3.0),
) -> bool:
    """Return whether the authenticated private helper reports ready.

    The probe uses the same typed client and fixed status route as the plugin;
    it never accepts a caller-supplied path or emits response details.  The
    entrypoint calls it against ``127.0.0.1`` even when the server is bound to
    ``0.0.0.0`` so readiness does not depend on container DNS.
    """

    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("policy helper probe host must be loopback")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port <= 65_535:
        raise ValueError("policy helper probe port is invalid")
    if len(timeout) != 2 or timeout[0] <= 0 or timeout[1] <= 0:
        raise ValueError("policy helper probe timeout is invalid")
    address = f"[{host}]" if ":" in host else host
    try:
        status = PolicyHelperAPI(
            f"http://{address}:{port}", key_file=key_file, timeout=timeout
        ).runtime_status()
    except (PolicyHelperError, OSError, ValueError):
        return False
    return status.ready


def _env_ids(value: str) -> tuple[int, ...]:
    if not value.strip():
        return ()
    values: list[int] = []
    for raw in value.split(","):
        try:
            values.append(_helper_id(raw.strip()))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("policy helper admin IDs are invalid") from exc
    return tuple(values)


_POLICY_SELECTOR_VARIABLES = frozenset(
    {"TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_USERS", "TELEGRAM_ADMIN_IDS"}
)
_POLICY_ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(?P<value>.*?)[ \t]*$"
)


def _policy_selected_values(path: Path) -> dict[str, str]:
    """Select only the bot/admin assignments from canonical Hermes dotenv.

    ``dotenv_values`` parses Hermes' quoting/comment rules without exporting
    anything into this process.  We pre-scan only the three approved names so
    duplicate or malformed security assignments fail closed; unrelated dotenv
    values are never returned to callers.
    """

    try:
        info = path.stat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size <= 0
            or info.st_size > 1024 * 1024
        ):
            raise ValueError("policy helper policy file is unsafe")
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise ValueError("policy helper policy file is unavailable") from exc
    seen_names: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export") and (
            len(stripped) == 6 or stripped[6].isspace()
        ):
            stripped = stripped[6:].lstrip()
        assignment = _POLICY_ASSIGNMENT_RE.fullmatch(stripped)
        if assignment is None:
            for name in _POLICY_SELECTOR_VARIABLES:
                if stripped.startswith(name) and (
                    len(stripped) == len(name)
                    or not stripped[len(name)].isalnum()
                    and stripped[len(name)] != "_"
                ):
                    raise ValueError("policy helper selected policy is malformed")
            continue
        name = assignment.group("name")
        if name not in _POLICY_SELECTOR_VARIABLES:
            continue
        if name in seen_names:
            raise ValueError("policy helper selected policy is duplicated")
        seen_names.add(name)
        value = assignment.group("value").strip()
        if value.startswith(("'", '"')) and len(value) == 1:
            raise ValueError("policy helper selected policy is malformed")
    try:
        from dotenv import dotenv_values

        parsed = dotenv_values(stream=io.StringIO(text), interpolate=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("policy helper selected policy is malformed") from exc
    selected: dict[str, str] = {}
    for name in seen_names:
        value = parsed.get(name)
        if not isinstance(value, str):
            raise ValueError("policy helper selected policy is malformed")
        selected[name] = value.strip()
    return selected


def _policy_admin_ids(path: Path) -> tuple[int, ...]:
    """Read only the bounded admin-ID assignments from canonical Hermes dotenv."""

    selected = _policy_selected_values(path)
    users_value = selected.get("TELEGRAM_ADMIN_USERS")
    ids_value = selected.get("TELEGRAM_ADMIN_IDS")
    if users_value is not None and ids_value is not None:
        users = _env_ids(users_value)
        ids = _env_ids(ids_value)
        if set(users) != set(ids):
            raise ValueError("policy helper admin selectors conflict")
        return tuple(sorted(set(users)))
    value = users_value if users_value is not None else ids_value
    return _env_ids(value) if value is not None else ()


def _admin_ids_from_environment() -> tuple[int, ...] | None:
    """Read an explicit process override without silently merging selectors."""

    users_present = "TELEGRAM_ADMIN_USERS" in os.environ
    ids_present = "TELEGRAM_ADMIN_IDS" in os.environ
    if not users_present and not ids_present:
        return None
    users_value = os.getenv("TELEGRAM_ADMIN_USERS", "")
    ids_value = os.getenv("TELEGRAM_ADMIN_IDS", "")
    if users_present and ids_present:
        users = _env_ids(users_value)
        ids = _env_ids(ids_value)
        if set(users) != set(ids):
            raise ValueError("policy helper admin selectors conflict")
        return tuple(sorted(set(users)))
    value = users_value if users_present else ids_value
    return tuple(sorted(set(_env_ids(value))))


def _telegram_bot_from_policy(selected: Mapping[str, str]) -> object | None:
    """Build the narrow Telegram lookup client from the selected token only."""

    token = selected.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    try:
        from media_companion.clients.telegram import TelegramClient

        return TelegramClient(token=token)
    except Exception:  # noqa: BLE001
        return None


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Hermes-hosted helper as a private sidecar process."""

    parser = argparse.ArgumentParser(description="Run the CRBL Hermes policy helper")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--serve", action="store_true", help="serve until stopped")
    mode.add_argument(
        "--probe", action="store_true", help="probe the authenticated helper status"
    )
    parser.add_argument(
        "--host", default=os.getenv("CRBL_POLICY_HELPER_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("CRBL_POLICY_HELPER_PORT", "8787")),
    )
    parser.add_argument(
        "--policy-file",
        default=os.getenv(
            "CRBL_POLICY_FILE",
            str(Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / ".env"),
        ),
    )
    parser.add_argument(
        "--key-file",
        default=os.getenv("CRBL_POLICY_HELPER_KEY_FILE")
        or os.getenv("POLICY_HELPER_KEY_FILE"),
    )
    parser.add_argument(
        "--log-file",
        action="append",
        default=None,
        help="bounded gateway log path; may be repeated",
    )
    args = parser.parse_args(argv)
    if not isinstance(args.key_file, str) or not args.key_file:
        parser.error("a private policy-helper key file is required")
    if args.probe:
        return (
            0
            if probe_policy_helper(
                host=args.host,
                port=args.port,
                key_file=args.key_file,
            )
            else 78
        )
    try:
        selected_policy = _policy_selected_values(Path(args.policy_file))
    except ValueError as exc:
        parser.error(str(exc))
    hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
    log_paths = tuple(args.log_file or (str(hermes_home / "logs" / "gateway.log"),))
    bot = _telegram_bot_from_policy(selected_policy)
    try:
        canonical_admins_present = any(
            name in selected_policy
            for name in ("TELEGRAM_ADMIN_USERS", "TELEGRAM_ADMIN_IDS")
        )
        admin_ids = (
            _policy_admin_ids(Path(args.policy_file))
            if canonical_admins_present
            else _admin_ids_from_environment()
        )
        server = PolicyHelperServer(
            policy_path=args.policy_file,
            key_file=args.key_file,
            log_paths=log_paths,
            bot=bot,
            # Preserve None when neither explicit process selector exists so
            # PolicyHelperServer reads the canonical dotenv itself.
            admin_ids=admin_ids,
            host=args.host,
            port=args.port,
        )
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    stop = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop.set()
        server.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    server.start()
    while not stop.wait(1.0):
        pass
    return 0


__all__ = [
    "AdminNotification",
    "BlockedContact",
    "BlockedUser",
    "DEFAULT_HELPER_VERSION",
    "HERMES_GATEWAY_SERVICE",
    "CurrentUser",
    "CurrentUsers",
    "IdentityResolution",
    "MAX_HELPER_REQUEST_BYTES",
    "MAX_HELPER_RESPONSE_BYTES",
    "MAX_NOTIFICATION_TEXT_BYTES",
    "MAX_NOTIFICATION_RETRY_AFTER_SECONDS",
    "NOTIFICATION_STATUSES",
    "NOTIFICATION_TIMEOUT_SECONDS",
    "MAX_POLICY_USERS",
    "Membership",
    "POLICY_KEY_HEADER",
    "PolicyHelperAPI",
    "PolicyHelperDenied",
    "PolicyHelperError",
    "PolicyHelperResponseError",
    "PolicyHelperServer",
    "PolicyHelperServerError",
    "PolicyHelperUnavailable",
    "PolicyMembership",
    "NotificationDelivery",
    "PolicyUser",
    "PolicyUsers",
    "PolicySnapshot",
    "ResolvedIdentity",
    "RuntimeStatus",
    "S6_SVC_COMMAND",
    "main",
    "probe_policy_helper",
]


if __name__ == "__main__":  # pragma: no cover - sidecar process entrypoint.
    raise SystemExit(main())
