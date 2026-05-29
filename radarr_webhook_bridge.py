from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from media_request_server import MediaRequestService, RequestStore, load_config

ENV_HOST = "PLEX_MEDIA_REQUEST_WEBHOOK_HOST"
ENV_PORT = "PLEX_MEDIA_REQUEST_WEBHOOK_PORT"
ENV_TOKEN = "PLEX_MEDIA_REQUEST_WEBHOOK_TOKEN"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080


def extract_radarr_movie_id(payload: dict[str, Any]) -> int | None:
    movie = payload.get("movie")
    if not isinstance(movie, dict):
        return None
    movie_id = movie.get("id")
    if movie_id is None or isinstance(movie_id, bool):
        return None
    try:
        parsed = int(movie_id)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def should_handle_radarr_event(payload: dict[str, Any]) -> bool:
    event_type = str(payload.get("eventType") or "").strip().lower()
    # Radarr sends Download after import. Rename can also indicate an imported file
    # became available or was upgraded; the MCP notify path verifies hasFile before
    # notifying, so it is safe to check these narrow file-present events.
    return event_type in {"download", "rename", "moviefileupgrade"}


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    data = json.dumps(body, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _authorized(handler: BaseHTTPRequestHandler, token: str | None) -> bool:
    if not token:
        return True
    header = handler.headers.get("X-Webhook-Token") or handler.headers.get("X-Radarr-Webhook-Token")
    if header == token:
        return True
    query = parse_qs(urlparse(handler.path).query)
    return token in query.get("token", [])


class RadarrWebhookHandler(BaseHTTPRequestHandler):
    server: "RadarrWebhookServer"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        print(f"{self.address_string()} - {format % args}", flush=True)

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        if urlparse(self.path).path == "/health":
            _json_response(self, 200, {"ok": True})
            return
        _json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        path = urlparse(self.path).path
        if path not in {"/radarr", "/webhooks/radarr"}:
            _json_response(self, 404, {"ok": False, "error": "not found"})
            return
        if not _authorized(self, cast(RadarrWebhookServer, self.server).webhook_token):
            _json_response(self, 401, {"ok": False, "error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            _json_response(self, 400, {"ok": False, "error": f"invalid JSON: {exc}"})
            return
        if not isinstance(payload, dict):
            _json_response(self, 400, {"ok": False, "error": "payload must be a JSON object"})
            return

        if not should_handle_radarr_event(payload):
            _json_response(
                self,
                202,
                {
                    "ok": True,
                    "handled": False,
                    "reason": "ignored eventType",
                    "eventType": payload.get("eventType"),
                },
            )
            return

        movie_id = extract_radarr_movie_id(payload)
        if movie_id is None:
            _json_response(self, 400, {"ok": False, "error": "missing movie.id"})
            return

        service = cast(RadarrWebhookServer, self.server).service
        result = service.notify_movie_available(movie_id)
        _json_response(self, 200, {"ok": True, "handled": True, "radarrMovieId": movie_id, "result": result})


class RadarrWebhookServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        service: MediaRequestService,
        webhook_token: str | None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.service = service
        self.webhook_token = webhook_token


def build_service() -> MediaRequestService:
    return MediaRequestService(load_config(), request_store=RequestStore.from_env())


def main() -> None:
    host = os.getenv(ENV_HOST, DEFAULT_HOST).strip() or DEFAULT_HOST
    port = int(os.getenv(ENV_PORT, str(DEFAULT_PORT)))
    token = os.getenv(ENV_TOKEN, "").strip() or None
    server = RadarrWebhookServer((host, port), RadarrWebhookHandler, build_service(), token)
    print(f"Radarr webhook bridge listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
