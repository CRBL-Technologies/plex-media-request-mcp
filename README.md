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

Search results retain the provider's public poster URL. A search that resolves
to exactly one result is posted as that poster, carrying an `Open in Plex` link
when the title is held and no button at all otherwise. That link is the only
control a card has ever offered: there is no picker and no button that acts.
At most one poster is posted per user message, however many searches a turn
makes. Remote poster URLs are never sent through Hermes' local-file `MEDIA:`
convention.

The tools describe what they do and the model decides which to use. The
platform hint carries only what cannot be inferred: that Telegram identity is
trusted, that the tools are the sole source of truth for what this library
holds, that adding is the only thing the agent can change, and that a poster
may already have been posted. Which tool answers a question -- a library
search, a bulk lookup, a season report, or web_search for something the
library cannot know -- is the model's call.

Conversation history is never a valid substitute for a current provider result,
which is why the hint names the tools as the only source of truth rather than
prescribing when to call them. That guidance has exactly one copy,
`hermes_media.plugin.PLATFORM_HINT`. Hermes prefers its own built-in Telegram
hint over a plugin's registered one, so the plugin installs its text on Hermes'
hint resolver at registration instead of mirroring it into
`platform_hints.telegram.append`; a config file that is not in this repository
must never be a second source of truth for text that changes with the tools it
describes. The image startup gate resolves the prompt with no config override at
all and refuses to start unless the guidance arrived and Hermes' own Telegram
hint survived beside it.

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

The agent's prompt comes from three tiers, each versioned in this repository. A
tool says what it does in its own `description` and `inputSchema`, so choosing
between tools needs no instruction elsewhere. `PLATFORM_HINT` carries what is
true of the Telegram channel whichever tools exist. `src/hermes_media/SOUL.md`
carries what is true whichever platform and tools exist: identity, tone, and the
rules about never claiming availability, an ETA, or a request the tools did not
confirm. It names no tool, and a test enforces that.

The init script installs `SOUL.md` into `HERMES_HOME` on every start, so the
host copy is a build artifact and an edit made there does not outlive a deploy.
The startup gate then reads it back through Hermes' own loader and refuses to
serve if the identity it was built with is not the one the agent will use.

A Plex arrival is announced once the title has been quiet for
`MEDIA_GATEWAY_NOTIFICATION_DELAY_SECONDS` (5 seconds), so a season import is
one message rather than one per episode. A movie is its own batch with nothing
to group with, so it is sent on the next pass instead of serving out a window
that can only delay it.

The window absorbs the spread between Plex webhooks, not download time. Sonarr's
queue drains when it finishes importing, which is before Plex scans the files
and emits those webhooks -- a season pack is one queue item that becomes many
webhooks after it drains -- so an empty queue never proves an arrival is
complete. The queue is therefore only ever read to keep a quiet season waiting
while Sonarr is still fetching it, never to release one early, and an
unreachable Sonarr delivers on quiet alone.

The worker re-evaluates every pending batch once every 5 seconds, so the quiet
window is a threshold that pass tests, not a timer that fires, and that cycle
sets the floor on how soon an arrival can be announced.

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
