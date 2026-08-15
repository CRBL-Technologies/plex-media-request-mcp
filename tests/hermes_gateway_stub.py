"""Minimal 71-tool gateway contract used by the Hermes image smoke test."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from media_gateway.constants import ADMIN_UPSTREAM_TOOLS, SHARED_TOOLS


def _tool(name: str, scope: str) -> dict[str, object]:
    return {
        "name": name,
        "description": f"CI contract for {name}",
        "inputSchema": {"type": "object", "additionalProperties": False},
        "scope": scope,
    }


BODY = json.dumps(
    {
        "tools": [_tool(name, "shared") for name in SHARED_TOOLS]
        + [_tool(name, "admin") for name in sorted(ADMIN_UPSTREAM_TOOLS)]
    },
    separators=(",", ":"),
).encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/api/schema":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, _format: str, *args: object) -> None:
        del args


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 18082), Handler).serve_forever()
