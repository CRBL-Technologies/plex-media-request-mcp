"""Project fetched gateway options once, then supervise the native app."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path
from types import FrameType

from .config import Config
from .managed_files import FILE_KEYS, SECRET_KEYS, SECRET_PATHS, project_gateway_files
from .managed_start import RUNTIME_KEYS


def application_environment(source: dict[str, str]) -> dict[str, str]:
    if not RUNTIME_KEYS.issubset(source) or FILE_KEYS.intersection(source):
        raise ValueError("incomplete or over-scoped runtime Environment")
    return {
        key: value
        for key, value in source.items()
        if not key.startswith("OP_")
        and key != "MEDIA_GATEWAY_CONFIG_FILE"
        and key not in SECRET_KEYS
    }


def main() -> int:
    # op hides its own stderr. Preserve application errors on the masked output
    # stream instead of losing normal service logs with the CLI diagnostics.
    os.dup2(sys.stdout.fileno(), sys.stderr.fileno())
    stopped = threading.Event()
    child: subprocess.Popen[bytes] | None = None
    stop_signal: int = signal.SIGTERM

    def forward(signum: int, _frame: FrameType | None) -> None:
        nonlocal stop_signal
        if stopped.is_set():
            return
        stop_signal = signum
        stopped.set()
        if child is not None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    source = dict(os.environ)
    try:
        environment = application_environment(source)
        directory = Path("/run/media-secrets")
        if any(environment.get(key) != str(directory / name) for key, name in SECRET_PATHS.items()):
            raise ValueError("managed secret paths must select the private tmpfs")
        os.environ.clear()
        os.environ.update(environment)
        Config.from_env()
        project_gateway_files(source, directory)
    except (KeyError, ValueError, OSError):
        print("Managed gateway runtime options are incomplete or invalid.", flush=True)
        return 1
    finally:
        source.clear()
        for key in tuple(os.environ):
            if key.startswith("OP_") or key in SECRET_KEYS:
                del os.environ[key]
    if stopped.is_set():
        return 0
    os.kill(int(sys.argv[1]), signal.SIGUSR1)
    delay = 1
    while not stopped.is_set():
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "media_gateway.app"],
                env=environment,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            # A stop can arrive between checking the loop and spawning.
            if stopped.is_set():
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, stop_signal)
            while child.poll() is None and not stopped.is_set():
                stopped.wait(0.1)
        except OSError:
            print("Gateway process could not start.", flush=True)
        finally:
            if child is not None:
                if not stopped.is_set():
                    with suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGTERM)
                try:
                    # Finish before the outer loader's five-second deadline.
                    child.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                child = None
        if stopped.is_set():
            break
        print(f"Gateway process stopped; retrying in {delay} seconds.", flush=True)
        stopped.wait(delay)
        delay = min(delay * 2, 30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
