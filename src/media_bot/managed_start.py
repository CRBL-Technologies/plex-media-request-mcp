"""Fetch only this bot's inputs from its 1Password Environment at startup."""

from media_gateway.managed_files import FILE_KEYS, SECRET_KEYS, UPSTREAM_KEYS
from media_gateway.managed_start import run_managed

APP_KEYS = frozenset({"TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY", "CRBL_MEDIA_GATEWAY_URL"})
FORBIDDEN_KEYS = (SECRET_KEYS | FILE_KEYS | frozenset(UPSTREAM_KEYS) | {"TMDB_API_KEY"}) - APP_KEYS


def main() -> None:
    run_managed("media_bot.managed_app", APP_KEYS | FORBIDDEN_KEYS)


if __name__ == "__main__":
    main()
