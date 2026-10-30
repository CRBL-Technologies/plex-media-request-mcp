"""Project fetched gateway secrets into the existing, private file contracts."""

from __future__ import annotations

import os
from pathlib import Path

# Locally generated secrets stay in their owner-provisioned host files, mounted
# read-only. An Environment copy would be a second, unmanaged source.
FILE_KEYS = frozenset(
    {
        "MEDIA_GATEWAY_TOKEN",
        "MEDIA_GATEWAY_DASHBOARD_PASSWORD_HASH",
        "MEDIA_GATEWAY_DASHBOARD_SESSION_KEY",
        "MEDIA_GATEWAY_PLEX_WEBHOOK_TOKEN",
        "CRBL_MEDIA_GATEWAY_TOKEN",
    }
)
UPSTREAM_KEYS = (
    "MCP_AUTH_TOKEN",
    "RADARR_URL",
    "RADARR_API_KEY",
    "SONARR_URL",
    "SONARR_API_KEY",
    "PLEX_URL",
    "PLEX_API_KEY",
)
SECRET_KEYS = frozenset(UPSTREAM_KEYS) | {"TELEGRAM_BOT_TOKEN"}
SECRET_PATHS = {
    "MEDIA_GATEWAY_UPSTREAM_TOKEN_FILE": "upstream.env",
    "MEDIA_GATEWAY_TELEGRAM_TOKEN_FILE": "telegram.env",
}


def single_line(value: str, minimum: int = 1) -> str:
    if len(value.strip()) < minimum or any(character in value for character in "\r\n\0"):
        raise ValueError("invalid runtime secret or option")
    return value


def write_private(path: Path, content: str) -> None:
    # A second launch must not reuse or overwrite residual files. A container
    # restart clears its tmpfs. Never follow a path planted by another process.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def project_gateway_files(source: dict[str, str], directory: Path) -> None:
    if not SECRET_KEYS.issubset(source):
        raise ValueError("incomplete runtime secret fields")
    for key in SECRET_KEYS:
        single_line(source[key])
    single_line(source["MCP_AUTH_TOKEN"], 32)
    # These files use the gateway's strict non-interpolating dotenv reader.
    # Outer quotes preserve embedded quotes and literal $, #, and = characters.
    files = {
        "upstream.env": "".join(f'{key}="{source[key]}"\n' for key in UPSTREAM_KEYS),
        "telegram.env": f'TELEGRAM_BOT_TOKEN="{source["TELEGRAM_BOT_TOKEN"]}"\n',
    }
    for filename, content in files.items():
        write_private(directory / filename, content)
