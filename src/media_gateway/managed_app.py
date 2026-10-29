"""Validate fetched gateway options and remove bootstrap data before app exec."""

from __future__ import annotations

import os
import signal
import sys

from .config import Config
from .managed_start import APP_KEYS


def application_environment(source: dict[str, str]) -> dict[str, str]:
    if not APP_KEYS.issubset(source):
        raise ValueError("incomplete runtime Environment")
    return {
        key: value
        for key, value in source.items()
        if not key.startswith("OP_") and key != "MEDIA_GATEWAY_CONFIG_FILE"
    }


def main() -> int:
    # op hides its own stderr. Preserve application errors on the masked output
    # stream instead of losing normal service logs with the CLI diagnostics.
    os.dup2(sys.stdout.fileno(), sys.stderr.fileno())
    try:
        environment = application_environment(dict(os.environ))
        os.environ.clear()
        os.environ.update(environment)
        Config.from_env()
    except (KeyError, ValueError):
        print("Managed gateway runtime options are incomplete or invalid.", flush=True)
        return 1
    os.kill(int(sys.argv[1]), signal.SIGUSR1)
    os.execve(sys.executable, [sys.executable, "-m", "media_gateway.app"], environment)
    return 1


if __name__ == "__main__":
    sys.exit(main())
