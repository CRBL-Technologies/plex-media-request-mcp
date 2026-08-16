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
   unchanged to that native handler.
5. Verify a tab switch edits one card's poster in place, the active row changes
   from `○` to `●`, and Request movie resolves explicit request intent without
   posting callback text into the chat.
6. Run Ruff, strict mypy, the complete pytest suite, both hash-locked dependency
   audits, Compose validation, and the gateway container migration smoke.
7. Publish immutable images, deploy with a verified database backup, and check
   that Plex and unrelated agent containers retain their IDs.
