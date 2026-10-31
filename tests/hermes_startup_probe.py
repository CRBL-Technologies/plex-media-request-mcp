"""Probe native managed startup and the real launcher using disposable state."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def check_native_launcher() -> None:
    """Observe imports and dependency selection without registering anything."""
    from pm.environments import selected_venv, site_packages

    from hermes_media import plugin

    assert Path(plugin.__file__).parent == Path("/opt/hermes/hermes_media")
    assert "/app/src" not in sys.path
    selected = selected_venv(Path("/opt/hermes"))
    assert str(site_packages(selected)) in sys.path
    print(f"CRBL_LAUNCHER_PROOF:{selected.name}")


def check_launcher() -> str:
    result = subprocess.run(
        ["/opt/hermes/.venv/bin/hermes", "tools", "list", "--platform", "telegram"],
        env={**os.environ, "PYTHONPATH": "/not-the-crbl-source"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CRBL_LAUNCHER_PROOF:" in result.stdout, result.stdout + result.stderr
    return result.stdout


def main() -> None:
    from gateway.config import Platform, load_gateway_config
    from hermes_cli.config import load_config_readonly
    from hermes_cli.env_loader import load_hermes_dotenv
    from pm.environments import install_state_dir, runtime_facts_path, site_packages

    assert not any(key.startswith("OP_") for key in os.environ)
    load_hermes_dotenv(load_external_secrets=False)
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "fetched-telegram-token"
    assert os.environ["CRBL_MEDIA_GATEWAY_URL"] == "http://gateway:18082"
    assert "RADARR_API_KEY" not in os.environ
    assert os.environ["CRBL_MEDIA_GATEWAY_TOKEN_FILE"] == "/run/media-keys/gateway.key"
    assert Path("/run/media-keys/gateway.key").read_text().strip() == "g" * 40
    assert "CRBL_MEDIA_GATEWAY_TOKEN" not in os.environ
    assert "TELEGRAM_BOT_TOKEN=old-token" in Path("/opt/data/.env").read_text()
    assert os.environ["TELEGRAM_ALLOWED_USERS"] == "10"
    assert os.environ["OPENROUTER_API_KEY"] == "dummy-owner-provider-key"
    assert "OPENROUTER_API_KEY=dummy-owner-provider-key" in Path("/opt/data/.env").read_text()
    config = load_config_readonly()
    assert config["model"]["default"] == "owner-selected-model"
    assert config["model"]["provider"] == "openrouter"
    telegram = load_gateway_config().platforms[Platform.TELEGRAM]
    assert telegram.enabled
    assert telegram.token == "fetched-telegram-token"

    home = Path("/opt/data")
    before = {name: (home / name).read_bytes() for name in (".env", "config.yaml")}
    assert "CRBL_LAUNCHER_PROOF:.venv" in check_launcher()
    gateway = subprocess.Popen(
        ["/opt/hermes/.venv/bin/hermes", "gateway", "run", "--replace"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert gateway.poll() is None, "native gateway launcher exited before its process check"
            argv = Path(f"/proc/{gateway.pid}/cmdline").read_bytes().decode().split("\0")[:-1]
            if argv[:3] == ["/opt/hermes/.venv/bin/python", "-P", "-c"]:
                assert argv[-3:] == ["gateway", "run", "--replace"]
                matches = subprocess.check_output(
                    [
                        "pgrep",
                        "-f",
                        "^/opt/hermes/[.]venv/bin/python -P -c .* gateway run --replace$",
                    ],
                    text=True,
                )
                assert str(gateway.pid) in matches.splitlines()
                print("Native gateway process: " + " ".join([*argv[:3], "<bootstrap>", *argv[-3:]]))
                break
            time.sleep(0.01)
        else:
            raise AssertionError("native gateway launcher did not enter its Python process")
    finally:
        gateway.terminate()
        gateway.wait(timeout=10)
    root = Path("/opt/hermes")
    generation = install_state_dir(root) / "environments" / "crbl-smoke"
    selected_site = generation / "lib/python3.14/site-packages"
    selected_site.mkdir(parents=True)
    (generation / "bin").mkdir()
    (generation / "bin/python").symlink_to(root / ".venv/bin/python")
    (generation / "pyvenv.cfg").write_bytes((root / ".venv/pyvenv.cfg").read_bytes())
    for entry in site_packages(root / ".venv").iterdir():
        (selected_site / entry.name).symlink_to(entry)
    runtime_facts_path(root).write_text(
        json.dumps({"packages": {"venv": {"environment": str(generation)}}})
    )
    assert "CRBL_LAUNCHER_PROOF:crbl-smoke" in check_launcher()
    assert {name: (home / name).read_bytes() for name in before} == before
    print("Real Hermes CLI launcher imported CRBL with baked and selected dependencies.")
    print("Native s6 managed startup and isolated credential checks passed.")


if __name__ == "__main__":
    main()
