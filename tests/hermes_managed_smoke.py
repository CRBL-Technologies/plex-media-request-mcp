"""Run with the pinned Hermes Python; no pytest or live account is needed."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hermes_cli.env_loader import load_hermes_dotenv

from hermes_media.managed_app import project_hermes_files

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    user, managed, secrets = (root / name for name in ("user", "managed", "secrets"))
    for directory in (user, managed, secrets):
        directory.mkdir()
    policy = user / ".env"
    policy.write_text("TELEGRAM_BOT_TOKEN=old-token\nTELEGRAM_ALLOWED_USERS=10\n")
    config = user / "config.yaml"
    config.write_text("model:\n  default: user-selected-model\n")
    project_hermes_files(
        {
            "TELEGRAM_BOT_TOKEN": "fetched-telegram-token",
            "CRBL_MEDIA_GATEWAY_URL": "http://gateway:18082",
            "CRBL_MEDIA_GATEWAY_TOKEN": "fetched-gateway-key-with-more-than-thirty-two-bytes",
        },
        secrets,
        managed,
    )
    os.environ["HERMES_HOME"] = str(user)
    os.environ["HERMES_MANAGED_DIR"] = str(managed)
    os.environ["TELEGRAM_BOT_TOKEN"] = "old-export"
    before = policy.read_text()
    load_hermes_dotenv(hermes_home=user, load_external_secrets=False)
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "fetched-telegram-token"
    assert os.environ["TELEGRAM_ALLOWED_USERS"] == "10"
    assert policy.read_text() == before
    assert config.read_text() == "model:\n  default: user-selected-model\n"
    policy.write_text(before.replace("USERS=10", "USERS=10,20"))
    load_hermes_dotenv(hermes_home=user, load_external_secrets=False)
    assert os.environ["TELEGRAM_ALLOWED_USERS"] == "10,20"
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "fetched-telegram-token"
    assert (secrets / "gateway.key").stat().st_mode & 0o777 == 0o600
    assert (managed / ".env").stat().st_mode & 0o777 == 0o600
print("Native Hermes managed secrets and live policy checks passed.")
