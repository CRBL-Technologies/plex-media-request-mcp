#!/bin/sh
set -eu

APP_USER="${APP_USER:-app}"
APP_DIR="${APP_DIR:-/app}"
APP_HOME="${HERMES_HOME:-/opt/data}"
STATE_DIR="${PLEX_MEDIA_REQUEST_STATE_DIR:-$APP_HOME/state}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK_VALUE="${UMASK:-0002}"
CHOWN_STATE="${PLEX_MEDIA_REQUEST_CHOWN_STATE:-true}"

case "$PUID" in
    ''|*[!0-9]*)
        echo "PUID must be a numeric user ID" >&2
        exit 64
        ;;
esac

case "$PGID" in
    ''|*[!0-9]*)
        echo "PGID must be a numeric group ID" >&2
        exit 64
        ;;
esac

umask "$UMASK_VALUE"

if [ "$(id -u)" = "0" ]; then
    current_gid="$(id -g "$APP_USER")"
    if [ "$current_gid" != "$PGID" ] && ! getent group "$PGID" >/dev/null 2>&1; then
        groupmod -o -g "$PGID" "$APP_USER"
    fi
    current_uid="$(id -u "$APP_USER")"
    if [ "$current_uid" != "$PUID" ] || [ "$current_gid" != "$PGID" ]; then
        usermod -o -u "$PUID" -g "$PGID" "$APP_USER"
    fi

    mkdir -p "$STATE_DIR"
    if [ "$CHOWN_STATE" != "false" ]; then
        chown -R "$PUID:$PGID" "$STATE_DIR"
    fi
    chmod -R ug+rwX "$STATE_DIR"
    find "$STATE_DIR" -type d -exec chmod g+s {} +

    if [ -d "$APP_DIR" ]; then
        chown -R "$PUID:$PGID" "$APP_DIR"
        chmod -R u+rwX,go+rX "$APP_DIR"
    fi

    exec gosu "$APP_USER" "$@"
fi

exec "$@"
