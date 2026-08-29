#!/bin/sh
# Container entrypoint for wine-db.
#
# The persistent data directory (/data) is a host bind mount. On a fresh start
# the host directory is owned by root, but the app runs as the unprivileged
# 'wine' user (uid 10001) and must be able to create /data/uploads. We are
# still root when this script starts, so we fix ownership once and then drop
# privileges for the actual server process via gosu.
set -e

DATA_UID=$(id -u wine)

# Only chown when necessary (avoids a slow recursive chown on every boot once
# the directory is already owned by the app user).
if [ "$(stat -c '%u' /data 2>/dev/null)" != "$DATA_UID" ]; then
    mkdir -p /data/uploads
    chown -R wine:wine /data
fi

# Drop to the unprivileged user for the real server process. gosu forwards
# signals (SIGTERM etc.) to the child so graceful shutdown still works.
exec gosu wine "$@"
