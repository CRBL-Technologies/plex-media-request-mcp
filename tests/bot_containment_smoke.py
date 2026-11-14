"""Start the egress proxy from the real Compose file and prove the bot's network is sealed."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

image = sys.argv[1]
tests = Path(__file__).resolve().parent
compose = [
    "docker",
    "compose",
    "-p",
    f"media-contain-{uuid.uuid4().hex[:8]}",
    "--env-file",
    "/dev/null",
    "-f",
    str(tests.parent / "deployment" / "compose.managed.yaml"),
]
# Compose checks every required variable, also for the services this test never starts.
required = [
    "MEDIA_UPSTREAM_IMAGE",
    "UPSTREAM_ONEPASSWORD_FILE",
    "HERMES_DATA_DIR",
    "GATEWAY_STATE_DIR",
    "GATEWAY_ONEPASSWORD_FILE",
    "GATEWAY_TOKEN_FILE",
    "DASHBOARD_NETWORK",
    "DASHBOARD_ALLOWED_HOSTS",
    "PLEX_WEBHOOK_TOKEN_FILE",
    "BOT_ONEPASSWORD_FILE",
    "BOT_STATE_DIR",
    "MEDIA_BOT_MODEL",
]
environment = {**os.environ, **dict.fromkeys(required, "/unused"), "MEDIA_GATEWAY_IMAGE": image}


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, env=environment)
    if check and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result


def probe(network: str, expect: str) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "-v",
            f"{tests / 'bot_containment_probe.py'}:/probe.py:ro",
            "--entrypoint",
            "python",
            image,
            "/probe.py",
            host_address,
            str(listener.getsockname()[1]),
            expect,
        ],
        check=False,
    )


# The address this host uses to reach the outside, and a listener on every address.
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
    route.connect(("192.0.2.1", 9))
    host_address = route.getsockname()[0]
listener = socket.create_server(("0.0.0.0", 0))
try:
    run([*compose, "up", "-d", "--wait", "media-egress"])
    control = probe("media-egress", "open")
    assert control.returncode == 0, control.stdout + control.stderr
    sealed = probe("media-bot", "sealed")
    assert sealed.returncode == 0, sealed.stdout + sealed.stderr
    print("Bot network containment passed: only the egress proxy and its named hosts.")
finally:
    listener.close()
    run([*compose, "down", "--volumes", "--timeout", "5"], check=False)
