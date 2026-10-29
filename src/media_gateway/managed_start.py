"""Mandatory startup retrieval for the separately built managed gateway image."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from types import FrameType

from .managed_files import SECRET_KEYS

BOOTSTRAP_FILE = Path("/run/secrets/onepassword.json")
APP_KEYS = frozenset(
    {
        "MEDIA_GATEWAY_UPSTREAM_URL",
        "MEDIA_GATEWAY_SECURE_COOKIES",
        "MEDIA_GATEWAY_RADARR_PROFILE_ID",
        "MEDIA_GATEWAY_RADARR_ROOT",
        "MEDIA_GATEWAY_RADARR_TAG_IDS",
        "MEDIA_GATEWAY_SONARR_PROFILE_ID",
        "MEDIA_GATEWAY_SONARR_ANIME_PROFILE_ID",
        "MEDIA_GATEWAY_SONARR_ROOT",
        "MEDIA_GATEWAY_SONARR_TAG_IDS",
        "MEDIA_GATEWAY_PLEX_MACHINE_ID",
        "MEDIA_GATEWAY_NOTIFICATION_DELAY_SECONDS",
        "MEDIA_GATEWAY_TELEGRAM_IDENTITY_SYNC",
    }
)
STARTUP_TIMEOUT = 60
RUNTIME_KEYS = APP_KEYS | SECRET_KEYS


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate bootstrap field")
        result[key] = value
    return result


def read_bootstrap(path: Path) -> tuple[str, str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("bootstrap must be an owner-only regular file")
        content = stream.read(16385)
    if len(content) > 16384:
        raise ValueError("bootstrap exceeds size limit")
    data = json.loads(content, object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or set(data) != {"environment_id", "service_account_token"}:
        raise ValueError("invalid bootstrap fields")
    environment_id = data["environment_id"]
    token = data["service_account_token"]
    if not isinstance(environment_id, str) or not re.fullmatch(r"[a-z2-7]{26}", environment_id):
        raise ValueError("invalid Environment ID")
    if (
        not isinstance(token, str)
        or not token
        or any(character.isspace() or ord(character) < 32 for character in token)
    ):
        raise ValueError("invalid bootstrap token")
    return environment_id, token


def retrieval_environment(
    source: dict[str, str], token: str, keys: frozenset[str]
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in source.items()
        if not key.startswith("OP_") and key not in keys and key != "MEDIA_GATEWAY_CONFIG_FILE"
    }
    environment["OP_SERVICE_ACCOUNT_TOKEN"] = token
    environment["OP_CONFIG_DIR"] = "/tmp/onepassword"
    return environment


def run_managed(module: str, keys: frozenset[str]) -> None:
    stopped = threading.Event()
    ready = threading.Event()
    child: subprocess.Popen[bytes] | None = None

    def forward(signum: int, _frame: FrameType | None) -> None:
        stopped.set()
        if child is not None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signum)

    def started(_signum: int, _frame: FrameType | None) -> None:
        ready.set()

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGUSR1, started)
    try:
        environment_id, token = read_bootstrap(BOOTSTRAP_FILE)
        child = subprocess.Popen(
            [
                "/usr/local/bin/op",
                "run",
                "--environment",
                environment_id,
                "--",
                sys.executable,
                "-m",
                module,
                str(os.getpid()),
            ],
            env=retrieval_environment(dict(os.environ), token, keys),
            stdin=subprocess.DEVNULL,
            # CLI errors can include identifiers. App stderr is redirected to
            # stdout in managed_app and still passes through op's masking.
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while child.poll() is None and not stopped.is_set():
            if not ready.is_set() and time.monotonic() >= deadline:
                break
            stopped.wait(0.1)
        if child.poll() == 0 and ready.is_set():
            return
    except (OSError, ValueError):
        pass
    finally:
        if child is not None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    if not stopped.is_set():
        print(
            "Managed media startup stopped. Check health and configuration; restart to retry.",
            file=sys.stderr,
            flush=True,
        )
        # One attempt per container start prevents an automatic restart loop
        # from exhausting the account's shared request quota.
        stopped.wait()


def main() -> None:
    run_managed("media_gateway.managed_app", RUNTIME_KEYS)


if __name__ == "__main__":
    main()
