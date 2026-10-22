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

# SECURITY: the only peers allowed to set X-Forwarded-For. This replaces the
# previous "--forwarded-allow-ips *" that trusted the header from ANY source.
# With "*" (or when the app parses the raw header), a client could claim a
# fresh arbitrary IP per request and fully bypass rate limiting / the login
# throttle. Restricting this to the reverse proxy means uvicorn rewrites
# scope["client"] to the real client IP ONLY when the direct peer is that proxy.
# Default is loopback, which covers an nginx running on the same host. If your
# proxy runs in another container/host, set FORWARDED_ALLOW_IPS to its address.
: "${FORWARDED_ALLOW_IPS:=127.0.0.1,::1}"

APP_CMD="uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1 \
    --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\" \
    --no-server-header --log-level info"

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
             "running the server as root so it can write to the bind mount." >&2
        exec sh -c "$APP_CMD"
    fi
fi

# Drop to the unprivileged user for the real server process. gosu forwards
# signals (SIGTERM etc.) to the child so graceful shutdown still works.
exec gosu wine sh -c "$APP_CMD"
