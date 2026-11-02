"""Prove native managed Hermes startup with synthetic data and no host mounts."""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

image = sys.argv[1]
suffix = uuid.uuid4().hex[:12]
fixture_image = f"media-hardening-fixture:{suffix}"
container = f"media-hardening-{suffix}"
root = Path(__file__).resolve().parents[1]


def docker(*args: str, check: bool = True, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["docker", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=root,
    )
    if check and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    if args[0] == "logs":
        return result.stdout + result.stderr
    return result.stdout


# Keep fixtures in a disposable image and anonymous data volume. Never bind a
# host directory, real credentials, or production data into a test container.
launcher_hook = (
    "import atexit, runpy, sys; atexit.register(lambda run=runpy.run_path, runtime=sys: "
    "run('/probe.py', run_name='launcher_check')['check_native_launcher']() "
    "if runtime.argv[0] == 'hermes' else None)"
)
dockerfile = f"""FROM {image}
COPY fake_op.sh /usr/local/bin/op
COPY hermes_gateway_stub.py /fixture/gateway_stub.py
COPY hermes_startup_probe.py /probe.py
COPY hermes_config_smoke.yaml /opt/data/config.yaml
COPY hermes_hardening_init /etc/cont-init.d/00-test-fixtures
RUN chmod 0755 /etc/cont-init.d/00-test-fixtures /usr/local/bin/op \\
    && mkdir -p /fixtures \\
    && printf '%s\\n' 'export TELEGRAM_BOT_TOKEN=fetched-telegram-token' \\
       'export CRBL_MEDIA_GATEWAY_URL=http://gateway:18082' > /fixtures/fetched.sh \\
    && printf '%s\\n' 'TELEGRAM_BOT_TOKEN=old-token' 'TELEGRAM_ALLOWED_USERS=10' \\
       'OPENROUTER_API_KEY=dummy-owner-provider-key' > /opt/data/.env \\
    && chown -R 1000:1000 /opt/data \\
    && printf '%s\\n' {shlex.quote(launcher_hook)} > \\
       /opt/hermes/.venv/lib/python3.14/site-packages/crbl_launcher_probe.pth
"""
try:
    docker("build", "--network", "none", "-t", fixture_image, "-f", "-", "tests", stdin=dockerfile)
    docker(
        "run",
        "--detach",
        "--name",
        container,
        "--network",
        "none",
        "--add-host",
        "gateway:127.0.0.1",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "DAC_OVERRIDE",
        "--cap-add",
        "FOWNER",
        "--cap-add",
        "SETUID",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "KILL",
        "--security-opt",
        "no-new-privileges:true",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=128m",
        "--tmpfs",
        "/run:rw,exec,nosuid,nodev,size=64m",
        "-e",
        "HERMES_UID=1000",
        "-e",
        "HERMES_GID=1000",
        "-e",
        "HERMES_HOME=/opt/data",
        "-e",
        "HERMES_DASHBOARD=false",
        fixture_image,
        "sh",
        "-c",
        "/opt/hermes/.venv/bin/python /probe.py && exec sleep infinity",
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        output = docker("logs", container)
        if "Native s6 managed startup and isolated credential checks passed." in output:
            break
        if "fatal:" in output or "PermissionError:" in output:
            raise RuntimeError(output)
        if docker("inspect", "--format", "{{.State.Running}}", container).strip() != "true":
            raise RuntimeError(output)
        time.sleep(0.25)
    else:
        raise RuntimeError(output)
    docker("stop", "--time", "10", container)
    exit_code = docker("inspect", "--format", "{{.State.ExitCode}}", container).strip()
    assert exit_code == "0", f"expected graceful exit 0, got {exit_code}"
    for line in output.splitlines():
        if line.startswith(("Native gateway process:", "Real Hermes CLI", "Restricted runtime:")):
            print(line)
    print("Read-only managed Hermes startup, native launcher and shutdown passed.")
finally:
    docker("rm", "--force", "--volumes", container, check=False)
    docker("image", "rm", fixture_image, check=False)
