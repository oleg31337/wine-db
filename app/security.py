"""Authentication & session security.

- Argon2id password hashing (argon2-cffi).
- Stateless session JWT in an HttpOnly, SameSite=Strict cookie.
- Double-submit CSRF token bound to the session id.
"""

from __future__ import annotations

import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import User

SESSION_COOKIE = "winedb_session"
CSRF_COOKIE = "winedb_csrf"
CSRF_HEADER = "X-CSRF-Token"
ALGORITHM = "HS256"

_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 128


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Username must be 3-32 chars: letters, digits, dot, dash, underscore.",
        )
    return username


def validate_password(password: str) -> str:
    if not (MIN_PASSWORD_LEN <= len(password or "") <= MAX_PASSWORD_LEN):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Password must be {MIN_PASSWORD_LEN}-{MAX_PASSWORD_LEN} characters.",
        )
    classes = sum(
        bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    if classes < 3:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password needs at least 3 of: lowercase, uppercase, digit, symbol.",
        )
    return password


def create_session_token(user: User, settings: Settings, session_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "usr": user.username,
        "tv": user.token_version,
        "sid": session_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.session_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def csrf_token_for(session_id: str, settings: Settings) -> str:
    return hmac.new(settings.secret_key.encode(), session_id.encode(), "sha256").hexdigest()


def issue_session(response: Response, user: User, settings: Settings) -> str:
    session_id = secrets.token_urlsafe(24)
    token = create_session_token(user, settings, session_id)
    max_age = settings.session_ttl_minutes * 60
    common = dict(
        max_age=max_age,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
        domain=settings.cookie_domain,
    )
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)
    # Readable by JS on purpose: double-submit CSRF pattern.
    response.set_cookie(CSRF_COOKIE, csrf_token_for(session_id, settings), httponly=False, **common)
    return token


def clear_session(response: Response, settings: Settings) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", domain=settings.cookie_domain)


def _decode(token: str, settings: Settings) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:  # expired / tampered / malformed
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session") from exc


def current_user(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = _decode(token, settings)
    user = db.scalar(select(User).where(User.id == payload.get("sub")))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if int(payload.get("tv", 0)) != user.token_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    request.state.session_id = payload.get("sid", "")
    return user


def require_admin(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """Resolve the caller and require the admin role.

    Used to gate user provisioning and database import/export. A non-admin
    authenticated user gets 403 (not 401) so the client can tell "logged in but
    not allowed" apart from "not logged in".
    """
    user = current_user(request, db, settings)
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return user


def optional_user(request: Request, db: Session, settings: Settings) -> User | None:
    """Resolve the caller without raising when the session is absent/invalid.

    Used by logout, which must succeed (and be idempotent) even when the token
    is already expired or has been invalidated elsewhere.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        payload = _decode(token, settings)
    except HTTPException:
        return None
    user = db.scalar(select(User).where(User.id == payload.get("sub")))
    if user is None or not user.is_active:
        return None
    if int(payload.get("tv", 0)) != user.token_version:
        return None
    return user


def require_csrf(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Enforce double-submit CSRF for all state-changing requests."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    session_id = _decode(token, settings).get("sid", "")
    sent = request.headers.get(CSRF_HEADER, "")
    expected = csrf_token_for(session_id, settings)
    if not sent or not hmac.compare_digest(sent, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")


def bootstrap_admin(db: Session, settings: Settings, log) -> None:
    """Provision the admin account from the environment on first boot.

    Idempotent: if a user with ADMIN_USERNAME already exists we ensure it stays
    an admin and keep the configured hash (rotating it when the env hash
    changes). Refuses to run in production if the credentials are missing, so an
    exposed instance can never end up with no admin and no way to create one.
    """
    is_prod = settings.environment.strip().lower() in {"production", "prod"}
    username = (settings.admin_username or "").strip()
    pw_hash = (settings.admin_password_hash or "").strip()

    if not username or not pw_hash:
        if is_prod:
            raise RuntimeError(
                "ADMIN_USERNAME and ADMIN_PASSWORD_HASH must be set in production. "
                "Generate a hash with `python -m app.tools.admin_hash \"<password>\"`."
            )
        # Development: create a throwaway admin so the app is usable.
        import secrets as _secrets

        username = username or "admin"
        if not pw_hash:
            pw_hash = hash_password(_secrets.token_urlsafe(16))
            if log:
                log(
                    "DEV admin %r password auto-generated (not for production): %s",
                    username,
                    _secrets.token_urlsafe(8),
                )

    existing = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if existing is None:
        admin = User(
            username=username,
            password_hash=pw_hash,
            is_admin=True,
            is_active=True,
        )
        db.add(admin)
        db.commit()
        if log:
            log("admin account %r provisioned", username)
        return

    # Keep the seeded admin in sync with the environment.
    changed = False
    if not existing.is_admin:
        existing.is_admin = True
        changed = True
    if pw_hash and existing.password_hash != pw_hash:
        existing.password_hash = pw_hash
        existing.token_version += 1  # force re-login on rotation
        changed = True
    if changed and log:
        log("admin account %r updated from environment", username)
    if changed:
        db.commit()
