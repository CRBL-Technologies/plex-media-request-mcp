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

Series availability comes from Sonarr episode records, not monitoring-filtered
summary counts. Being caught up with aired episodes is distinct from a finished,
fully held season. A failed provider read means unknown availability, not missing
media. Bulk requests leave ambiguous exact titles unresolved instead of choosing
one remake or media type arbitrarily. Mutations and reconciliation for the same
title are serialized within the gateway worker.

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
season, and episode payloads are supported. Every movie and grouped show or
season arrival notifies each configured administrator. A lone weekly episode
is requester-only, so an administrator receives it only after requesting that
season through the bot. Every currently allowed requester of the matching movie
or season is notified at the chat where they made the request. Plex's related
show, season and episode events are coalesced before delivery; a season batch
waits until the latest event has been quiet for the configured delay, so a bulk
import sends one message while weekly episodes remain individual messages.
Each message contains an `Open in Plex` button.
When Plex supplies its catalog slug, that button uses a `watch.plex.tv` universal
link so supported mobile clients open the Plex app; the browser remains the
fallback. Missing slugs are resolved against Plex's fixed metadata endpoint
with the existing read-only-mounted Plex credential, never a user-controlled
URL or a second secret copy. The server-specific Plex Web URL remains the
fallback when resolution is unavailable.

Webhook events and delivery receipts are durable and deduplicated in SQLite.
Movie alerts use TMDB identity per chat, not Plex's replaceable library entry ID.
Those compact receipts remain after the 60-day cleanup. New explicit request
attempts have separate receipts, so every requester can still be notified.
Schema migration 4 copies retained movie delivery history into this identity;
it preserves the original events and receipts and sends no messages. History
already pruned cannot be recovered from the database. Movie observations without
a TMDB ID wait for enrichment rather than sending a possible duplicate.
Unresolved Plex provider IDs remain retryable, and terminal operational data is
pruned after 60 days, at startup and hourly while running. Delivery failures are
isolated per recipient: blocked chats are suppressed for that event, while
temporary failures and rate limits retry without holding up other recipients.
Acquisition intent and every Telegram destination are
committed before Radarr or Sonarr is mutated. Provider outcomes are recorded
afterward; interrupted or unknown operations are reconciled idempotently at
gateway startup. A repeat request by one user from another chat therefore adds
a destination instead of overwriting the first.

Each request attempt has an internal generation; results from an older attempt
cannot overwrite a newer one. Series requests track verified seasons individually
and become available only when all requested seasons are fulfilled. Fulfilled
series rows remain active episode subscriptions rather than being pruned as
terminal movie requests. New attempts clear their season progress without changing
the original creation time or losing notification destinations.

Database schema 3 adds only `generation` and `fulfilled_seasons` to existing
requests. The migration runs transactionally on startup and preserves existing
rows. Rolling back to an older image requires restoring a pre-upgrade database;
older images intentionally refuse newer schemas.

## Authorization

The Telegram actor is extracted from Hermes' native Telegram update before the
model runs. Model arguments never supply identity. The gateway rechecks the
current `TELEGRAM_ALLOWED_USERS` / `TELEGRAM_ADMIN_USERS` policy on every tool
call. The dashboard changes only `TELEGRAM_ALLOWED_USERS`, using a locked atomic
rewrite of Hermes' canonical `.env`; it never promotes or removes an admin.

Group and topic sessions are isolated per user in both Hermes' queue and history.
The executing handler rechecks identity and role for queued turns. Native shared
unmentioned-group observation is disabled; private-chat session keys are unchanged.
Poster cards preserve the originating topic and private-topic reply anchor.

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

### Dashboard design

The Media admin dashboard uses CRBL Atelier: stone paper, warm ink, antique
gold, square controls, and self-hosted Space Grotesk, Inter, and JetBrains Mono.
`src/media_gateway/static/brand/` contains the shared stylesheet and fonts from
the CRBL design-system revision `00d9f06` (2026-10-01 adoption). Keep that bundle
in sync with the shared source; dashboard-specific layout stays in `dashboard.py`.
The existing media clapperboard favicon uses the same palette. Fonts and icons
are served locally under the existing same-origin content security policy.

