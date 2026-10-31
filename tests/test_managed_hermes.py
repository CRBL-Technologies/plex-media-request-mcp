from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hermes_media import compat
from hermes_media.managed_app import project_hermes_files


def hermes_values() -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": "fetched-telegram-token",
        "CRBL_MEDIA_GATEWAY_URL": "http://gateway:18082",
    }


def test_hermes_projection_reads_the_gateway_key_from_its_host_file(tmp_path: Path) -> None:
    project_hermes_files(hermes_values(), tmp_path)
    assert [path.name for path in tmp_path.iterdir()] == [".env"]
    content = (tmp_path / ".env").read_text()
    assert "CRBL_MEDIA_GATEWAY_TOKEN_FILE='/run/media-keys/gateway.key'" in content
    assert "CRBL_MEDIA_GATEWAY_TOKEN=" not in content


@pytest.mark.parametrize("invalid", ["missing", "upstream", "gateway-key", "interpolation"])
def test_hermes_projection_rejects_bad_scope_without_writing(tmp_path: Path, invalid: str) -> None:
    source = hermes_values()
    if invalid == "missing":
        del source["TELEGRAM_BOT_TOKEN"]
    elif invalid == "upstream":
        source["RADARR_API_KEY"] = "wrong-service-secret"
    elif invalid == "gateway-key":
        source["CRBL_MEDIA_GATEWAY_TOKEN"] = "generated-key-belongs-in-its-host-file"
    else:
        source["TELEGRAM_BOT_TOKEN"] = "${STALE_TOKEN}"
    with pytest.raises(ValueError):
        project_hermes_files(source, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"healthy": True}, id="connected"),
        pytest.param(
            {"telegram": {"state": "fatal", "error_code": "missing_credentials"}}, id="fatal"
        ),
        pytest.param({"gateway": {"gateway_state": "degraded"}}, id="degraded"),
        pytest.param({"heartbeat_age": timedelta(minutes=5)}, id="stale-heartbeat"),
        pytest.param(
            {"gateway": {"pid": 2147483647}, "telegram": {"writer_pid": 2147483647}},
            id="dead-process",
        ),
        pytest.param(
            {
                "gateway": {"start_time": "previous-process-start"},
                "telegram": {"writer_start_time": "previous-process-start"},
            },
            id="reused-pid",
        ),
        pytest.param(
            {"telegram": {"writer_start_time": "previous-process-start"}}, id="previous-adapter"
        ),
        pytest.param({"command": "python unrelated.py"}, id="non-gateway-process"),
        pytest.param({"missing_file": True}, id="missing-file"),
        pytest.param({"file_content": "{invalid"}, id="invalid-file"),
    ],
)
def test_pinned_runtime_health_requires_connected_current_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]
) -> None:
    status = pytest.importorskip("gateway.status")
    # Use this live process and the native PID-start-time record. Only its
    # command line is substituted; file parsing, freshness, and PID checks run.
    record = status._build_pid_record()
    record.update(
        gateway_state="running",
        updated_at=(datetime.now(UTC) - case.get("heartbeat_age", timedelta())).isoformat(),
        platforms={
            "telegram": {
                "state": "connected",
                "writer_pid": record["pid"],
                "writer_start_time": record["start_time"],
            }
        },
    )
    monkeypatch.setattr(
        status,
        "_read_process_cmdline",
        lambda pid: case.get(
            "command", "/opt/hermes/.venv/bin/python -m hermes_cli.main gateway run --replace"
        ),
    )
    record.update(case.get("gateway", {}))
    record["platforms"]["telegram"].update(case.get("telegram", {}))
    path = tmp_path / "gateway_state.json"
    if not case.get("missing_file"):
        path.write_text(case.get("file_content", json.dumps(record)))
    read_status = status.read_runtime_status
    monkeypatch.setattr(status, "read_runtime_status", lambda: read_status(path))

    assert compat.gateway_healthy() is case.get("healthy", False)
