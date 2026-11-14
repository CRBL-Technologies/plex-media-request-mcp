"""The bot's only way out: an HTTPS CONNECT proxy that opens named hosts and nothing else.

The bot's network has no route to the internet, the NAS or the home network. This
proxy runs in its own container on that network. It opens a tunnel only to a host
in ALLOWED_HOSTS, on port 443, at a public address. Everything else is refused.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from contextlib import suppress

LOGGER = logging.getLogger("media_bot.egress")

PORT = 3128
# The engines web_search may use, each with the one host it contacts.
SEARCH_ENGINES = {
    "wikipedia": "en.wikipedia.org",
    "duckduckgo": "html.duckduckgo.com",
    "yahoo": "search.yahoo.com",
    "brave": "search.brave.com",
}
ALLOWED_HOSTS = frozenset(
    {
        "api.telegram.org",  # messages and photos
        "openrouter.ai",  # the model
        "my.1password.com",  # the bot's secrets at startup
        *SEARCH_ENGINES.values(),
    }
)
HEAD_LIMIT = 8192
HEAD_TIMEOUT_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 15


def target(head: bytes) -> str | None:
    """Return the allowed host a CONNECT request names, or None to refuse it."""

    try:
        method, authority, _version = head.split(b"\r\n", 1)[0].decode("ascii").split(" ")
    except (UnicodeDecodeError, ValueError):
        return None
    host, _, port = authority.rpartition(":")
    host = host.lower()
    if method != "CONNECT" or port != "443" or host not in ALLOWED_HOSTS:
        return None
    return host


def public_addresses(addresses: list[str]) -> list[str]:
    """Keep only addresses on the public internet, so a name cannot lead inside."""

    return [item for item in addresses if ipaddress.ip_address(item).is_global]


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with suppress(OSError, asyncio.IncompleteReadError):
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    with suppress(OSError):
        writer.close()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    upstream: asyncio.StreamWriter | None = None
    try:
        try:
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=HEAD_TIMEOUT_SECONDS
            )
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, OSError):
            return
        host = target(head)
        if host is None:
            LOGGER.warning("refused: %r", head.split(b"\r\n", 1)[0][:120])
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        try:
            loop = asyncio.get_running_loop()
            found = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            addresses = public_addresses([item[4][0] for item in found])
            if not addresses:
                raise OSError("no public address")
            upstream_reader, opened = await asyncio.wait_for(
                asyncio.open_connection(addresses[0], 443), timeout=CONNECT_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError) as exc:
            LOGGER.warning("%s unreachable: %s", host, type(exc).__name__)
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        upstream = opened
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_pipe(reader, opened), _pipe(upstream_reader, writer))
    finally:
        for stream in (writer, upstream):
            if stream is not None:
                with suppress(OSError):
                    stream.close()


async def serve() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", PORT, limit=HEAD_LIMIT)
    async with server:
        await server.serve_forever()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(serve())


if __name__ == "__main__":
    main()
