"""Fetch only this media adapter's immutable inputs during native s6 startup."""

from media_gateway.managed_files import SECRET_KEYS, UPSTREAM_KEYS
from media_gateway.managed_start import run_managed

APP_KEYS = frozenset({"TELEGRAM_BOT_TOKEN", "CRBL_MEDIA_GATEWAY_URL", "CRBL_MEDIA_GATEWAY_TOKEN"})
FORBIDDEN_KEYS = (SECRET_KEYS | frozenset(UPSTREAM_KEYS) | {"TMDB_API_KEY"}) - APP_KEYS


def main() -> None:
    run_managed("hermes_media.managed_app", APP_KEYS | FORBIDDEN_KEYS)


if __name__ == "__main__":
    main()
