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

## Container Image

The repository publishes a Docker image to GitHub Container Registry on pushes to
`main` and on version tags:

```text
ghcr.io/crbl-technologies/plex-media-request-mcp:latest
```

For a private repository/package, log in from the Docker host before pulling:

```bash
docker login ghcr.io -u YOUR_GITHUB_USERNAME
# password: GitHub token with read access to the package
```

This image is an MCP stdio server, so it should be launched by the client that
speaks MCP instead of as a detached long-running web service. For example, when
Hermes starts the MCP process, point the MCP command at Docker:

```yaml
mcp_servers:
  media:
    command: docker
    args:
      - run
      - --rm
      - -i
      - --env-file
      - /path/to/media-request.env
      - -e
      - PUID=1000
      - -e
      - PGID=1000
      - -v
      - media-request-state:/opt/data/state
      - ghcr.io/crbl-technologies/plex-media-request-mcp:latest
```

The env file should contain the same `PLEX_MEDIA_REQUEST_*` variables documented
below, plus `TELEGRAM_BOT_TOKEN` if availability notifications are enabled.

The Radarr/Sonarr webhook listener is a separate HTTP service image:

```text
ghcr.io/crbl-technologies/plex-media-request-webhook-bridge:latest
```

Use that image in Docker Compose when Radarr or Sonarr needs to call a persistent
HTTP endpoint. The bridge defaults to port `18081` and intentionally has no
built-in auth; keep it on an internal Docker network and do not expose it to the
public internet.

```yaml
services:
  media-request-webhook:
    image: ghcr.io/crbl-technologies/plex-media-request-webhook-bridge:latest
    restart: unless-stopped
    env_file:
      - ./media-request.env
    environment:
      PUID: "1000"
      PGID: "1000"
      UMASK: "0002"
    expose:
      - "18081"
    volumes:
      - media-request-state:/opt/data/state

volumes:
  media-request-state:
```

`media-request.env` contains the same Arr/Telegram settings used by the MCP
server, for example:

```dotenv
HERMES_HOME=/opt/data
PLEX_MEDIA_REQUEST_DB_PATH=/opt/data/state/media_requests.sqlite3
PLEX_MEDIA_REQUEST_RADARR_BASE_URL=http://radarr:7878
PLEX_MEDIA_REQUEST_RADARR_API_KEY=replace-with-radarr-api-key
PLEX_MEDIA_REQUEST_RADARR_QUALITY_PROFILE_ID=25
PLEX_MEDIA_REQUEST_RADARR_QUALITY_PROFILE_NAME=HD Bluray + WEB - Original
PLEX_MEDIA_REQUEST_RADARR_ROOT_FOLDER_PATH=/movies
PLEX_MEDIA_REQUEST_RADARR_TAG_IDS=12
PLEX_MEDIA_REQUEST_SONARR_BASE_URL=http://sonarr:8989
PLEX_MEDIA_REQUEST_SONARR_API_KEY=replace-with-sonarr-api-key
PLEX_MEDIA_REQUEST_SONARR_NORMAL_QUALITY_PROFILE_ID=25
PLEX_MEDIA_REQUEST_SONARR_NORMAL_QUALITY_PROFILE_NAME=WEB-1080p - Original
PLEX_MEDIA_REQUEST_SONARR_ANIME_QUALITY_PROFILE_ID=26
PLEX_MEDIA_REQUEST_SONARR_ANIME_QUALITY_PROFILE_NAME=Remux-1080p - Anime - Original
PLEX_MEDIA_REQUEST_SONARR_ROOT_FOLDER_PATH=/tv
PLEX_MEDIA_REQUEST_SONARR_TAG_IDS=7
TELEGRAM_BOT_TOKEN=replace-with-telegram-bot-token
# Optional; defaults to 65536 bytes.
PLEX_MEDIA_REQUEST_WEBHOOK_MAX_BODY_BYTES=65536
# Optional; disabled by default. Set to 300 for five-minute durable retries.
PLEX_MEDIA_REQUEST_NOTIFICATION_RETRY_INTERVAL_SECONDS=300
```

The env vars are required because the webhook bridge is standalone: it must read
the same request SQLite DB, verify availability from Radarr/Sonarr, and send the
Telegram notification. Keeping them in one `env_file` avoids duplicating a long
environment block in Compose.

