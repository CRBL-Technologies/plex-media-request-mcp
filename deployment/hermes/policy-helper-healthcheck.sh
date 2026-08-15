#!/command/with-contenv sh
set -eu

python=/opt/hermes/.venv/bin/python

# The helper probe authenticates the exact status route and never prints the
# response body.  The contract probe verifies the loaded Telegram override and
# tool inventory in this process.  ``gateway run`` is the pinned image's /init
# main program rather than a ``gateway-default`` s6 longrun, so readiness uses
# Hermes' native runtime-status/PID checks instead of assuming that slot exists.
"$python" -m hermes_media_extension.policy_helper_api \
    --probe --host 127.0.0.1 --port 8787 >/dev/null
"$python" -m hermes_media_extension.startup_contract --json >/dev/null
/command/s6-svstat /run/service/policy-helper | grep -q '^up '
"$python" -c '
from gateway.status import get_running_pid, read_runtime_status

record = read_runtime_status()
if not isinstance(record, dict):
    raise SystemExit(1)
if record.get("gateway_state") not in {"running", "degraded"}:
    raise SystemExit(1)
if get_running_pid(cleanup_stale=False) is None:
    raise SystemExit(1)
'
