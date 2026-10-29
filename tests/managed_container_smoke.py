"""Credential-free startup proof for three managed images; never deploys a stack."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from media_gateway.managed_files import SECRET_PATHS

gateway_image, hermes_image, upstream_image = sys.argv[1:]
tests = Path(__file__).resolve().parent
suffix = uuid.uuid4().hex[:12]
containers: list[str] = []
network = f"media-managed-smoke-{suffix}"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
    if check and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result


def fixture(root: Path, name: str, values: dict[str, str]) -> list[str]:
    directory = root / name
    directory.mkdir(mode=0o755)
    (directory / "bootstrap.json").write_text(
        json.dumps({"environment_id": "a" * 26, "service_account_token": "dummy-token"})
    )
    (directory / "bootstrap.json").chmod(0o600)
    (directory / "fetched.sh").write_text(
        "".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items())
    )
    docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0",
        "--entrypoint",
        "python",
        "-v",
        f"{directory}:/fixtures",
        gateway_image,
        "-c",
        "import os; os.chown('/fixtures/bootstrap.json', 1000, 1000)",
    )
    return [
        "-v",
        f"{directory}:/fixtures:ro",
        "-v",
        f"{directory / 'bootstrap.json'}:/run/secrets/onepassword.json:ro",
        "-v",
        f"{tests / 'fake_op.sh'}:/usr/local/bin/op:ro",
    ]


def daemon(image: str, args: list[str], probe: list[str]) -> None:
    name = f"media-managed-{len(containers)}-{suffix}"
    containers.append(name)
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs",
        "/run/media-secrets:rw,noexec,nosuid,nodev,size=1m,uid=1000,gid=1000,mode=0700",
        *args,
        image,
    )
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if docker("exec", name, *probe, check=False).returncode == 0:
            break
        time.sleep(0.25)
    else:
        raise RuntimeError(docker("logs", name).stdout + docker("logs", name).stderr)
    docker("stop", "--time", "8", name)
    assert docker("inspect", "--format", "{{.State.ExitCode}}", name).stdout.strip() == "0"


with tempfile.TemporaryDirectory(prefix="media-managed-smoke-") as temporary:
    root = Path(temporary)
    root.chmod(0o755)
    try:
        upstream = {
            "MCP_AUTH_TOKEN": "m" * 40,
            "RADARR_URL": "http://127.0.0.1:9",
            "RADARR_API_KEY": "r" * 40,
            "SONARR_URL": "http://127.0.0.1:9",
            "SONARR_API_KEY": "s" * 40,
            "PLEX_URL": "http://127.0.0.1:9",
            "PLEX_API_KEY": "p" * 40,
            "TMDB_API_KEY": "t" * 40,
            "TOOL_PROFILE": "full",
        }
        gateway = {
            key: value
            for key, value in upstream.items()
            if key not in {"TMDB_API_KEY", "TOOL_PROFILE"}
        }
        gateway.update(
            {
                "MEDIA_GATEWAY_TOKEN": "g" * 40,
                "MEDIA_GATEWAY_DASHBOARD_PASSWORD_HASH": "dummy-hash",
                "MEDIA_GATEWAY_DASHBOARD_SESSION_KEY": "d" * 40,
                "MEDIA_GATEWAY_PLEX_WEBHOOK_TOKEN": "w" * 40,
                "TELEGRAM_BOT_TOKEN": "fetched-telegram-token",
                "MEDIA_GATEWAY_UPSTREAM_URL": "http://127.0.0.1:9",
                "MEDIA_GATEWAY_SECURE_COOKIES": "false",
                "MEDIA_GATEWAY_RADARR_PROFILE_ID": "25",
                "MEDIA_GATEWAY_RADARR_ROOT": "/movies",
                "MEDIA_GATEWAY_RADARR_TAG_IDS": "",
                "MEDIA_GATEWAY_SONARR_PROFILE_ID": "26",
                "MEDIA_GATEWAY_SONARR_ANIME_PROFILE_ID": "27",
                "MEDIA_GATEWAY_SONARR_ROOT": "/tv",
                "MEDIA_GATEWAY_SONARR_TAG_IDS": "",
                "MEDIA_GATEWAY_PLEX_MACHINE_ID": "dummy-machine",
                "MEDIA_GATEWAY_NOTIFICATION_DELAY_SECONDS": "5",
                "MEDIA_GATEWAY_TELEGRAM_IDENTITY_SYNC": "false",
            }
        )
        gateway_args = fixture(root, "gateway", gateway)
        policy = root / "policy.env"
        policy.write_text("TELEGRAM_BOT_TOKEN=old-token\nTELEGRAM_ALLOWED_USERS=10\n")
        gateway_args += [
            "-v",
            f"{policy}:/policy.env:ro",
            "--tmpfs",
            "/opt/data/state:uid=1000,gid=1000",
            "-e",
            "MEDIA_GATEWAY_POLICY_FILE=/policy.env",
            "-e",
            "MEDIA_GATEWAY_RADARR_PROFILE_ID=999",
        ]
        for key, filename in SECRET_PATHS.items():
            gateway_args += ["-e", f"{key}=/run/media-secrets/{filename}"]
        daemon(
            gateway_image,
            gateway_args,
            [
                "python",
                "-c",
                "import urllib.request; "
                "urllib.request.urlopen('http://127.0.0.1:18082/healthz', timeout=1)",
            ],
        )
        assert "TELEGRAM_BOT_TOKEN=old-token" in policy.read_text()
        print("Managed gateway startup and shutdown passed.")

        daemon(
            upstream_image,
            fixture(root, "upstream", upstream),
            [
                "deno",
                "eval",
                "const r = await fetch('http://127.0.0.1:3000/health'); if (!r.ok) Deno.exit(1)",
            ],
        )
        print("Managed native upstream startup and shutdown passed.")

        docker("network", "create", "--internal", network)
        stub = f"media-managed-stub-{suffix}"
        containers.append(stub)
        docker(
            "run",
            "-d",
            "--rm",
            "--name",
            stub,
            "--network",
            network,
            "--network-alias",
            "gateway",
            "--entrypoint",
            "python",
            "-v",
            f"{tests / 'hermes_gateway_stub.py'}:/stub.py:ro",
            gateway_image,
            "/stub.py",
        )
        hermes_args = fixture(
            root,
            "hermes",
            {
                "TELEGRAM_BOT_TOKEN": "fetched-telegram-token",
                "CRBL_MEDIA_GATEWAY_URL": "http://gateway:18082",
                "CRBL_MEDIA_GATEWAY_TOKEN": "g" * 40,
            },
        )
        data = root / "data"
        data.mkdir(mode=0o777)
        data.chmod(0o777)
        (data / "config.yaml").write_bytes((tests / "hermes_config_smoke.yaml").read_bytes())
        (data / ".env").write_text("TELEGRAM_BOT_TOKEN=old-token\nTELEGRAM_ALLOWED_USERS=10\n")
        hermes_name = f"media-managed-hermes-{suffix}"
        containers.append(hermes_name)
        result = docker(
            "run",
            "--rm",
            "--name",
            hermes_name,
            "--network",
            network,
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
            "-e",
            "OP_SERVICE_ACCOUNT_TOKEN=stale-dummy-token",
            "-e",
            "OP_SESSION_OLD=stale-dummy-session",
            "-v",
            f"{data}:/opt/data",
            "-v",
            f"{tests / 'hermes_startup_probe.py'}:/probe.py:ro",
            *hermes_args,
            hermes_image,
            "/opt/hermes/.venv/bin/python",
            "/probe.py",
        )
        assert "Native s6 managed startup" in result.stdout
        print("Managed native Hermes s6 startup and isolated credentials passed.")
    finally:
        for container in containers:
            docker("rm", "-f", container, check=False)
        docker("network", "rm", network, check=False)
        # Native Hermes creates owner-only subdirectories as UID 1000. Remove
        # only this test's generated data before the host temp-dir cleanup.
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0",
            "--entrypoint",
            "python",
            "-v",
            f"{root}:/fixtures",
            gateway_image,
            "-c",
            "import shutil; shutil.rmtree('/fixtures/data', ignore_errors=True)",
            check=False,
        )
