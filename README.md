# Plex Media Request MCP

Small Python MCP stdio server for safe Plex media requests from an agent such as
Hermes Agent over Telegram.

The server exposes narrow tools for searching and adding Radarr movies and
Sonarr series. It does not expose quality profile IDs or root folder choices as
tool arguments.

## Configuration

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Set the required environment variables:

```bash
PLEX_MEDIA_REQUEST_RADARR_BASE_URL=replace-with-radarr-base-url
PLEX_MEDIA_REQUEST_RADARR_API_KEY=replace-with-radarr-api-key
PLEX_MEDIA_REQUEST_RADARR_QUALITY_PROFILE_ID=replace-with-radarr-quality-profile-id
PLEX_MEDIA_REQUEST_RADARR_QUALITY_PROFILE_NAME=replace-with-radarr-quality-profile-name
PLEX_MEDIA_REQUEST_RADARR_ROOT_FOLDER_PATH=replace-with-radarr-root-folder-path
PLEX_MEDIA_REQUEST_RADARR_TAG_IDS=replace-with-radarr-tag-ids
PLEX_MEDIA_REQUEST_SONARR_BASE_URL=replace-with-sonarr-base-url
PLEX_MEDIA_REQUEST_SONARR_API_KEY=replace-with-sonarr-api-key
PLEX_MEDIA_REQUEST_SONARR_NORMAL_QUALITY_PROFILE_ID=replace-with-sonarr-normal-quality-profile-id
PLEX_MEDIA_REQUEST_SONARR_NORMAL_QUALITY_PROFILE_NAME=replace-with-sonarr-normal-quality-profile-name
PLEX_MEDIA_REQUEST_SONARR_ANIME_QUALITY_PROFILE_ID=replace-with-sonarr-anime-quality-profile-id
PLEX_MEDIA_REQUEST_SONARR_ANIME_QUALITY_PROFILE_NAME=replace-with-sonarr-anime-quality-profile-name
PLEX_MEDIA_REQUEST_SONARR_ROOT_FOLDER_PATH=replace-with-sonarr-root-folder-path
PLEX_MEDIA_REQUEST_SONARR_TAG_IDS=replace-with-sonarr-tag-ids
```

Base URLs are normalized, so a trailing slash is fine. API keys are sent with
the `X-Api-Key` header and are never returned by tools. Profile and root folder
settings are configured by environment variables, but they are not exposed as
tool inputs.

Tag IDs are optional comma-separated lists. Radarr and Sonarr keep separate tag
namespaces, so create the visible tag in each app and use that app's numeric tag
ID in the matching env var.

## Hermes Example

```yaml
mcp_servers:
  media:
    command: python3
    args:
      - /path/to/plex-media-request-mcp/media_request_server.py
    env:
      PLEX_MEDIA_REQUEST_RADARR_BASE_URL: replace-with-radarr-base-url
      PLEX_MEDIA_REQUEST_RADARR_API_KEY: replace-with-radarr-api-key
      PLEX_MEDIA_REQUEST_RADARR_QUALITY_PROFILE_ID: replace-with-radarr-quality-profile-id
      PLEX_MEDIA_REQUEST_RADARR_QUALITY_PROFILE_NAME: replace-with-radarr-quality-profile-name
      PLEX_MEDIA_REQUEST_RADARR_ROOT_FOLDER_PATH: replace-with-radarr-root-folder-path
      PLEX_MEDIA_REQUEST_RADARR_TAG_IDS: replace-with-radarr-tag-ids
      PLEX_MEDIA_REQUEST_SONARR_BASE_URL: replace-with-sonarr-base-url
      PLEX_MEDIA_REQUEST_SONARR_API_KEY: replace-with-sonarr-api-key
      PLEX_MEDIA_REQUEST_SONARR_NORMAL_QUALITY_PROFILE_ID: replace-with-sonarr-normal-quality-profile-id
      PLEX_MEDIA_REQUEST_SONARR_NORMAL_QUALITY_PROFILE_NAME: replace-with-sonarr-normal-quality-profile-name
      PLEX_MEDIA_REQUEST_SONARR_ANIME_QUALITY_PROFILE_ID: replace-with-sonarr-anime-quality-profile-id
      PLEX_MEDIA_REQUEST_SONARR_ANIME_QUALITY_PROFILE_NAME: replace-with-sonarr-anime-quality-profile-name
      PLEX_MEDIA_REQUEST_SONARR_ROOT_FOLDER_PATH: replace-with-sonarr-root-folder-path
      PLEX_MEDIA_REQUEST_SONARR_TAG_IDS: replace-with-sonarr-tag-ids
```

## Tools

- `search_media(query: str, media_type: str = "any", season: int | None = None,
  limit: int = 5)` searches Radarr and/or Sonarr and returns factual
  file-based availability.
- `request_movie(tmdbId: int, title: str | None = None)` requests a movie using
  the configured Radarr policy.
- `request_series(tvdbId: int, title: str | None = None,
  seasons: list[int], anime: bool = False)` requests a series using the
  configured Sonarr policy. `seasons` is required; pass every wanted season
  explicitly.
- `request_status(query: str | None = None, limit: int = 10)` checks active
  queues plus monitored missing media and returns whether requests are
  downloading, waiting for release, or waiting for a suitable release.
- `download_status()` checks Radarr and Sonarr queues and returns a sanitized,
  read-only download summary.
- `browse_library(...)` browses available Radarr/Sonarr library items with
  filters for media type, genre, query, year, runtime, language, and limit.
- `media_status()` checks basic Radarr and Sonarr connectivity.

## Tool Specification

An OpenAPI-style reference for the public MCP tools is available at
[docs/openapi.yaml](docs/openapi.yaml). It documents request schemas, response
schemas, examples, and the sanitized fields returned by the current tools.

## Development

Run tests with:

```bash
python3 -m unittest -v
```
