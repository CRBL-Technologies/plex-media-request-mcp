from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from media_gateway.app import Runtime
from media_gateway.config import Config
from media_gateway.managed_app import application_environment
from media_gateway.managed_files import (
    SECRET_KEYS,
    SECRET_PATHS,
    UPSTREAM_KEYS,
    project_gateway_files,
)
from media_gateway.managed_start import RUNTIME_KEYS, read_bootstrap, retrieval_environment
from media_gateway.secrets import read_dotenv


def secret_values() -> dict[str, str]:
    return dict.fromkeys(SECRET_KEYS, "dummy-secret-with-more-than-thirty-two-bytes")


def test_bootstrap_rejects_extra_duplicate_and_unsafe_files(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.json"
    data = {"environment_id": "a" * 26, "service_account_token": "dummy-token"}
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    assert read_bootstrap(path) == ("a" * 26, "dummy-token")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        read_bootstrap(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_bootstrap(link)
    path.write_text(json.dumps({**data, "extra": "rejected"}))
    with pytest.raises(ValueError, match="fields"):
        read_bootstrap(path)
    path.write_text(json.dumps(data)[:-1] + ', "environment_id": "duplicate"}')
    with pytest.raises(ValueError, match="duplicate"):
        read_bootstrap(path)


def test_inherited_app_values_cannot_supply_missing_fetched_options() -> None:
    inherited = dict.fromkeys(RUNTIME_KEYS, "old-value")
    inherited.update(
        {
            "MEDIA_GATEWAY_CONFIG_FILE": "/old/gateway.env",
            "MEDIA_GATEWAY_PORT": "18082",
            "MEDIA_GATEWAY_POLICY_FILE": "/data/hermes.env",
            "MEDIA_GATEWAY_TOKEN_FILE": "/secrets/gateway.key",
            "OP_SESSION_OLD": "old-session",
        }
    )
    retrieved = retrieval_environment(inherited, "dummy-token", RUNTIME_KEYS)
    assert not RUNTIME_KEYS.intersection(retrieved)
    assert "MEDIA_GATEWAY_CONFIG_FILE" not in retrieved
    assert "OP_SESSION_OLD" not in retrieved
    assert retrieved["MEDIA_GATEWAY_PORT"] == "18082"
    assert retrieved["MEDIA_GATEWAY_POLICY_FILE"] == "/data/hermes.env"
    assert retrieved["MEDIA_GATEWAY_TOKEN_FILE"] == "/secrets/gateway.key"
    with pytest.raises(ValueError, match="incomplete"):
        application_environment(retrieved)
    retrieved.update(dict.fromkeys(RUNTIME_KEYS, "fetched-value"))
    retrieved["MEDIA_GATEWAY_RADARR_TAG_IDS"] = ""
    retrieved["MEDIA_GATEWAY_SONARR_TAG_IDS"] = ""
    environment = application_environment(retrieved)
    assert environment["MEDIA_GATEWAY_UPSTREAM_URL"] == "fetched-value"
    assert environment["MEDIA_GATEWAY_RADARR_TAG_IDS"] == ""
    assert not any(key.startswith("OP_") for key in environment)
    assert not SECRET_KEYS.intersection(environment)


def test_managed_child_exec_removes_bootstrap_and_keeps_app_stderr(tmp_path: Path) -> None:
    launcher = """
import os, signal, sys
from pathlib import Path
from media_gateway import managed_app
project = managed_app.project_gateway_files
directory = Path(sys.argv[1])
managed_app.project_gateway_files = lambda source, ignored: project(source, directory)
signal.signal(signal.SIGUSR1, lambda *_: None)
sys.argv = ['managed_app', str(os.getpid())]
real_exec = os.execve
def check_exec(executable, args, environment):
    assert args[1:] == ['-m', 'media_gateway.app']
    probe = '''
import os, sys
assert not any(key.startswith('OP_') for key in os.environ)
assert 'MEDIA_GATEWAY_CONFIG_FILE' not in os.environ
assert 'MEDIA_GATEWAY_TOKEN' not in os.environ
assert 'TELEGRAM_BOT_TOKEN' not in os.environ
assert os.environ['MEDIA_GATEWAY_RADARR_PROFILE_ID'] == '25'
assert os.environ['MEDIA_GATEWAY_POLICY_FILE'] == '/unchanged/hermes.env'
print('application stderr survives', file=sys.stderr)
'''
    real_exec(executable, [executable, '-c', probe], environment)
managed_app.os.execve = check_exec
sys.exit(managed_app.main())
"""
    environment = dict(os.environ)
    environment.update(secret_values())
    environment.update({key: f"/run/media-secrets/{name}" for key, name in SECRET_PATHS.items()})
    environment.update(
        {
            "MEDIA_GATEWAY_UPSTREAM_URL": "http://upstream:3000",
            "MEDIA_GATEWAY_SECURE_COOKIES": "false",
            "MEDIA_GATEWAY_RADARR_PROFILE_ID": "25",
            "MEDIA_GATEWAY_RADARR_ROOT": "/data/movies",
            "MEDIA_GATEWAY_RADARR_TAG_IDS": "",
            "MEDIA_GATEWAY_SONARR_PROFILE_ID": "26",
            "MEDIA_GATEWAY_SONARR_ANIME_PROFILE_ID": "27",
            "MEDIA_GATEWAY_SONARR_ROOT": "/data/tv",
            "MEDIA_GATEWAY_SONARR_TAG_IDS": "",
            "MEDIA_GATEWAY_PLEX_MACHINE_ID": "dummy-machine",
            "MEDIA_GATEWAY_NOTIFICATION_DELAY_SECONDS": "5",
            "MEDIA_GATEWAY_TELEGRAM_IDENTITY_SYNC": "false",
            "MEDIA_GATEWAY_POLICY_FILE": "/unchanged/hermes.env",
            "MEDIA_GATEWAY_CONFIG_FILE": "/not-readable/old-gateway.env",
            "OP_SERVICE_ACCOUNT_TOKEN": "dummy-bootstrap-token",
            "OP_SESSION_OLD": "dummy-session",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", launcher, str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert "application stderr survives" in result.stdout
    assert result.stderr == ""
    assert (tmp_path / "gateway.key").stat().st_mode & 0o777 == 0o600
    environment["MEDIA_GATEWAY_RADARR_PROFILE_ID"] = "invalid-sensitive-value"
    failed = subprocess.run(
        [sys.executable, "-c", launcher, str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert failed.returncode == 1
    assert "incomplete or invalid" in failed.stdout
    assert "invalid-sensitive-value" not in failed.stdout + failed.stderr


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_supervisor_signals_logs_and_single_fetch(tmp_path: Path, outcome: str) -> None:
    """Exercise actual processes while replacing only the external CLI transport."""
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_text(
        json.dumps({"environment_id": "a" * 26, "service_account_token": "dummy-token"})
    )
    bootstrap.chmod(0o600)
    events = tmp_path / "events"
    child_code = """
import os, signal, sys, time
from pathlib import Path
events = Path(sys.argv[1])
outcome = sys.argv[2]
events.write_text('fetched\\n')
assert 'MEDIA_GATEWAY_RADARR_PROFILE_ID' not in os.environ
assert 'MEDIA_GATEWAY_CONFIG_FILE' not in os.environ
print('dummy CLI diagnostic', file=sys.stderr, flush=True)
if outcome == 'failure':
    sys.exit(1)
def stop(signum, frame):
    with events.open('a') as stream:
        stream.write('stopped\\n')
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
if outcome == 'success':
    os.kill(os.getppid(), signal.SIGUSR1)
    os.dup2(1, 2)
    print('application log is preserved', file=sys.stderr, flush=True)
while True:
    time.sleep(0.1)
"""
    launcher = """
import subprocess, sys
from pathlib import Path
from media_gateway import managed_start
managed_start.BOOTSTRAP_FILE = Path(sys.argv[1])
managed_start.STARTUP_TIMEOUT = 0.3
real_popen = subprocess.Popen
def fake_op(command, **kwargs):
    assert command[1:3] == ['run', '--environment']
    assert '--no-masking' not in command
    return real_popen([sys.executable, '-c', sys.argv[2], sys.argv[3], sys.argv[4]], **kwargs)
managed_start.subprocess.Popen = fake_op
managed_start.main()
"""
    environment = dict(os.environ)
    environment["MEDIA_GATEWAY_RADARR_PROFILE_ID"] = "old-value"
    environment["MEDIA_GATEWAY_CONFIG_FILE"] = "/old/gateway.env"
    process = subprocess.Popen(
        [sys.executable, "-c", launcher, str(bootstrap), child_code, str(events), outcome],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        import time

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if events.exists():
                if outcome == "success" or "stopped" in events.read_text():
                    break
                if outcome == "failure":
                    break
            time.sleep(0.02)
        assert events.exists()
        # Failure and timeout park without another fetch. A successful app must
        # continue after the retrieval deadline until the stop signal arrives.
        time.sleep(0.5)
        assert process.poll() is None
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0
        assert events.read_text().count("fetched") == 1
        assert "dummy CLI diagnostic" not in stdout + stderr
        if outcome == "success":
            assert "application log is preserved" in stdout
            assert "Managed media startup stopped" not in stderr
            assert "stopped" in events.read_text()
        else:
            assert "Managed media startup stopped" in stderr
            if outcome == "timeout":
                assert "stopped" in events.read_text()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_gateway_files_preserve_literal_values_and_mutable_policy(
    config: Config, tmp_path: Path
) -> None:
    directory = tmp_path / "projected"
    directory.mkdir()
    source = secret_values()
    source["PLEX_API_KEY"] = 'literal$#=with"quotes"'
    source["TELEGRAM_BOT_TOKEN"] = "fetched-telegram-token"
    project_gateway_files(source, directory)
    assert (
        read_dotenv(directory / "upstream.env", set(UPSTREAM_KEYS))["PLEX_API_KEY"]
        == source["PLEX_API_KEY"]
    )
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())
    managed = replace(config, telegram_token_file=directory / "telegram.env")
    runtime = Runtime(managed)
    original = config.policy_file.read_text()
    assert runtime.notifications._telegram_token() == "fetched-telegram-token"
    runtime.policy.set_allowed(42, allowed=True)
    assert runtime.policy.snapshot().role(42).value == "user"
    assert "TELEGRAM_BOT_TOKEN=test-token" in config.policy_file.read_text()
    assert config.policy_file.read_text() != original
    assert runtime.notifications._telegram_token() == "fetched-telegram-token"
    assert Runtime(config).notifications._telegram_token() == "test-token"
    (directory / "telegram.env").unlink()
    with pytest.raises(FileNotFoundError):
        runtime.notifications._telegram_token()


@pytest.mark.parametrize("missing", sorted(SECRET_KEYS))
def test_gateway_projection_requires_every_secret(tmp_path: Path, missing: str) -> None:
    source = secret_values()
    del source[missing]
    with pytest.raises(ValueError, match="incomplete"):
        project_gateway_files(source, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_gateway_projection_rejects_injection_and_existing_files(tmp_path: Path) -> None:
    source = secret_values()
    source["RADARR_API_KEY"] = "key\nOTHER=bad"
    with pytest.raises(ValueError):
        project_gateway_files(source, tmp_path)
    assert list(tmp_path.iterdir()) == []
    protected = tmp_path / "existing"
    protected.write_text("unchanged")
    (tmp_path / "gateway.key").symlink_to(protected)
    with pytest.raises(OSError):
        project_gateway_files(secret_values(), tmp_path)
    assert protected.read_text() == "unchanged"
