# Immutable media-request deployment

This directory contains the production Compose topology and release-manifest
template for the four immutable services:

- `media-server-mcp`, pinned to the reviewed upstream v2.3.0 revision;
- `media-companion`, which owns the private typed API and Plex ingress;
- `hermes-media`, a pinned Hermes image carrying the checked-in `media-policy`
  platform extension; and
- `media-request-dashboard`, an authenticated LAN/Tailscale operations UI.

There is deliberately no `plex` service in `compose.yaml`. Plex remains an
existing host-network container outside this stack. The supported Plex
integration is the loopback mapping `127.0.0.1:18081` into
`media-companion`; this release does not require a Plex restart or recreation.
If verification establishes that a separate Plex action is necessary, it must
be explicitly authorized and performed outside this Compose project.

The dashboard is published as `18082:18082` on the NAS's existing `media`
network. It is intended for the operator's LAN/Tailscale address. This stack
adds no Cloudflare route or router/NAT WAN publication; verify WAN rejection in
the NAS firewall before cutover. Hermes has no dashboard port and must run with
`HERMES_DASHBOARD=false`; port 9119 is intentionally absent.

## Deployment scope

Any Compose or Portainer action for this release is limited to these four
services: `media-server-mcp`, `media-companion`, `hermes-media`, and
`media-request-dashboard`. Plex is not a stack member and has no service entry;
stack actions do not target Plex, and no Plex restart is expected for this
release. The supported integration is the loopback webhook delivered to
`media-companion`. Any separately verified and explicitly authorized Plex
operation remains outside this stack; do not grant it a Docker socket or add a
`docker ... plex` lifecycle command to Compose or the release manifest.

The upstream `media-server-mcp` service is run with `TOOL_PROFILE=full` and no
`TOOL_INCLUDE` override. Its live `tools/list` contract is pinned to the 64
Plex/Radarr/Sonarr tools captured in the checked-in contract artifact.

## Host files

Copy [`deployment/.env.example`](.env.example) to a deployment-only file and
fill in image digests, provenance, artifact hashes, and the canonical host
paths. Do not put credentials in Compose YAML, this repository, Portainer
editor text, or a second copy of a secret.

The expected host-side files are:

| Path | Consumer(s) | Contents |
| --- | --- | --- |
| `/volume2/docker/hermes-media/data/.env` | Hermes | Native Telegram token/allowlist/admin policy |
| `/volume2/docker/hermes-media/secrets/upstream.env` | Upstream and companion | Configured provider origins and MCP bearer |
| `/volume2/docker/hermes-media/secrets/companion.env` | Companion | Loopback webhook capability only, mode 0600 |
| `/volume2/docker/hermes-media/secrets/actor-signing.key` | Hermes and companion | Actor assertion key, mode 0600 |
| `/volume2/docker/hermes-media/secrets/dashboard-auth.env` | Dashboard | Raw password hash only, mode 0600 |
| `/volume2/docker/hermes-media/secrets/dashboard-api.key` | Dashboard and companion | Separate dashboard API key, mode 0600 |

