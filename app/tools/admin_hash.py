"""Generate an Argon2id hash for ADMIN_PASSWORD_HASH in the docker .env.

Usage:
    python -m app.tools.admin_hash "your-admin-password"

Copy the printed `argon2...` string into .env as ADMIN_PASSWORD_HASH. The
admin password is never stored in plaintext by the app — only this hash is
compared at login via the same Argon2id parameters used for every user.
"""

from __future__ import annotations

import sys

from app.security import hash_password


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1]:
        sys.stderr.write('usage: python -m app.tools.admin_hash "<password>"\n')
        return 2
    password = sys.argv[1]
    # Reuse the app's Argon2id parameters so the hash is directly comparable.
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
