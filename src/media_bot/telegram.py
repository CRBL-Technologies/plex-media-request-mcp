"""The few Telegram Bot API calls this bot makes, over plain HTTPS."""

from __future__ import annotations

from typing import Any

import httpx

POLL_SECONDS = 50
MESSAGE_LIMIT = 4096


class TelegramError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class Telegram:
    def __init__(self, token: str):
        # The token is part of every URL, so neither it nor a URL is ever logged.
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = httpx.AsyncClient(timeout=POLL_SECONDS + 15)

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, body: dict[str, Any]) -> Any:
        try:
            response = await self._http.post(f"{self._base}/{method}", json=body)
        except httpx.HTTPError as exc:
            raise TelegramError(f"Telegram {method} failed: {type(exc).__name__}") from None
        try:
            value = response.json()
        except ValueError:
            value = {}
        if response.status_code >= 400 or not isinstance(value, dict) or not value.get("ok"):
            description = value.get("description") if isinstance(value, dict) else None
            raise TelegramError(
                f"Telegram {method} failed: {str(description or response.status_code)[:200]}",
                status=response.status_code,
            )
        return value.get("result")

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {})
        if not isinstance(result, dict) or not isinstance(result.get("username"), str):
            raise TelegramError("Telegram getMe returned no bot username")
        return result

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"timeout": POLL_SECONDS, "allowed_updates": ["message"]}
        if offset is not None:
            body["offset"] = offset
        result = await self._call("getUpdates", body)
        if not isinstance(result, list):
            raise TelegramError("Telegram getUpdates returned no list")
        return [item for item in result if isinstance(item, dict)]

    async def send_message(
        self, chat_id: int, text: str, *, html: bool = False, reply_to: int | None = None
    ) -> None:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:MESSAGE_LIMIT],
            "link_preview_options": {"is_disabled": True},
        }
        if html:
            body["parse_mode"] = "HTML"
        if reply_to is not None:
            body["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        await self._call("sendMessage", body)

    async def send_card(
        self,
        chat_id: int,
        *,
        caption: str,
        poster_url: str | None,
        button_url: str | None,
        reply_to: int | None,
    ) -> None:
        body: dict[str, Any] = {"chat_id": chat_id, "parse_mode": "HTML"}
        if button_url:
            body["reply_markup"] = {
                "inline_keyboard": [[{"text": "▶ Open in Plex", "url": button_url}]]
            }
        if reply_to is not None:
            body["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if poster_url is not None:
            await self._call("sendPhoto", {**body, "photo": poster_url, "caption": caption})
            return
        await self._call(
            "sendMessage", {**body, "text": caption, "link_preview_options": {"is_disabled": True}}
        )

    async def typing(self, chat_id: int) -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
