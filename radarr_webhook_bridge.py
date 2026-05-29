from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import urlparse

from media_request_server import MediaRequestService, RequestStore, load_config

ENV_HOST = "PLEX_MEDIA_REQUEST_WEBHOOK_HOST"
ENV_PORT = "PLEX_MEDIA_REQUEST_WEBHOOK_PORT"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 18081


def _positive_payload_id(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def extract_radarr_movie_id(payload: dict[str, Any]) -> int | None:
    movie = payload.get("movie")
    if not isinstance(movie, dict):
        return None
    return _positive_payload_id(movie.get("id"))


def extract_sonarr_series_id(payload: dict[str, Any]) -> int | None:
    series = payload.get("series")
    if not isinstance(series, dict):
        return None
    return _positive_payload_id(series.get("id"))


def should_handle_radarr_event(payload: dict[str, Any]) -> bool:
    event_type = str(payload.get("eventType") or "").strip().lower()
    # Radarr sends Download after import. Rename/file-upgrade events can also
    # mean an already requested movie became available. The notification path
    # verifies hasFile before sending.
    return event_type in {"download", "rename", "moviefileupgrade"}


def should_handle_sonarr_event(payload: dict[str, Any]) -> bool:
    event_type = str(payload.get("eventType") or "").strip().lower()
    # Sonarr sends Download after import. Rename/episode-file-delete are ignored;
    # the notification path verifies requested seasons are complete before sending.
    return event_type in {"download", "episodefileupgrade"}


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    data = json.dumps(body, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class ArrWebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        print(f"{self.address_string()} - {format % args}", flush=True)

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        if urlparse(self.path).path == "/health":
            _json_response(self, 200, {"ok": True, "service": "plex-media-request-webhook-bridge"})
            return
        _json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        path = urlparse(self.path).path
        if path in {"/radarr", "/webhooks/radarr"}:
            self._handle_radarr()
            return
        if path in {"/sonarr", "/webhooks/sonarr"}:
            self._handle_sonarr()
            return
        _json_response(self, 404, {"ok": False, "error": "not found"})

    def _read_payload(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            _json_response(self, 400, {"ok": False, "error": f"invalid JSON: {exc}"})
            return None
        if not isinstance(payload, dict):
            _json_response(self, 400, {"ok": False, "error": "payload must be a JSON object"})
            return None
        return payload

    def _handle_radarr(self) -> None:
        payload = self._read_payload()
        if payload is None:
            return
        if not should_handle_radarr_event(payload):
            _json_response(self, 202, {"ok": True, "handled": False, "reason": "ignored eventType", "eventType": payload.get("eventType")})
            return
        movie_id = extract_radarr_movie_id(payload)
        if movie_id is None:
            _json_response(self, 400, {"ok": False, "error": "missing movie.id"})
            return
        service = cast(ArrWebhookServer, self.server).service
        result = service.notify_movie_available(movie_id)
        _json_response(self, 200, {"ok": True, "handled": True, "service": "radarr", "radarrMovieId": movie_id, "result": result})

    def _handle_sonarr(self) -> None:
        payload = self._read_payload()
        if payload is None:
            return
        if not should_handle_sonarr_event(payload):
            _json_response(self, 202, {"ok": True, "handled": False, "reason": "ignored eventType", "eventType": payload.get("eventType")})
            return
        series_id = extract_sonarr_series_id(payload)
        if series_id is None:
            _json_response(self, 400, {"ok": False, "error": "missing series.id"})
            return
        service = cast(ArrWebhookServer, self.server).service
        result = service.notify_series_available(series_id)
        _json_response(self, 200, {"ok": True, "handled": True, "service": "sonarr", "sonarrSeriesId": series_id, "result": result})


class ArrWebhookServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        service: MediaRequestService,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.service = service


def build_service() -> MediaRequestService:
    return MediaRequestService(load_config(), request_store=RequestStore.from_env())


def main() -> None:
    host = os.getenv(ENV_HOST, DEFAULT_HOST).strip() or DEFAULT_HOST
    port = int(os.getenv(ENV_PORT, str(DEFAULT_PORT)))
    server = ArrWebhookServer((host, port), ArrWebhookHandler, build_service())
    print(f"Arr webhook bridge listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
