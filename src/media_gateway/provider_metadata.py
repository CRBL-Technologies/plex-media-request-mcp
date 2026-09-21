"""Metadata facts shared by Radarr and Sonarr consumers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def public_poster_url(item: dict[str, Any]) -> str | None:
    """Return a provider-hosted HTTPS poster, never a private relative path."""

    candidates: list[object] = [item.get("remotePoster")]
    images = item.get("images")
    if isinstance(images, list):
        candidates.extend(
            image.get("remoteUrl")
            for image in images
            if isinstance(image, dict) and image.get("coverType") == "poster"
        )
    for candidate in candidates:
        if not isinstance(candidate, str) or len(candidate) > 2048:
            continue
        parsed = urlsplit(candidate)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        ):
            return candidate
    return None
