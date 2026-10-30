# Hermes pin migration checklist

`src/hermes_media/compat.py` is the only production module allowed to touch
private Hermes APIs. A Hermes base-image digest change is an integration
migration, not an unattended dependency bump.

The media adapter isolates both ordinary group and topic sessions per user.
`isolate_actor_sessions` must update the adapter's queue settings AND the
session store's canonical key builder for Telegram sources before dispatch.
It must not change global session flags for other platforms. Shared unmentioned
group observation is disabled because native Hermes anonymizes those turns.
The registered message handler rechecks the sender and role when a queued turn
actually executes; intake context alone is not an authorization boundary.
Persisted native replay bypasses that adapter handler, so the compatibility layer
also wraps the runner's principal check and execution entry. It reads the current
transport policy and resolves the current adapter on each use. The native bot-loop
guard and other platforms remain unchanged. A restored actor requires native
admission context and the matching restored transport binding.
Execution initializes the current delivery loop even after a cold replay.
Removing the handler during native draining must leave a real `None`, not a wrapper.
Native queued continuations may retain an existing actor only when their source
user and chat still match. The wrapped handler marks only gateway-authorized
messages with the native `role_authorized` field. It does not authorize the
whole adapter. Callback admission reads the shared policy from
the receiving transport's `HERMES_HOME/.env` on each tap through the native scoped
authorization callback. Secondary bots cannot borrow the primary policy. Missing
or invalid policy denies access. Native
callback dispatch and picker state remain in place. Every model/choice picker action
must match both its stored per-user session key and its message ID before it can
change state. Navigation and cancellation use the same check; missing ownership
fields deny the action. The callback sender is the user who tapped, not the bot
that posted the message. Poster cards
use native topic/reply routing helpers.

Run the behavioral integration cases against the pinned Hermes source as well
as the standalone suite (the latter intentionally skips native-only tests):

```sh
PYTHONPATH=src:/path/to/hermes-v0.22.5 pytest -q tests/test_hermes.py -k pinned_runtime
```

Before changing the pinned digest:

1. Confirm the upstream release is still `v0.22.5` at commit
   `bf0203a794a07aac3ff764972ab9087baa4e4f4e`. The native install stamp
   identifies this release; its package metadata reports `0.0.0`. The selected
   base digest `9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d`
   was inspected with Python 3.14.9. The Hermes lock targets Python 3.14; it is not
   the previous Python 3.13 lock or previous base digest. Update the
   release and commit constants only after reviewing the new runtime.
2. Run the s6-overlay smoke with `tests/hermes_gateway_stub.py`; it verifies the
   exact shared/admin tool manifest, plugin registration, private visibility
   resolver, effective platform hint, and search-only provider boundary.
3. Verify the native Telegram formatting hint in
   `plugins.platforms.telegram.adapter_hint.PLATFORM_HINT`. Registration combines
   it with CRBL guidance. `install_platform_hint` wraps
   `agent.system_prompt._resolve_platform_hint` so a user override cannot remove
   CRBL guidance. The startup gate checks the registered hint and its resolved
   text. Do not copy guidance into user configuration.
4. Verify the identity slot still loads from a file. `hermes_constants.get_hermes_home`
   and `agent.prompt_builder.load_soul_md` are the two names the init script uses
   to install `SOUL.md` and to read it back; `load_soul_md` returning `None` for
   an unreadable file is why the read-back exists at all. If Hermes gains a
   supported way to supply the identity in process, `install_soul` and its write
   to a bind-mounted host directory can be dropped for it.
5. Run Ruff, strict mypy, the complete pytest suite, native compatibility cases,
   both hash-locked dependency audits, Compose validation, and both container
   startup checks. The managed check uses disposable schema-36 data and tests
   the real launcher with baked and selected dependency environments. CRBL
   packages must remain under immutable `/opt/hermes` because native launchers
   replace `PYTHONPATH`.
6. Publish immutable images, deploy with a verified database backup, and check
   that Plex and unrelated agent containers retain their IDs.
