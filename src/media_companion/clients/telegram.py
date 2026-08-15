"""Bounded Telegram Bot API client and deterministic notification renderer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import html
import json
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

import requests

from ..config import SecretFileRef, ServiceEndpoint, TimeoutConfig, normalize_url
from ..errors import DependencyError
from ..redaction import redact_text
from .radarr import (
    AdapterCircuitOpenError,
    AdapterConfigurationError,
    AdapterError,
    AdapterHTTPError,
    AdapterResponseError,
    AdapterTimeoutError,
    AdapterTransportError,
    ConfiguredHTTPTransport,
    HTTPResponse,
    HttpTransport,
    MAX_PROVIDER_RESPONSE_BYTES,
    SecretReader,
    _ConfiguredHTTPTransport,
    _response_body,
    _response_json,
    _response_status,
    _secret_value,
)


TELEGRAM_DEFAULT_URL = "https://api.telegram.org"
TELEGRAM_ALLOWED_HOSTS = frozenset({"api.telegram.org"})
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_FIELD_LIMIT = 512
TELEGRAM_MAX_PHOTO_BYTES = 8 * 1024 * 1024
TELEGRAM_MAX_ERROR_BYTES = 512
_TELEGRAM_METHODS = frozenset({"sendMessage", "sendPhoto", "getChat"})


class TelegramErrorClass(str, Enum):
    """Stable delivery classification consumed by the durable worker."""

    RETRYABLE = "retryable"
    AMBIGUOUS = "ambiguous"
    TERMINAL_RECIPIENT = "terminal_recipient"
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    APPLICATION = "application"


class TelegramError(DependencyError):
    """Safe Telegram failure with no token, URL, or raw response body."""

    def __init__(
        self,
        error_class: TelegramErrorClass,
        description: str,
        *,
        status_code: int | None = None,
        retry_after: int | None = None,
        transmitted: bool = False,
        migrate_to_chat_id: int | None = None,
    ) -> None:
        self.error_class = error_class
        self.status_code = status_code
        self.retry_after = retry_after
        self.transmitted = transmitted
        self.migrate_to_chat_id = migrate_to_chat_id
        self.description = redact_text(description, max_bytes=TELEGRAM_MAX_ERROR_BYTES)
        super().__init__(self.description)

    @property
    def classification(self) -> TelegramErrorClass:
        """Compatibility name used by delivery workers."""

        return self.error_class

    @property
    def pre_transmission(self) -> bool:
        """Whether delivery may safely retry this transport outcome."""

        return not self.transmitted and self.error_class in {
            TelegramErrorClass.RETRYABLE,
            TelegramErrorClass.RATE_LIMITED,
        }


@dataclass(frozen=True, slots=True)
class TelegramSendResult:
    ok: bool
    chat_id: int
    message_id: int | None = None
    error_class: TelegramErrorClass | None = None
    description: str | None = None
    retry_after: int | None = None

    @property
    def transmitted(self) -> bool:
        return self.ok


@dataclass(frozen=True, slots=True)
class TelegramChat:
    chat_id: int
    chat_type: str
    username: str | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationLine:
    """Untrusted normalized unit accepted by the deterministic renderer."""

    title: str
    year: int | None = None
    quality: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    plex_url: str | None = None
    requester: str | None = None


def _chat_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise ValueError("chat_id must be a non-zero integer")
    return value


def _safe_field(
    value: object, *, fallback: str = "", max_bytes: int = TELEGRAM_FIELD_LIMIT
) -> str:
    if not isinstance(value, str):
        return fallback
    redacted = redact_text(value, max_bytes=max_bytes)
    if not isinstance(redacted, str):
        return fallback
    value = redacted
    # Escape after removing controls so attacker-provided HTML cannot become a
    # formatting/control channel.  Preserve ordinary newlines for captions.
    value = "".join(
        character if character in "\n\r\t" or ord(character) >= 0x20 else " "
        for character in value
    )
    return value.strip()[:max_bytes]


def escape_html(value: object, *, max_bytes: int = TELEGRAM_FIELD_LIMIT) -> str:
    return html.escape(_safe_field(value, max_bytes=max_bytes), quote=True)


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = urlsplit(text)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        if parsed.hostname.lower() not in {"app.plex.tv", "plex.tv", "www.plex.tv"}:
            return None
        sensitive_keys = {
            "token",
            "access_token",
            "api_key",
            "apikey",
            "auth",
            "authorization",
            "password",
            "secret",
        }
        if any(
            key.lower() in sensitive_keys
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        ):
            return None
        if any(
            term in parsed.fragment.lower()
            for term in ("token=", "access_token=", "api_key=")
        ):
            return None
        if (
            any(ord(character) < 0x20 for character in text)
            or len(text.encode("utf-8")) > 2048
        ):
            return None
    except ValueError:
        return None
    return text


def _image_mime_type(photo: bytes) -> str | None:
    if photo.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if photo.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if photo.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if photo.startswith(b"RIFF") and photo[8:12] == b"WEBP":
        return "image/webp"
    return None


def _unit_line(
    unit: NotificationLine, *, admin: bool, max_bytes: int | None = None
) -> str:
    title = escape_html(unit.title or "Available media")
    if (
        unit.year is not None
        and isinstance(unit.year, int)
        and not isinstance(unit.year, bool)
        and 1800 <= unit.year <= 3000
    ):
        title += f" ({unit.year})"
    detail: list[str] = []
    if (
        unit.season_number is not None
        and isinstance(unit.season_number, int)
        and unit.season_number >= 0
    ):
        if (
            unit.episode_number is not None
            and isinstance(unit.episode_number, int)
            and unit.episode_number > 0
        ):
            detail.append(f"S{unit.season_number:02d}E{unit.episode_number:02d}")
        else:
            detail.append(
                "Specials"
                if unit.season_number == 0
                else f"Season {unit.season_number}"
            )
    if unit.quality:
        detail.append(escape_html(unit.quality, max_bytes=128))
    if admin and unit.requester:
        detail.append(f"for {escape_html(unit.requester, max_bytes=128)}")
    suffix = f" — {' · '.join(detail)}" if detail else ""
    link = _safe_url(unit.plex_url)
    line = (
        f'• <b>{title}</b>{suffix} — <a href="{html.escape(link, quote=True)}">Open in Plex</a>'
        if link
        else f"• <b>{title}</b>{suffix}"
    )
    if max_bytes is None or len(line.encode("utf-8")) <= max_bytes:
        return line

    # A caller-supplied bound can be smaller than one ordinary rendered unit.
    # Drop the optional link first, then shorten only the untrusted title.  The
    # result remains valid HTML and is never cut through an entity or tag.
    compact = f"• <b>{title}</b>{suffix}"
    if len(compact.encode("utf-8")) <= max_bytes:
        return compact
    raw_title = unit.title if isinstance(unit.title, str) else "Available media"
    characters = list(raw_title)
    while characters:
        characters.pop()
        candidate_title = escape_html(
            "".join(characters) + "…", max_bytes=TELEGRAM_FIELD_LIMIT
        )
        candidate = f"• <b>{candidate_title}</b>{suffix}"
        if len(candidate.encode("utf-8")) <= max_bytes:
            return candidate
    minimal = "• <b>…</b>"
    if len(minimal.encode("utf-8")) <= max_bytes:
        return minimal
    # Extremely small test/configuration bounds cannot carry a formatted unit;
    # keep the deterministic marker bounded rather than returning over-limit
    # HTML that Telegram would reject.
    return "•" if max_bytes >= len("•".encode("utf-8")) else "."


def render_notification(
    units: Sequence[NotificationLine],
    *,
    notification_class: str = "requester",
    heading: str = "Available on Plex",
    max_bytes: int = TELEGRAM_MESSAGE_LIMIT,
) -> tuple[str, ...]:
    """Render stable HTML chunks, splitting only at unit boundaries."""

    if notification_class not in {"requester", "admin"}:
        raise ValueError("notification_class must be requester or admin")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    ordered = tuple(units)
    if not ordered:
        return ()
    limit = min(max_bytes, TELEGRAM_MESSAGE_LIMIT)
    heading_text = f"<b>{escape_html(heading, max_bytes=256)}</b>"
    if len(heading_text.encode("utf-8")) > limit:
        heading_text = "<b>…</b>" if len("<b>…</b>".encode("utf-8")) <= limit else "."
    line_limit = max(1, limit - len(heading_text.encode("utf-8")) - 1)
    lines = [
        _unit_line(unit, admin=notification_class == "admin", max_bytes=line_limit)
        for unit in ordered
    ]
    chunks: list[str] = []
    current = heading_text
    for line in lines:
        candidate = f"{current}\n{line}"
        if len(candidate.encode("utf-8")) <= limit:
            current = candidate
            continue
        if current == heading_text:
            # ``_unit_line`` already compacted to the available budget.  The
            # fallback protects against unusual multibyte/heading arithmetic.
            current = (
                f"{heading_text}\n{line}"
                if len(candidate.encode("utf-8")) <= limit
                else heading_text
            )
            continue
        chunks.append(current)
        current = (
            f"{heading_text}\n{line}"
            if len(f"{heading_text}\n{line}".encode("utf-8")) <= limit
            else heading_text
        )
    if current:
        chunks.append(current)
    return tuple(chunks)


class TelegramClient:
    """Fixed-method Telegram Bot API client.

    The API token is necessarily present in Telegram's Bot API path, but it is
    kept only in this object and all transport/error representations redact the
    URL.  No method accepts an arbitrary Bot API method name.
    """

    service_name = "telegram"

    def __init__(
        self,
        token: str | SecretFileRef | Path | None = None,
        *,
        endpoint: ServiceEndpoint | str | None = None,
        config: object | None = None,
        secret_reader: SecretReader | Callable[[object], str] | None = None,
        transport: HttpTransport | None = None,
        timeouts: TimeoutConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        configured_endpoint: object | None = endpoint
        if configured_endpoint is None and config is not None:
            configured_endpoint = getattr(config, "telegram_url", None)
        if configured_endpoint is None:
            self.base_url = TELEGRAM_DEFAULT_URL
        elif isinstance(configured_endpoint, ServiceEndpoint):
            self.base_url = configured_endpoint.origin
        elif isinstance(configured_endpoint, str):
            self.base_url = normalize_url(
                configured_endpoint, field_name="telegram_url"
            )
        else:
            raise AdapterConfigurationError("telegram origin is invalid")
        if urlsplit(self.base_url).scheme.lower() != "https":
            raise AdapterConfigurationError("Telegram origin must use HTTPS")
        parsed_endpoint = urlsplit(self.base_url)
        if (
            parsed_endpoint.hostname is None
            or parsed_endpoint.hostname.lower() not in TELEGRAM_ALLOWED_HOSTS
        ):
            raise AdapterConfigurationError("Telegram origin is not allowlisted")
        if parsed_endpoint.port not in {None, 443}:
            raise AdapterConfigurationError("Telegram origin port is not allowlisted")
        self.token = _secret_value(
            token, config=config, field_name="telegram_bot_token", reader=secret_reader
        )
        configured_timeouts = timeouts or getattr(config, "timeouts", None)
        self.transport: HttpTransport = transport or _ConfiguredHTTPTransport(
            timeouts=configured_timeouts,
            session=session,
            allowed_origin=self.base_url,
            allowed_addresses=getattr(config, "telegram_allowed_addresses", ())
            if config is not None
            else (),
        )

    def _api_url(self, method: str) -> str:
        if method not in _TELEGRAM_METHODS:
            raise ValueError("Telegram method is not allowlisted")
        return f"{self.base_url}/bot{self.token}/{method}"

    def _call(
        self,
        method: str,
        *,
        payload: Mapping[str, object] | None = None,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    ) -> Mapping[str, object]:
        try:
            response = self.transport.request(
                "POST",
                self._api_url(method),
                headers={"Accept": "application/json", **dict(headers or {})},
                json_body=payload,
                body=body,
                max_bytes=max_bytes,
            )
        except AdapterTransportError as exc:
            error_class = (
                TelegramErrorClass.AMBIGUOUS
                if getattr(exc, "transmitted", False)
                else TelegramErrorClass.RETRYABLE
            )
            raise TelegramError(
                error_class,
                "Telegram transport outcome is unknown"
                if error_class is TelegramErrorClass.AMBIGUOUS
                else "Telegram request was not transmitted",
                transmitted=bool(getattr(exc, "transmitted", False)),
            ) from exc
        if len(_response_body(response)) > max_bytes:
            raise TelegramError(
                TelegramErrorClass.APPLICATION,
                "Telegram response exceeded the bounded size",
            )
        status_code = _response_status(response)
        if status_code < 200:
            raise TelegramError(
                TelegramErrorClass.APPLICATION,
                "Telegram returned an invalid HTTP status",
                status_code=status_code,
            )
        if 300 <= status_code < 400:
            raise TelegramError(
                TelegramErrorClass.APPLICATION,
                "Telegram redirects are disabled",
                status_code=status_code,
            )
        try:
            result = _response_json(response)
        except AdapterResponseError as exc:
            if status_code >= 500:
                raise TelegramError(
                    TelegramErrorClass.AMBIGUOUS,
                    "Telegram returned an ambiguous response",
                    status_code=status_code,
                    transmitted=True,
                ) from exc
            raise TelegramError(
                TelegramErrorClass.APPLICATION,
                "Telegram returned invalid JSON",
                status_code=status_code,
            ) from exc
        if not isinstance(result, Mapping):
            raise TelegramError(
                TelegramErrorClass.APPLICATION,
                "Telegram returned an unexpected response",
                status_code=status_code,
            )
        if result.get("ok") is True and status_code < 400:
            return result
        code = result.get("error_code")
        code_int = (
            code
            if isinstance(code, int) and not isinstance(code, bool)
            else status_code
        )
        raw_description = result.get("description")
        description: str = (
            raw_description
            if isinstance(raw_description, str)
            else "Telegram request failed"
        )
        retry_after: int | None = None
        migrate_to_chat_id: int | None = None
        parameters = result.get("parameters")
        if isinstance(parameters, Mapping):
            candidate = parameters.get("retry_after")
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate >= 0
            ):
                retry_after = min(candidate, 86_400)
            migration = parameters.get("migrate_to_chat_id")
            if (
                isinstance(migration, int)
                and not isinstance(migration, bool)
                and migration != 0
            ):
                migrate_to_chat_id = migration
        error_class = classify_telegram_error(
            code_int, description, retry_after=retry_after
        )
        raise TelegramError(
            error_class,
            description,
            status_code=code_int,
            retry_after=retry_after,
            transmitted=True,
            migrate_to_chat_id=migrate_to_chat_id,
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str = "HTML",
        reply_markup: Mapping[str, object] | None = None,
        disable_web_page_preview: bool = False,
    ) -> TelegramSendResult:
        chat_id = _chat_id(chat_id)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must not be blank")
        if len(text.encode("utf-8")) > TELEGRAM_MESSAGE_LIMIT:
            raise ValueError("text exceeds Telegram message limit")
        if parse_mode not in {"HTML", ""}:
            raise ValueError("only HTML or plain text is supported")
        if not isinstance(disable_web_page_preview, bool):
            raise ValueError("disable_web_page_preview must be a boolean")
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": bool(disable_web_page_preview),
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = _bounded_markup(reply_markup)
        result = self._call("sendMessage", payload=payload)
        message = result.get("result")
        message_id = message.get("message_id") if isinstance(message, Mapping) else None
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            raise TelegramError(
                TelegramErrorClass.APPLICATION,
                "Telegram returned no message identifier",
            )
        return TelegramSendResult(True, chat_id, message_id)

    send_text = send_message

    def send_photo(
        self,
        chat_id: int,
        photo: bytes,
        *,
        caption: str | None = None,
        parse_mode: str = "HTML",
        reply_markup: Mapping[str, object] | None = None,
    ) -> TelegramSendResult:
        chat_id = _chat_id(chat_id)
        if (
            not isinstance(photo, bytes)
            or not photo
            or len(photo) > TELEGRAM_MAX_PHOTO_BYTES
        ):
            raise ValueError("photo is empty or exceeds the bounded size")
        mime = _image_mime_type(photo)
        if mime is None:
            raise ValueError("photo is not a supported image")
        if parse_mode not in {"HTML", ""}:
            raise ValueError("only HTML or plain text is supported")
        if caption is not None:
            if not isinstance(caption, str) or not caption.strip():
                raise ValueError("caption must be non-empty text when provided")
            if len(caption.encode("utf-8")) > TELEGRAM_CAPTION_LIMIT:
                raise ValueError("caption exceeds Telegram caption limit")
        boundary = (
            "crbl-" + hashlib.sha256(photo + (caption or "").encode()).hexdigest()[:16]
        )
        parts = [
            ("chat_id", str(chat_id).encode()),
            (
                "photo",
                photo,
                mime.encode("ascii"),
                (b"poster." + mime.split("/", 1)[1].encode("ascii")),
            ),
        ]
        if caption is not None:
            parts.append(("caption", caption.encode()))
        if parse_mode:
            parts.append(("parse_mode", parse_mode.encode()))
        if reply_markup is not None:
            parts.append(
                (
                    "reply_markup",
                    json.dumps(
                        _bounded_markup(reply_markup), separators=(",", ":")
                    ).encode(),
                )
            )
        body = _multipart(parts, boundary)
        result = self._call(
            "sendPhoto",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        )
        message = result.get("result")
        message_id = message.get("message_id") if isinstance(message, Mapping) else None
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            raise TelegramError(
                TelegramErrorClass.APPLICATION,
                "Telegram returned no message identifier",
            )
        return TelegramSendResult(True, chat_id, message_id)

    def get_chat(self, chat_id: int) -> TelegramChat:
        chat_id = _chat_id(chat_id)
        result = self._call("getChat", payload={"chat_id": chat_id})
        value = result.get("result")
        if not isinstance(value, Mapping):
            raise TelegramError(
                TelegramErrorClass.APPLICATION, "Telegram returned no chat"
            )
        returned = value.get("id")
        if (
            isinstance(returned, bool)
            or not isinstance(returned, int)
            or returned != chat_id
        ):
            raise TelegramError(
                TelegramErrorClass.APPLICATION, "Telegram chat identity did not match"
            )
        raw_chat_type = value.get("type")
        raw_username = value.get("username")
        raw_first = value.get("first_name")
        raw_last = value.get("last_name")
        chat_type: str = _safe_field(raw_chat_type, fallback="unknown", max_bytes=32)
        username: str | None = raw_username if isinstance(raw_username, str) else None
        first: str = raw_first if isinstance(raw_first, str) else ""
        last: str = raw_last if isinstance(raw_last, str) else ""
        name = " ".join(part for part in (first, last) if part).strip() or None
        return TelegramChat(
            chat_id,
            chat_type,
            _safe_field(username, max_bytes=128) or None,
            _safe_field(name, max_bytes=256) or None,
        )

    def send_notification(
        self,
        chat_id: int,
        units: Sequence[NotificationLine],
        *,
        notification_class: str = "requester",
    ) -> tuple[TelegramSendResult, ...]:
        # Durable chunk claiming/retry/unknown resolution belongs to
        # ``delivery.py``.  This adapter intentionally exposes one bounded
        # transport call per already-persisted chunk; callers should prefer
        # ``notification_chunks`` + ``send_message`` from the outbox worker.
        return tuple(
            self.send_message(chat_id, chunk)
            for chunk in self.notification_chunks(
                units, notification_class=notification_class
            )
        )

    @staticmethod
    def notification_chunks(
        units: Sequence[NotificationLine], *, notification_class: str = "requester"
    ) -> tuple[str, ...]:
        return render_notification(units, notification_class=notification_class)


def classify_telegram_error(
    code: int | None, description: str, *, retry_after: int | None = None
) -> TelegramErrorClass:
    text = description.lower()
    if retry_after is not None or code == 429:
        return TelegramErrorClass.RATE_LIMITED
    if code == 401:
        return TelegramErrorClass.AUTHENTICATION
    if code in {403} and any(
        term in text
        for term in (
            "blocked",
            "kicked",
            "deactivated",
            "not a member",
            "can't initiate conversation",
            "cannot initiate conversation",
        )
    ):
        return TelegramErrorClass.TERMINAL_RECIPIENT
    if code == 400 and any(
        term in text
        for term in (
            "chat not found",
            "user is deactivated",
            "bot was blocked",
            "kicked",
            "can't initiate conversation",
            "cannot initiate conversation",
        )
    ):
        return TelegramErrorClass.TERMINAL_RECIPIENT
    if code in {403, 404}:
        return TelegramErrorClass.APPLICATION
    if code is not None and code >= 500:
        return TelegramErrorClass.AMBIGUOUS
    if code == 400:
        return TelegramErrorClass.APPLICATION
    return TelegramErrorClass.RETRYABLE


def _bounded_markup(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("reply markup must be a mapping")
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("reply markup is not JSON-safe") from exc
    if len(encoded.encode("utf-8")) > 4 * 1024:
        raise ValueError("reply markup is too large")
    return value


def _multipart(parts: Sequence[object], boundary: str) -> bytes:
    encoded: list[bytes] = []
    for part in parts:
        if not isinstance(part, tuple) or len(part) < 2:
            raise ValueError("invalid multipart part")
        name = part[0]
        value = part[1]
        if not isinstance(name, str) or not isinstance(value, bytes):
            raise ValueError("invalid multipart value")
        encoded.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'.encode()
        )
        if len(part) == 4:
            content_type, filename = part[2], part[3]
            if not isinstance(content_type, bytes) or not isinstance(filename, bytes):
                raise ValueError("invalid multipart metadata")
            encoded[-1] += (
                b'; filename="' + filename + b'"\r\nContent-Type: ' + content_type
            )
        encoded.append(b"\r\n\r\n" + value + b"\r\n")
    encoded.append(f"--{boundary}--\r\n".encode())
    return b"".join(encoded)


__all__ = [
    "AdapterCircuitOpenError",
    "AdapterConfigurationError",
    "AdapterError",
    "AdapterHTTPError",
    "AdapterResponseError",
    "AdapterTimeoutError",
    "AdapterTransportError",
    "ConfiguredHTTPTransport",
    "HTTPResponse",
    "HttpTransport",
    "NotificationLine",
    "TelegramChat",
    "TelegramClient",
    "TelegramError",
    "TelegramErrorClass",
    "TelegramSendResult",
    "escape_html",
    "classify_telegram_error",
    "render_notification",
]
