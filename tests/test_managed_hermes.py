from __future__ import annotations

from pathlib import Path

import pytest

from hermes_media.managed_app import project_hermes_files


def hermes_values() -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": "fetched-telegram-token",
        "CRBL_MEDIA_GATEWAY_URL": "http://gateway:18082",
        "CRBL_MEDIA_GATEWAY_TOKEN": "fetched-gateway-key-with-more-than-thirty-two-bytes",
    }


@pytest.mark.parametrize("invalid", ["missing", "upstream", "interpolation"])
def test_hermes_projection_rejects_bad_scope_without_writing(tmp_path: Path, invalid: str) -> None:
    source = hermes_values()
    if invalid == "missing":
        del source["TELEGRAM_BOT_TOKEN"]
    elif invalid == "upstream":
        source["RADARR_API_KEY"] = "wrong-service-secret"
    else:
        source["TELEGRAM_BOT_TOKEN"] = "${STALE_TOKEN}"
    with pytest.raises(ValueError):
        project_hermes_files(source, tmp_path, tmp_path)
    assert list(tmp_path.iterdir()) == []
