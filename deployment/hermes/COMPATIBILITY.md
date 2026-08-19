# Hermes pin migration checklist

`src/hermes_media/compat.py` is the only production module allowed to touch
private Hermes APIs. A Hermes base-image digest change is an integration
migration, not an unattended dependency bump.

Before changing the pinned digest:

1. Confirm the upstream release is still `v2026.8.3` with package version
   `0.20.0`, or deliberately update both compatibility constants after
   reviewing the release notes.
2. Run the s6-overlay smoke with `tests/hermes_gateway_stub.py`; it verifies the
   exact shared/admin tool manifest, plugin registration, private visibility
   resolver, effective platform hint, and search-only provider boundary.
3. Verify `agent.system_prompt._resolve_platform_hint` still exists and is still
   the single site that resolves a platform's prompt hint. `install_platform_hint`
   wraps it so `PLATFORM_HINT` stays the only copy of the CRBL guidance instead
   of being mirrored into `platform_hints.telegram.append`. If Hermes stops
   preferring its built-in `PLATFORM_HINTS["telegram"]` over a plugin's
   registered hint, the wrapper can be dropped in favour of the value already
   passed to `register_platform`. `verify_pinned_runtime` fails closed when the
   wrapper is inactive or when the guidance no longer reaches a prompt resolved
   with no config override.
4. Verify the identity slot still loads from a file. `hermes_constants.get_hermes_home`
   and `agent.prompt_builder.load_soul_md` are the two names the init script uses
   to install `SOUL.md` and to read it back; `load_soul_md` returning `None` for
   an unreadable file is why the read-back exists at all. If Hermes gains a
   supported way to supply the identity in process, `install_soul` and its write
   to a bind-mounted host directory can be dropped for it.
5. Run Ruff, strict mypy, the complete pytest suite, both hash-locked dependency
   audits, Compose validation, and the gateway container migration smoke.
6. Publish immutable images, deploy with a verified database backup, and check
   that Plex and unrelated agent containers retain their IDs.
