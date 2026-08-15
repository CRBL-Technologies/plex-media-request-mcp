#!/bin/sh
set -eu

# Compatibility shim only.  The pinned Hermes image owns the real container
# ENTRYPOINT at /opt/hermes/docker/entrypoint-dispatch.sh, which delegates to
# s6-overlay /init and preserves the native CMD (gateway run).  The derived
# image must not replace that dispatcher with a shell supervisor: the policy
# helper is an s6-rc longrun under deployment/hermes/s6-rc.d/policy-helper.
exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