Both container images support `PUID`, `PGID`, and `UMASK` so the MCP process and
webhook bridge can write the same SQLite state directory. Set `PUID`/`PGID` to
the numeric owner of your mounted state path. For example, on a NAS bind mount:

```bash
id your-media-user
sudo chown -R PUID:PGID /volume2/docker/hermes-media/data/state
sudo chmod -R ug+rwX /volume2/docker/hermes-media/data/state
sudo find /volume2/docker/hermes-media/data/state -type d -exec chmod g+s {} +
```

If Hermes launches the MCP image with `docker run`, pass the same `PUID` and
`PGID` there as well. If Hermes runs the Python MCP process directly, use the
UID/GID of that Hermes process for the webhook bridge.

Configure Connect webhooks to POST to these internal URLs:

```text
http://media-request-webhook:18081/radarr
http://media-request-webhook:18081/sonarr
```

Radarr events call `notify_movie_available(radarr_movie_id)`. Sonarr events call
`notify_series_available(sonarr_series_id)`, which only notifies once all seasons
for that stored request are complete. Requests for distinct season sets remain
independent, while exact repeats are deduplicated.

The webhook bridge rejects oversized request bodies, malformed
`Content-Length`, and explicit non-JSON content types before parsing the payload.
It returns HTTP 503 when an Arr lookup or Telegram delivery fails and keeps the
request pending. Because Arr webhook delivery is not a durable retry queue, the
bridge also rechecks pending movie and series notifications every five minutes
when the retry interval is enabled. Its HTTP responses and retry logs contain
only aggregate counts.

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
      # Optional; defaults to $HERMES_HOME/state/media_requests.sqlite3, or /opt/data/state/media_requests.sqlite3 when HERMES_HOME is unset.
      PLEX_MEDIA_REQUEST_DB_PATH: /opt/data/state/media_requests.sqlite3
```

## Tools

- `search_media(query: str, media_type: str = "any", season: int | None = None,
  limit: int = 5)` searches Radarr and/or Sonarr and returns factual
  file-based availability.
- `request_movie(tmdbId: int, title: str | None = None,
  requested_by_user_id: int | None = None,
  requested_by_chat_id: int | None = None,
  requested_by_username: str | None = None)` requests a movie using the configured
  Radarr policy and records an accepted request in SQLite when the server is
  running.
- `request_series(tvdbId: int, title: str | None = None,
  seasons: list[int], anime: bool = False,
  requested_by_user_id: int | None = None,
  requested_by_chat_id: int | None = None,
  requested_by_username: str | None = None)` requests a series using the
  configured Sonarr policy and records an accepted request in SQLite when the
  server is running. `seasons` is required; pass every wanted season explicitly.
- `request_status(query: str | None = None, limit: int = 250)` checks active
  queues plus monitored media and returns whether requests are downloading,
  partially available, available, waiting for release, or waiting for a
  suitable release.
- `download_status()` checks Radarr and Sonarr queues and returns a sanitized,
  read-only download summary.
- `browse_library(...)` browses available Radarr/Sonarr library items with
  filters for media type, genre, query, year, runtime, language, and limit.
- `media_status()` checks basic Radarr and Sonarr connectivity.
- `notify_movie_available(radarr_movie_id: int)` is the narrow webhook/backfill
  target for notifying stored requesters after one Radarr movie becomes
  available.
- `notify_available_requests(limit: int = 100)` retries pending stored movie and
  series requests and sends one-shot availability notifications.

## Tool Specification

An OpenAPI-style reference for the public MCP tools is available at
[docs/openapi.yaml](docs/openapi.yaml). It documents request schemas, response
schemas, examples, and the sanitized fields returned by the current tools.

## Development

Install the locked development dependencies and run the verification suite with:

```bash
python3 -m pip install --require-hashes -r requirements-dev.txt
python3 -W error::ResourceWarning -m unittest -q
ruff check .
ruff format --check .
mypy --check-untyped-defs media_request_server.py radarr_webhook_bridge.py scripts/check_public_repo.py
openapi-spec-validator docs/openapi.yaml
pip-audit -r requirements.txt
```

`requirements.txt` and `requirements-dev.txt` are generated lock files. Update
their inputs in `requirements.in` or `requirements-dev.in`, then regenerate both:

```bash
uv pip compile --python-version 3.12 --generate-hashes requirements.in -o requirements.txt
uv pip compile --python-version 3.12 --generate-hashes requirements-dev.in -o requirements-dev.txt
```
