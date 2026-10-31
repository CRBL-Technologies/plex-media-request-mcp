from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from media_gateway.app import Runtime
from media_gateway.config import Config
from media_gateway.managed_app import application_environment
from media_gateway.managed_files import (
    FILE_KEYS,
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
    for key in FILE_KEYS:
        with pytest.raises(ValueError, match="over-scoped"):
            application_environment({**retrieved, key: "generated-value-in-environment"})
    assert environment["MEDIA_GATEWAY_UPSTREAM_URL"] == "fetched-value"
    assert environment["MEDIA_GATEWAY_RADARR_TAG_IDS"] == ""
    assert not any(key.startswith("OP_") for key in environment)
    assert not SECRET_KEYS.intersection(environment)


@pytest.mark.parametrize("stop_when", ["running", "backoff", "forced-stop"])
def test_managed_child_retries_without_reprojection_and_stops(
    tmp_path: Path, stop_when: str
) -> None:
    launcher = """
import os, signal, subprocess, sys, threading
from pathlib import Path
from media_gateway import managed_app
project = managed_app.project_gateway_files
directory = Path(sys.argv[1])
sources = []
def project_once(source, ignored):
    sources.append(source)
    assert len(sources) == 1
    project(source, directory)
managed_app.project_gateway_files = project_once
signal.signal(signal.SIGUSR1, lambda *_: None)
stop_when = sys.argv[2]
sys.argv = ['managed_app', str(os.getpid())]
real_popen = subprocess.Popen
children = []
wait = threading.Event.wait
delays = []
def fast_wait(event, timeout=None):
    if stop_when == 'running' and timeout is not None and timeout >= 1:
        delays.append(timeout)
        timeout = 0.01
    return wait(event, timeout)
threading.Event.wait = fast_wait
def check_spawn(args, **kwargs):
    assert args[1:] == ['-m', 'media_gateway.app']
    assert sources == [{}], 'fetched secret dictionary was retained'
    assert not any(key.startswith('OP_') for key in os.environ)
    probe = '''
import os, signal, sys, time
assert not any(key.startswith('OP_') for key in os.environ)
assert 'MEDIA_GATEWAY_CONFIG_FILE' not in os.environ
assert 'MEDIA_GATEWAY_TOKEN' not in os.environ
assert 'TELEGRAM_BOT_TOKEN' not in os.environ
assert os.environ['MEDIA_GATEWAY_RADARR_PROFILE_ID'] == '25'
assert os.environ['MEDIA_GATEWAY_POLICY_FILE'] == '/unchanged/hermes.env'
print('application stderr survives', file=sys.stderr, flush=True)
attempt, stop_when = int(sys.argv[1]), sys.argv[2]
crashes = {'running': 7, 'backoff': 1, 'forced-stop': 1}[stop_when]
if attempt <= crashes:
    sys.exit(1)
def stopped(signum, frame):
    if stop_when == 'forced-stop':
        return
    print('child received stop', flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, stopped)
os.kill(os.getppid(), signal.SIGTERM)
while True:
    time.sleep(0.1)
'''
    child = real_popen([sys.executable, '-c', probe, str(len(children) + 1), stop_when], **kwargs)
    children.append(child)
    return child
subprocess.Popen = check_spawn
# The old wrapper execs the application once. Keep that regression path
# synthetic too, so the failing test cannot launch a real gateway.
real_exec = os.execve
def old_exec(executable, args, environment):
    real_exec(executable, [executable, '-c',
        "import sys; print('application stderr survives', flush=True); sys.exit(1)"], environment)
os.execve = old_exec
result = managed_app.main()
assert children and all(child.returncode is not None for child in children)
if stop_when != 'backoff':
    assert len(children) == {'running': 8, 'forced-stop': 2}[stop_when]
if stop_when == 'running':
    assert delays == [1, 2, 4, 8, 16, 30, 30]
if stop_when == 'forced-stop':
    assert children[-1].returncode == -signal.SIGKILL
sys.exit(result)
"""
    environment = dict(os.environ)
    environment.update(secret_values())
    environment.update({key: f"/run/media-secrets/{name}" for key, name in SECRET_PATHS.items()})
    environment.update(
        {
            "MEDIA_GATEWAY_TOKEN_FILE": "/run/media-keys/gateway.key",
            "MEDIA_GATEWAY_DASHBOARD_PASSWORD_HASH_FILE": "/run/media-keys/dashboard-password.hash",
            "MEDIA_GATEWAY_DASHBOARD_SESSION_KEY_FILE": "/run/media-keys/dashboard-session.key",
            "MEDIA_GATEWAY_PLEX_WEBHOOK_TOKEN_FILE": "/run/media-keys/plex-webhook.key",
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
    process = subprocess.Popen(
        [sys.executable, "-c", launcher, str(tmp_path), stop_when],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if stop_when == "backoff":
            assert process.stdout is not None
            output = ""
            deadline = time.monotonic() + 5
            while "retrying in 1 seconds" not in output:
                remaining = deadline - time.monotonic()
                assert remaining > 0 and select.select([process.stdout], [], [], remaining)[0]
                chunk = os.read(process.stdout.fileno(), 8192)
                assert chunk, output
                output += chunk.decode()
            assert "application stderr survives" in output
            process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, stdout + stderr
        if stop_when == "running":
            assert stdout.count("application stderr survives") == 8
            assert "child received stop" in stdout
        assert stderr == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["telegram.env", "upstream.env"]
    assert (tmp_path / "upstream.env").stat().st_mode & 0o777 == 0o600
    environment["MEDIA_GATEWAY_RADARR_PROFILE_ID"] = "invalid-sensitive-value"
    failed = subprocess.run(
        [sys.executable, "-c", launcher, str(tmp_path), stop_when],
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
    (tmp_path / "upstream.env").symlink_to(protected)
    with pytest.raises(OSError):
        project_gateway_files(secret_values(), tmp_path)
    assert protected.read_text() == "unchanged"
