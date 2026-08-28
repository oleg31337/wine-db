"""Authentication routes."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import User, utcnow
from app.schemas import (
    AdminPasswordResetRequest,
    DisplayNameUpdate,
    LoginRequest,
    PasswordChangeRequest,
    UserCreateRequest,
    UserOut,
)
from app.security import (
    optional_user,
    clear_session,
    current_user,
    hash_password,
    issue_session,
    needs_rehash,
    require_admin,
    require_csrf,
    validate_password,
    validate_username,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# In-process throttle for credential endpoints (defence in depth alongside the
# global rate limiter): per-username and per-IP failure windows.
_FAIL_WINDOW_SECONDS = 300
_MAX_FAILURES = 10
_failures: dict[str, deque[float]] = defaultdict(deque)

# A dummy hash so a login attempt for an unknown user costs the same as a real one
# (prevents username enumeration by timing).
_DUMMY_HASH = hash_password("wine-db-timing-equalizer-0000")


def _prune(key: str) -> deque[float]:
    now = time.monotonic()
    bucket = _failures[key]
    while bucket and now - bucket[0] > _FAIL_WINDOW_SECONDS:
        bucket.popleft()
    return bucket


def _check_locked(keys: list[str]) -> None:
    for key in keys:
        if len(_prune(key)) >= _MAX_FAILURES:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts. Try again later.",
            )


def _record_failure(keys: list[str]) -> None:
    now = time.monotonic()
    for key in keys:
        _prune(key).append(now)


def _clear_failures(keys: list[str]) -> None:
    for key in keys:
        _failures.pop(key, None)


def reset_throttle() -> None:
    """Test hook."""
    _failures.clear()


def _client_ip(request: Request) -> str:
    # Reverse proxy terminates TLS; ProxyHeadersMiddleware populates request.client.
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=UserOut)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UserOut:
    ip = _client_ip(request)
    keys = [f"ip:{ip}", f"user:{payload.username.lower()}"]
    _check_locked(keys)

    user = db.scalar(select(User).where(func.lower(User.username) == payload.username.lower()))
    stored_hash = user.password_hash if user else _DUMMY_HASH
    ok = verify_password(payload.password, stored_hash)

    if not user or not user.is_active or not ok:
        _record_failure(keys)
        # Deliberately identical message for every failure mode.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
        )

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    user.last_login_at = utcnow()
    _clear_failures(keys)
    issue_session(response, user, settings)
    return UserOut.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    request: Request,
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Log out server-side, not just in the browser.

    Clearing the cookie alone would leave the signed token valid until it
    expires, so anyone who captured it could keep using the account. Bumping
    the user's token version invalidates it immediately. This is best-effort:
    an already-invalid or absent token still logs out cleanly.
    """
    user = optional_user(request, db, settings)
    if user is not None:
        user.token_version += 1
        db.flush()
    clear_session(response, settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.patch("/me", response_model=UserOut)
def update_me(
    payload: DisplayNameUpdate,
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> UserOut:
    """Any signed-in user may change their own display name.

    An empty/whitespace-only value clears the name (falls back to the username
    everywhere it is shown). The username itself is immutable.
    """
    user.display_name = payload.display_name or None
    db.flush()
    return UserOut.model_validate(user)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: PasswordChangeRequest,
    response: Response,
    user: User = Depends(current_user),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Current password is incorrect"
        )
    validate_password(payload.new_password)
    user.password_hash = hash_password(payload.new_password)
    user.token_version += 1  # invalidate every existing session
    db.flush()
    out = Response(status_code=status.HTTP_204_NO_CONTENT)
    issue_session(out, user, settings)
    return out


@router.get("/users", response_model=list[UserOut])
def list_users(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[UserOut]:
    """Admin-only: list every account (active and disabled)."""
    rows = db.scalars(select(User).order_by(User.username))
    return [UserOut.model_validate(u) for u in rows]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    _admin: User = Depends(require_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> UserOut:
    """Admin-only: create a non-admin account. The admin picks the password."""
    username = validate_username(payload.username)
    validate_password(payload.password)
    if db.scalar(select(User).where(func.lower(User.username) == username.lower())):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")
    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        display_name=(payload.display_name or "").strip() or None,
        # New accounts are never admins; the only admin is the seeded one.
        is_admin=False,
        is_active=True,
    )
    db.add(user)
    db.flush()
    return UserOut.model_validate(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: str,
    admin: User = Depends(require_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    """Admin-only: remove an account. Prevents deleting yourself or the admin."""
    target = db.scalar(select(User).where(User.id == user_id))
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You cannot delete your own account"
        )
    if target.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="The admin account cannot be deleted"
        )
    db.delete(target)
    db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def reset_user_password(
    user_id: str,
    payload: AdminPasswordResetRequest,
    _admin: User = Depends(require_admin),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    """Admin-only: reset another account's password.

    The user's existing sessions are invalidated by bumping token_version, so a
    reset immediately logs them out everywhere (they must sign in with the new
    password). New accounts are created with a temporary password and can change
    it themselves; this endpoint lets the admin do it on their behalf.
    """
    target = db.scalar(select(User).where(User.id == user_id))
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    validate_password(payload.new_password)
    target.password_hash = hash_password(payload.new_password)
    target.token_version += 1
    db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
