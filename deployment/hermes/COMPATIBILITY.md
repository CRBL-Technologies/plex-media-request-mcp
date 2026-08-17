# Hermes pin migration checklist

`src/hermes_media/compat.py` is the only production module allowed to touch
private Hermes APIs. A Hermes base-image digest change is an integration
migration, not an unattended dependency bump.

Before changing the pinned digest:

1. Confirm the upstream release is still `v2026.8.3` with package version
   `0.20.0`, or deliberately update both compatibility constants after
   reviewing the release notes.
2. Build the adapter image and run `tests/hermes_picker_smoke.py` inside it.
3. Run the s6-overlay smoke with `tests/hermes_gateway_stub.py`; it verifies the
   exact shared/admin tool manifest, plugin registration, private visibility
   resolver, effective platform hint, and search-only provider boundary.
4. Verify the native adapter still exposes `_handle_callback_query`; CRBL `md:`
   media-card callbacks must be intercepted before all other callbacks delegate
   unchanged to that native handler. `verify_pinned_runtime` now fails closed on
   this contract at container start, and `native_adapter()` returns the bare
   fallback when the native class is missing so the registration check can still
   detect it by identity.
5. Verify `agent.system_prompt._resolve_platform_hint` still exists and is still
   the single site that resolves a platform's prompt hint. `install_platform_hint`
   wraps it so `PLATFORM_HINT` stays the only copy of the CRBL guidance instead
   of being mirrored into `platform_hints.telegram.append`. If Hermes stops
   preferring its built-in `PLATFORM_HINTS["telegram"]` over a plugin's
   registered hint, the wrapper can be dropped in favour of the value already
   passed to `register_platform`. `verify_pinned_runtime` fails closed when the
   wrapper is inactive or when the guidance no longer reaches a prompt resolved
   with no config override.
6. Verify a tab switch edits one card's poster in place, the active row changes
   from `○` to `●`, and Request movie performs the gateway request from the
   tap itself without posting callback text into the chat.
7. Verify an unanswered media picker interrupts its exact active Hermes session
   with no queued interrupt message. The interrupted turn must end without a
   follow-up API call or another unsolicited search card.
8. Run Ruff, strict mypy, the complete pytest suite, both hash-locked dependency
   audits, Compose validation, and the gateway container migration smoke.
9. Publish immutable images, deploy with a verified database backup, and check
   that Plex and unrelated agent containers retain their IDs.
