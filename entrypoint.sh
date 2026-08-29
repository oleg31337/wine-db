#!/bin/sh
# Container entrypoint for wine-db.
#
# The persistent data directory (/data) is a host bind mount. We want the app
# to run as the unprivileged 'wine' user, but on a fresh start the host
# directory may be owned by root (or by a uid that doesn't match the container).
#
# Strategy:
#   1. As root, try to take ownership of /data so the 'wine' user can write.
#      This works on normal Docker hosts.
#   2. If chown is NOT permitted (e.g. /data lives on a FAT/exFAT/NTFS volume or
#      a CIFS/NAS mount where ownership changes are unsupported, or a hardened
#      userns-remap setup), chown returns EPERM. In that case we cannot make the
#      directory owned by 'wine', so we fall back to running the server as ROOT,
#      which can still write to the mount (root bypasses perms / FAT masks allow
#      it). This keeps a fresh start working instead of crashing on boot.
#
# Either way the app can write to /data/uploads, which is what matters.
set -e

DATA_UID=$(id -u wine)

# Only attempt chown when the directory isn't already owned by the app user.
if [ "$(stat -c '%u' /data 2>/dev/null)" != "$DATA_UID" ]; then
    mkdir -p /data/uploads
    if chown -R wine:wine /data 2>/dev/null; then
        : # ownership fixed; we will drop to 'wine' below
    else
        # chown not permitted on this mount. Run the server as root so it can
        # still write to /data (root write works on FAT masks and 755 root dirs).
        echo "wine-db: warning: cannot chown /data to the app user (filesystem does" \
             "not support changing ownership, e.g. exFAT/NTFS/CIFS or userns-remap);" \
             "running the server as root so it can write to the bind mount."
        exec "$@"
    fi
fi

# Drop to the unprivileged user for the real server process. gosu forwards
# signals (SIGTERM etc.) to the child so graceful shutdown still works.
exec gosu wine "$@"
