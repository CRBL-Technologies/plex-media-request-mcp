"""Project the fetched bot secrets into one private file, then supervise the bot."""

from __future__ import annotations

import sys
from pathlib import Path

from media_gateway.managed_app import supervise
from media_gateway.managed_files import single_line, write_private

from .bot import SECRET_KEYS, Settings
from .managed_start import APP_KEYS, FORBIDDEN_KEYS

SECRETS_FILE = Path("/run/media-secrets/bot.env")


def project_bot_files(source: dict[str, str], secrets_file: Path) -> dict[str, str]:
    if not APP_KEYS.issubset(source) or FORBIDDEN_KEYS.intersection(source):
        raise ValueError("incomplete or over-scoped bot Environment")
    values = {key: single_line(source[key]) for key in APP_KEYS}
    environment = {
        key: value
        for key, value in source.items()
        if not key.startswith("OP_") and key not in APP_KEYS
    }
    environment["CRBL_MEDIA_GATEWAY_URL"] = values["CRBL_MEDIA_GATEWAY_URL"]
    environment["MEDIA_BOT_SECRETS_FILE"] = str(secrets_file)
    Settings.from_env()  # Fail before the loader is told startup succeeded.
    # The strict dotenv reader strips one pair of outer quotes and nothing else.
    write_private(secrets_file, "".join(f'{key}="{values[key]}"\n' for key in sorted(SECRET_KEYS)))
    return environment


def main() -> int:
    return supervise(
        lambda source: project_bot_files(source, SECRETS_FILE),
        "media_bot.bot",
        "bot",
        frozenset(SECRET_KEYS),
    )


if __name__ == "__main__":
    sys.exit(main())
