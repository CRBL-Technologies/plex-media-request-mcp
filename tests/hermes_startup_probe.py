"""Executed as the native Hermes main command after managed s6 initialization."""

import os
from pathlib import Path

from hermes_cli.env_loader import load_hermes_dotenv

assert not any(key.startswith("OP_") for key in os.environ)
load_hermes_dotenv(load_external_secrets=False)
assert os.environ["TELEGRAM_BOT_TOKEN"] == "fetched-telegram-token"
assert os.environ["CRBL_MEDIA_GATEWAY_URL"] == "http://gateway:18082"
assert "RADARR_API_KEY" not in os.environ
assert Path("/run/media-secrets/gateway.key").read_text().strip() == "g" * 40
assert "TELEGRAM_BOT_TOKEN=old-token" in Path("/opt/data/.env").read_text()
assert os.environ["TELEGRAM_ALLOWED_USERS"] == "10"
print("Native s6 managed startup and isolated credential checks passed.")
