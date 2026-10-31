#!/bin/sh
# Credential-free transport fixture. Only test-generated values are sourced.
set -eu
test "$1" = run
test "$2" = --environment
test "$4" = --
test "$OP_SERVICE_ACCOUNT_TOKEN" = dummy-token
shift 4
. /fixtures/fetched.sh
exec "$@"
