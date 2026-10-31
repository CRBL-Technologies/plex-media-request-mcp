"""Run with the pinned Hermes Python; no pytest or live account is needed."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hermes_cli.env_loader import load_hermes_dotenv
from pm.pip_check import unexpected_incompatibilities

from hermes_media.managed_app import project_hermes_files

lock = Path("/opt/hermes/uv.lock")
expected = (
    "Found 1 incompatibility\n"
    "The package `browser-harness` requires `pillow==12.3.0`, but `13.0.0` is installed\n"
)
assert unexpected_incompatibilities(expected, lock) == []
assert unexpected_incompatibilities(expected.split("\n", 1)[1], lock) is None
assert unexpected_incompatibilities(expected.replace("13.0.0", "12.0.0"), lock)
assert (
    unexpected_incompatibilities(
        expected.replace("Found 1 incompatibility", "Found 2 incompatibilities"), lock
    )
    is None
)

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    user, managed = (root / name for name in ("user", "managed"))
    for directory in (user, managed):
        directory.mkdir()
    policy = user / ".env"
    policy.write_text("TELEGRAM_BOT_TOKEN=old-token\nTELEGRAM_ALLOWED_USERS=10\n")
    config = user / "config.yaml"
    config.write_text("model:\n  default: user-selected-model\n")
    project_hermes_files(
        {
            "TELEGRAM_BOT_TOKEN": "fetched-telegram-token",
            "CRBL_MEDIA_GATEWAY_URL": "http://gateway:18082",
        },
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
    assert os.environ["CRBL_MEDIA_GATEWAY_TOKEN_FILE"] == "/run/media-keys/gateway.key"
    assert (managed / ".env").stat().st_mode & 0o777 == 0o600
print("Native Hermes managed secrets and live policy checks passed.")
print("Native lock overrides reject unknown versions and incomplete reports.")