### Runtime

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
while Sonarr is still fetching it, never to release one early. An unavailable
queue does not by itself block delivery; season completeness still requires
verified Sonarr episode records and matching playable Plex episodes.

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

### Image channels and runtime migration boundary

The publish workflow runs CI before publishing. A `dev` push publishes `preview`
and `sha-<full-commit>` for the CRBL images. Only a `main` branch build moves
`latest`. Release tags keep their existing version tags. Other manually selected
branches cannot publish. Publication never deploys or restarts a container.
Record the digest from the workflow summary before an approved update. Use that
digest to recover the exact image; a channel tag can move. Keep the existing
database-backup procedure when a release changes its schema.

The default gateway and Hermes images keep their public startup and file-secret
contracts. Their separate `managed` build targets publish as
`ghcr.io/crbl-technologies/media-request-companion-managed` and
`ghcr.io/crbl-technologies/hermes-media-managed`. The managed upstream recipe
publishes as `ghcr.io/crbl-technologies/media-server-mcp-managed`. It adds a Deno
startup wrapper to the pinned vendor image; it does not patch vendor source.
Upstream updates still start with the vendor release and its compatibility
checks. The default images and `deployment/compose.yaml` do not require 1Password.

Each managed service requires its own scoped Environment and bootstrap mount.
The owner mounts an owner-only regular file at `/run/secrets/onepassword.json`,
read-only and owned by UID 1000. Its JSON object has exactly two string fields:
`environment_id` and `service_account_token`. Neither value belongs in Git,
image layers, Compose environment values or Portainer variables. The approved
CLI is 2.41.1 at the pinned Dockerfile digest. Do not use an assistant credential.

The gateway Environment must contain all twelve existing app-option names in
`media_gateway.managed_start.APP_KEYS`, including both tag lists even when empty.
It must also contain these file-backed values:

| Managed Environment fields | Ephemeral gateway file |
| --- | --- |
| `MCP_AUTH_TOKEN`, `RADARR_URL`, `RADARR_API_KEY`, `SONARR_URL`, `SONARR_API_KEY`, `PLEX_URL`, `PLEX_API_KEY` | `/run/media-secrets/upstream.env` |
| `TELEGRAM_BOT_TOKEN` | `/run/media-secrets/telegram.env` |

Locally generated keys are not Environment fields. The owner keeps them in the
existing host files, which Compose mounts read-only:

| Stack variable (host file) | Gateway mount |
| --- | --- |
| `GATEWAY_TOKEN_FILE` | `/run/media-keys/gateway.key`, also mounted for Hermes |
| `DASHBOARD_PASSWORD_HASH_FILE` | `/run/media-keys/dashboard-password.hash` |
| `DASHBOARD_SESSION_KEY_FILE` | `/run/media-keys/dashboard-session.key` |
| `PLEX_WEBHOOK_TOKEN_FILE` | `/run/media-keys/plex-webhook.key` |

The wrappers refuse to start if `MEDIA_GATEWAY_TOKEN`,
`MEDIA_GATEWAY_DASHBOARD_PASSWORD_HASH`, `MEDIA_GATEWAY_DASHBOARD_SESSION_KEY`,
`MEDIA_GATEWAY_PLEX_WEBHOOK_TOKEN` or `CRBL_MEDIA_GATEWAY_TOKEN` appears in an
Environment, so each generated key keeps one source.

These files retain their existing raw-key or dotenv formats. The managed gateway
sets `MEDIA_GATEWAY_TELEGRAM_TOKEN_FILE` to the separate Telegram file. The
public default still reads the token from its policy file. When the separate
path is set, a missing token file never falls back to the policy file.

