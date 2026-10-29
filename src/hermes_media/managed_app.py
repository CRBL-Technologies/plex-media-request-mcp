"""Prepare Hermes' supported managed-scope env without editing its user state."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from media_gateway.managed_files import single_line, write_private

from .managed_start import APP_KEYS, FORBIDDEN_KEYS


def project_hermes_files(source: dict[str, str], secrets: Path, managed: Path) -> None:
    if not APP_KEYS.issubset(source) or FORBIDDEN_KEYS.intersection(source):
        raise ValueError("incomplete or over-scoped Hermes Environment")
    values = {key: single_line(source[key]) for key in APP_KEYS}
    single_line(values["CRBL_MEDIA_GATEWAY_TOKEN"], 32)
    values["CRBL_MEDIA_GATEWAY_TOKEN_FILE"] = str(secrets / "gateway.key")
    token = values.pop("CRBL_MEDIA_GATEWAY_TOKEN")
    # Native python-dotenv resolves ${...} even inside quoted strings. Reject
    # that form instead of silently changing a fetched credential or URL.
    if any("${" in value for value in values.values()):
        raise ValueError("unsupported interpolation in managed runtime value")
    lines = []
    for key, value in sorted(values.items()):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"{key}='{escaped}'\n")
    write_private(secrets / "gateway.key", token + "\n")
    write_private(managed / ".env", "".join(lines))


def main() -> int:
    os.dup2(sys.stdout.fileno(), sys.stderr.fileno())
    try:
        if os.environ.get("HERMES_MANAGED_DIR") != "/run/hermes-managed":
            raise ValueError("managed scope must select the private tmpfs")
        project_hermes_files(
            dict(os.environ), Path("/run/media-secrets"), Path("/run/hermes-managed")
        )
    except (ValueError, OSError):
        print("Managed Hermes runtime fields are incomplete, invalid, or over-scoped.", flush=True)
        return 1
    # This hook exits. Neither its bootstrap token nor fetched secret environment
    # is inherited by s6 or the Hermes process. Hermes reads its private files.
    os.kill(int(sys.argv[1]), signal.SIGUSR1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