Portainer must not parse host-side Compose `env_file` paths. The upstream service
therefore mounts `upstream.env` read-only and uses Deno 2.6's
`--env-file=/run/media-secrets/upstream.env` with the pinned image's original
`deno run ... packages/media-server-mcp/src/index.ts` entrypoint and
`--http --host 0.0.0.0 --port 3000` command. The companion mounts the same
`upstream.env` and selects only approved provider URLs through
`MEDIA_COMPANION_PROVIDER_ENV_FILE`; credentials continue to use the
corresponding `*_FILE` references. The companion's non-secret database,
library, quality-profile, root-folder, and tag selectors are explicit Compose
environment values. `companion.env` is mounted read-only only so the companion
can read the loopback webhook capability; it is not a Compose `env_file`.
The dashboard similarly receives its non-secret allowed origins in Compose
environment and reads the raw hash from the mounted `dashboard-auth.env`.
The
pinned Hermes image retains its native s6 dispatcher as PID 1; Compose
therefore does not add a `user` or `init` wrapper and supplies
`HERMES_UID=1000` and `HERMES_GID=1000` for the image's own worker remap.
Hermes receives its one scoped data-directory bind at `/opt/data` read-write because
the native policy helper must atomically edit `TELEGRAM_ALLOWED_USERS` and
retain its sessions/logs; the actor key remains a separate read-only mount.
The Hermes service intentionally does not use a read-only root or drop all
capabilities: its native s6 runtime requires those facilities. Its bounded
`/run` tmpfs is explicitly executable for native s6 helper state.
The dashboard never mounts Hermes files, SQLite, provider credentials, or the
Docker socket. State is the only writable companion bind and must be owned by
UID/GID 1000 with a 0700 directory and 0600 SQLite/WAL/SHM files. Secret
directories are 0700 and secret files are 0600; all application workers use
UID/GID 1000 and umask 0077. The Hermes-hosted policy helper listens only on
internal network port `8787`; it has no host port. The Hermes plugin talks to
that service over its fixed loopback URL `http://127.0.0.1:8787`; only the
companion receives `CRBL_POLICY_HELPER_URL=http://hermes-media:8787` for its
private-network calls. The helper's `allowlist/mutate` route atomically edits
only `/opt/data/.env`, then requests a Hermes gateway recycle after its response;
it has no Docker/Plex lifecycle capability.

The `media-bot` bridge uses the fixed `172.30.40.0/24` subnet and
`172.30.40.1` gateway. The companion's
`MEDIA_COMPANION_PLEX_TRUSTED_PEERS` is pinned to that gateway because
host-network Plex reaches the loopback-published webhook through Docker's
bridge DNAT. Do not replace this with a runtime-discovered bridge address;
change the reviewed subnet and peer together if the operator's address plan
requires another non-overlapping range.

## Validation

Before any image publication or live action, resolve the deployment env and run:

```text
python3 scripts/validate_deployment.py \
  --env-file /path/to/deployment.env \
  --compose deployment/compose.yaml \
  --manifest deployment/manifest.yaml
```

The validator rejects missing or mutable image references, unresolved required
substitutions, incorrect network membership, public webhook binding, a Plex
service, or Plex-targeted lifecycle commands embedded in this stack. It also
rejects Docker-socket mounts, Hermes dashboard/9119 exposure, missing
hardening or health checks, duplicate canonical consumers, and manifest
provenance placeholders. It is deterministic and does not contact Docker,
Portainer, Plex, or any other live service; it does not decide whether a
separate, explicitly authorized Plex action is ever needed.

Build the companion and dashboard Dockerfiles only with a resolved digest-pinned
Python base (`PYTHON_BASE_IMAGE=repository@sha256:<digest>`). Record resulting
image digests and SBOM hashes in the manifest. Image publication and a later,
explicit production cutover are separate gates.

No command in this directory performs deployment, container restart, stop,
recreate, or Portainer/API operations.

## Hermes derived image

Build `hermes-media` from [`Dockerfile.hermes`](Dockerfile.hermes) with
`HERMES_BASE_IMAGE` resolved to the pinned Hermes v2026.8.3
`repository@sha256:<digest>`. The Dockerfile validates that reference during
the build, copies the extension and canonical `media_companion` policy package
into `/opt/hermes` and `/app/src`, installs the plugin at
`/opt/hermes/plugins/platforms/zzzz-media-policy`, and adds the helper under
`/etc/s6-overlay/s6-rc.d/policy-helper` plus the fail-closed cont-init check.
It deliberately leaves the base
`/opt/hermes/docker/entrypoint-dispatch.sh` as `ENTRYPOINT` and supplies
`gateway run` as the native CMD; Compose must not add `user`, `init`, or a
replacement entrypoint. The image sets
`S6_BEHAVIOUR_IF_STAGE2_FAILS=2`; with the pinned s6-overlay 3.2.3.0 base,
that makes a non-zero legacy cont-init contract exit terminate stage 2 instead
of allowing the gateway to start. Publish that resulting digest as
`HERMES_MEDIA_IMAGE`.
