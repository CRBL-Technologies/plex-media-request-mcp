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

The Portainer definition owns only those three media services. General Hermes
and accountant agents use a separate stack and release lifecycle while sharing
the external `media` network where required.

The gateway exposes exactly eight media tools to an allowed Telegram user:

- `search_media`
- `recommend_media`
- `request_movie`
- `request_series`
- `request_status`
- `download_status`
- `browse_library`
- `media_status`

The Hermes adapter additionally exposes its native `web_search` tool through
the search-only toolset. It uses the bundled DuckDuckGo provider for current
movie recommendations, release dates, and viewing-order research. The image
bakes a hash-locked provider dependency; it does not expose `web_extract`,
browser automation, files, a shell, or arbitrary URL fetching. Hermes treats
search results as untrusted web content. The adapter clamps the native runaway
guardrail to ten web searches per response through Hermes' canonical
`tool_loop_guardrails.loop_caps.max_web_searches` setting. The image startup
gate refuses to run if the persisted value is absent or different, so the
agent and release verifier always read the same setting.

Configured administrators also receive the reviewed Plex/Radarr/Sonarr tools
discovered from the pinned upstream MCP. Future upstream tools are not admitted
automatically. Retaining that broad operations surface in the same bot is a
deliberate product decision: only explicit administrators see it, while regular
users remain limited to eight normalized tools. No selection token is used:
requests take the TMDB or TVDB ID returned by a current search.

Search results retain the provider's public poster URL. The Hermes adapter
presents up to four matches in one tabbed Telegram card with the best match open
by default. Each labeled row opens that result and swaps the card's poster and
caption in place; no album or numbered legend is posted. A movie tab offers a
direct Request movie action that performs the gateway request from the tap
itself — the card then shows the recorded outcome and the agent only confirms
it — while a series tab selects the exact TVDB result before asking for
seasons. Only that selected result is returned to the model,
so a typed number or button click cannot become a second bare query. Remote
poster URLs are never sent through Hermes' local-file `MEDIA:` convention.
An exact number, unique year, or exact title still selects a result, while
other text supersedes the old card and starts a fresh request.
Recommendation research uses `recommend_media` once with four distinct titles
instead of opening one ambiguity picker per suggestion. Recommendation turns
reject model-generated single-title lookups before a card is sent. The one
four-title card offers Pick, Search more, and Cancel; Search more explicitly
asks Hermes for a fresh batch that excludes the current titles. Silence never
advances recommendation research.
Every title lookup, availability check, and request must refresh `search_media`
in the current turn; conversation history is never a valid substitute for a
current provider result. Hermes's built-in Telegram hint takes precedence over
plugin metadata, so this rule is also installed as the canonical
`platform_hints.telegram.append` override. The image startup gate validates the
effective resolved prompt before the agent starts.

Upstream 2.3.0 lists `radarr_get_queue` in its full profile but does not register
the tool. The gateway therefore contains one narrow read-only Radarr queue call;
all other provider operations go through upstream MCP.

The picker is interruptible. A reply containing a result number, a unique
result year, or an exact title selects it. Any other normal message cancels the
old picker and continues as a fresh request; unanswered media pickers expire
after two minutes and interrupt their exact Hermes turn without queuing a new
message. This prevents a new title search from waiting behind an abandoned
selection prompt or silently continuing into another search.

## Notifications

Plex `library.new` webhooks are the availability authority. Movie, show,
season, and episode payloads are supported. Every accepted addition notifies
each configured administrator. A bot requester is notified only when the
matching movie or requested season appears in Plex. A season batch waits until
the latest event has been quiet for the configured delay, so a bulk import
sends one message while weekly episodes remain individual messages. Each
message contains an `Open in Plex` button.
When Plex supplies its catalog slug, that button uses a `watch.plex.tv` universal
link so supported mobile clients open the Plex app; the browser remains the
fallback. Missing slugs are resolved against Plex's fixed metadata endpoint
with the existing read-only-mounted Plex credential, never a user-controlled
URL or a second secret copy. The server-specific Plex Web URL remains the
fallback when resolution is unavailable.

Webhook events and delivery receipts are durable and deduplicated in SQLite.
Unresolved Plex provider IDs remain retryable, and terminal operational data is
pruned after 60 days. Acquisition intent and every Telegram destination are
committed before Radarr or Sonarr is mutated. Provider outcomes are recorded
afterward; interrupted or unknown operations are reconciled idempotently at
gateway startup. A repeat request by one user from another chat therefore adds
a destination instead of overwriting the first.

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
blocked Telegram users, paginated requests, and paginated activity; it does not
expose raw JSON or secrets. Allowed messages update the user's `last_seen`
timestamp without creating repetitive activity rows. Plex activity identifies
the exact movie, series, season, or episode that was added.

Requester delivery rechecks the current allowlist. Removing a user preserves
their historical request for audit but immediately prevents later Telegram
notifications.

## Deployment

Copy the examples in `deployment/` and provide only host paths and immutable
image references as Portainer stack variables. Runtime settings live once in
`gateway.env`; upstream provider credentials live once in `upstream.env`; key
material is stored in individual read-only files.

The dashboard binds NAS port `18082` for LAN/Tailscale access. Port `18081` is
loopback-only for the host-network Plex webhook. The Hermes dashboard and Docker
socket are absent. The upstream MCP has no published port.

The media agent's canonical `/opt/data/config.yaml` must contain:

```yaml
tool_loop_guardrails:
  loop_caps:
    max_web_searches: 10
```

This is a native Hermes setting, not a second CRBL configuration source.

The gateway database advances through numbered, transactional migrations and
fails closed on an incompatible schema. Deployment takes and verifies a SQLite
backup before any schema-changing release. The one-time legacy importer was
retired after its production recovery was completed and verified.

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
