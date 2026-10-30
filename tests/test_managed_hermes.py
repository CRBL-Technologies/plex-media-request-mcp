from __future__ import annotations

from pathlib import Path

import pytest

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