The upstream Environment contains the seven upstream fields above.
`TMDB_API_KEY` is optional: without it the vendor does not register its TMDB
tools, which matches the current deployment. `TOOL_PROFILE=full` is a Compose
setting, not an Environment field; the wrapper still refuses any other profile.
The optional `TOOL_BRANCHES`, `TOOL_EXCLUDE` and `TOOL_INCLUDE` settings also
belong in Compose. They must preserve the reviewed gateway tool manifest. The wrapper writes its private
`/run/media-secrets/upstream.env`, then starts the unmodified vendor command with
Deno's native `--env-file` option. Native parsing is tested for literal dollar
signs, quotes and comment characters. Multiline values and `${...}` interpolation
forms are rejected instead of being silently changed.

The Hermes Environment contains only `TELEGRAM_BOT_TOKEN` and
`CRBL_MEDIA_GATEWAY_URL`. The gateway key comes from the read-only host-file
mount at `/run/media-keys/gateway.key`. A private `/run/hermes-managed/.env`
supplies the token, URL and key-file path through Hermes' native managed-scope loader.
That layer overrides stale startup values without rewriting the user `.env`.
The wrapper rejects upstream service keys and gateway-only private secrets in
this Environment. Native `/init` remains PID 1; retrieval runs after the UID
remap and before profile reconciliation or the CRBL startup checks.

All projected files are owner-only and live on tmpfs. Existing files and symlinks
are not overwritten. Inherited copies are stripped before retrieval. Gateway
file-secret values and all `OP_*` fields are removed before app execution.
Hermes' retrieval hook exits without passing its environment to s6. Gateway and
upstream logs pass through CLI masking. Hermes retains its native application
logging after the hook exits. Raw CLI diagnostics are suppressed. Host, port,
mount paths, native process settings and network/security controls stay in Compose.

Retrieval and validation have a 60-second deadline and one attempt per container
start. On failure, the process stays unhealthy until an operator corrects the
configuration and restarts it. There is no local cache or automatic fetch loop.
Signals still stop the process group. Budget account-wide request use and verify
real retrieval separately; tests use dummy data and a fake CLI transport.

For a later approved cutover, use `deployment/compose.managed.yaml` by itself.
Do not merge it with the default recipe: old read-only file mounts would cover
the projection paths. Select managed digests using `MEDIA_GATEWAY_IMAGE`,
`HERMES_MEDIA_IMAGE` and `MEDIA_UPSTREAM_IMAGE`. `GATEWAY_ONEPASSWORD_FILE`,
`HERMES_ONEPASSWORD_FILE` and `UPSTREAM_ONEPASSWORD_FILE` identify only protected
host mount paths, not tokens or Environment IDs.

Keep old saved Portainer fields and host secret files initially for rollback.
The managed recipe reads only the four generated-key files listed above. Verify fetched values, failure with
missing fields, restart behavior, notifications and live policy edits. Only then
remove obsolete saved fields and old secret copies (`upstream.env` and
`gateway.env`, but not the generated-key files), including the old
`TELEGRAM_BOT_TOKEN` line in the user policy file. Preserve all authorization
and other state lines. Restart and verify again after cleanup. No cutover or
cleanup is performed by this preparation.

The writable policy file remains the authority for `TELEGRAM_ALLOWED_USERS` and
`TELEGRAM_ADMIN_USERS`. The dashboard can still edit it while the app runs.
Hermes model/provider selection, session data and OAuth refresh state also stay
under native application control. The deployed provider and its active immutable
credential names cannot be determined from this repository alone. A names-only
provider review is still required before moving any provider credentials from
`.env` or `auth.json`. Copying refreshed OAuth state into a startup Environment,
or pinning empty credentials for every supported provider, would change the
native UI and refresh contract. This is the remaining provider-state boundary,
not a fallback for the managed Telegram, gateway or upstream fields.

Keep the three store identities separate. Shared bot/gateway token values are
needed only by their documented consumers; Hermes must not acquire Radarr,
Sonarr, Plex, TMDB or gateway dashboard/session keys. No real 1Password identifier,
bootstrap token, or private deployment inventory belongs in this public repository.
