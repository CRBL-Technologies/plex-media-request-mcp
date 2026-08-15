# CRBL Media Gateway

This repository contains the small CRBL-owned integration around the upstream
[`wyattjoh/media-server-mcp`](https://github.com/wyattjoh/media-server-mcp).
It does **not** fork, copy, or patch that MCP server.

Production keeps the upstream image pinned to version `2.3.0`, source revision
`8b469d2b321b27dd1e4f5b89a7236b3ea43c3c72`, and OCI digest
`sha256:f83620da1d008ef18df3324b15e44854572ea41b528eff585033e4054b438377`.
Compose runs its original Deno application directly and uses Deno's native
`--env-file` option because Portainer cannot resolve NAS project paths itself.

## Architecture

The media subsystem has three services:

1. `media-server-mcp`: the untouched, pinned upstream MCP on an internal network;
2. `media-gateway`: CRBL policy, safe tools, Plex notifications, SQLite state,
   and the LAN/Tailscale dashboard;
3. `hermes-media`: the pinned Hermes image plus a thin Telegram identity adapter.

The Portainer stack file also preserves the existing `hermes` and
`hermes-accountant` services because a stack update replaces the full Compose
document. Those agents are outside this project; this release does not change
their behavior or data mounts.

The gateway exposes exactly seven tools to an allowed Telegram user:

- `search_media`
- `request_movie`
- `request_series`
- `request_status`
- `download_status`
- `browse_library`
- `media_status`

Configured administrators also receive the reviewed Plex/Radarr/Sonarr tools
discovered from the pinned upstream MCP. Future upstream tools are not admitted
automatically. No selection token is used: requests take the TMDB or TVDB ID
returned by a current search.

Search results retain the provider's public poster URL. The Hermes tool
description tells the Telegram agent to render it through Hermes' native
`MEDIA:` delivery convention rather than exposing an inaccessible Radarr or
Sonarr cover path.

Upstream 2.3.0 lists `radarr_get_queue` in its full profile but does not register
the tool. The gateway therefore contains one narrow read-only Radarr queue call;
all other provider operations go through upstream MCP.

## Notifications

Plex `library.new` webhooks are the availability authority. Movie, show,
season, and episode payloads are supported. Every accepted addition notifies
each configured administrator. A bot requester is notified only when the
matching movie or requested season appears in Plex. A season batch waits until
the latest event has been quiet for the configured delay, so a bulk import
sends one message while weekly episodes remain individual messages. Each
message contains an `Open in Plex` button.

Webhook events and delivery receipts are durable and deduplicated in SQLite.
Unresolved Plex provider IDs remain retryable, and terminal operational data is
pruned after 60 days.

## Authorization

The Telegram actor is extracted from Hermes' native Telegram update before the
model runs. Model arguments never supply identity. The gateway rechecks the
current `TELEGRAM_ALLOWED_USERS` / `TELEGRAM_ADMIN_USERS` policy on every tool
call. The dashboard changes only `TELEGRAM_ALLOWED_USERS`, using a locked atomic
rewrite of Hermes' canonical `.env`; it never promotes or removes an admin.

If the CRBL plugin does not load completely, the container startup gate fails.
If it fails before replacement, Hermes' native Telegram allowlist remains the
fallback admission boundary.

The dashboard is password protected with a scrypt hash, server-side signed
sessions, strict same-site cookies, and CSRF tokens. It shows real observed and
blocked Telegram users, recent requests, and activity; it does not expose raw
JSON or secrets.

## Deployment

Copy the examples in `deployment/` and provide only host paths and immutable
image references as Portainer stack variables. Runtime settings live once in
`gateway.env`; upstream provider credentials live once in `upstream.env`; key
material is stored in individual read-only files.

The dashboard binds NAS port `18082` for LAN/Tailscale access. Port `18081` is
loopback-only for the host-network Plex webhook. The Hermes dashboard and Docker
socket are absent. The upstream MCP has no published port.

The new gateway database is schema-versioned and fails closed on an incompatible
old database. Back up the previous file, then start the clean deployment with a
new `gateway.sqlite3`.

## Development

Python 3.12 is required.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy src
.venv/bin/pytest -q
.venv/bin/python -m pip_audit -r requirements.txt --require-hashes
```

Runtime dependencies are hash-locked in `requirements.txt`. CI repeats the
checks, validates Compose, and builds both CRBL images from digest-pinned bases.
Publishing records immutable image digests, SBOMs, and provenance.
