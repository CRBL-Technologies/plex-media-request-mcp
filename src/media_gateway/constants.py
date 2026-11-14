"""Pinned upstream provenance and closed tool inventories."""

from typing import Final

UPSTREAM_VERSION: Final = "2.3.0"
UPSTREAM_REVISION: Final = "8b469d2b321b27dd1e4f5b89a7236b3ea43c3c72"
UPSTREAM_IMAGE: Final = (
    "ghcr.io/wyattjoh/media-server-mcp@"
    "sha256:f83620da1d008ef18df3324b15e44854572ea41b528eff585033e4054b438377"
)

SHARED_TOOLS: Final = (
    "search_media",
    "recommend_media",
    "request_movie",
    "request_series",
    "request_titles",
    "series_seasons",
    "request_status",
    "download_status",
    "browse_library",
    "media_status",
)
