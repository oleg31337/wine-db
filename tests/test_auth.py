"""Authentication, session, admin role and CSRF behaviour.

Registration is admin-only: there is no public signup endpoint. The admin
account is seeded from the environment (see conftest.ENV).
"""

from __future__ import annotations

from tests.conftest import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    GOOD_PASSWORD,
    admin_login,
    login,
    register,
)


def test_admin_is_bootstrapped_and_can_login(api, client):
    resp = api.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True

    session = client.cookies.get("winedb_session")
    assert session
    set_cookie_headers = "".join(resp.headers.get_list("set-cookie"))
    assert "HttpOnly" in set_cookie_headers
    assert "SameSite=strict" in set_cookie_headers.replace("SameSite=Strict", "SameSite=strict")
    assert client.cookies.get("winedb_csrf")


def test_me_requires_authentication(client):
    assert client.get("/api/auth/me").status_code == 401


def test_login_and_me_roundtrip(api, user):
    assert login(api).status_code == 200
    me = api.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "taster"
    assert me.json()["is_admin"] is False


def test_logout_clears_session(api, user, client):
    assert api.post("/api/auth/logout").status_code == 204
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401


def test_logout_invalidates_the_token_server_side(api, user, client):
    """A captured cookie must stop working the moment the user logs out."""
    stolen = client.cookies.get("winedb_session")
    stolen_csrf = client.cookies.get("winedb_csrf")
    assert api.post("/api/auth/logout").status_code == 204

    client.cookies.clear()
    client.cookies.set("winedb_session", stolen)
    client.cookies.set("winedb_csrf", stolen_csrf)
    assert client.get("/api/auth/me").status_code == 401


def test_logout_is_idempotent(api, user, client):
    assert api.post("/api/auth/logout").status_code == 204
    assert api.post("/api/auth/logout").status_code in (204, 401)


def test_wrong_password_is_rejected_with_generic_message(api, user):
    resp = api.post("/api/auth/login", json={"username": "taster", "password": "Wrong-Password-1!"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid username or password"


def test_unknown_user_gets_identical_error(api, user):
    resp = api.post("/api/auth/login", json={"username": "ghost", "password": GOOD_PASSWORD})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid username or password"


def test_username_is_case_insensitive_for_login(api, user):
    assert api.post("/api/auth/login", json={"username": "TASTER", "password": GOOD_PASSWORD}).status_code == 200


def test_public_registration_endpoint_does_not_exist(client):
    """There is no self-service signup route."""
    assert client.post("/api/auth/register", json={"username": "x", "password": GOOD_PASSWORD}).status_code == 404


def test_duplicate_username_conflicts(api):
    admin_login(api)
    assert register(api, username="taster").status_code == 201
    assert register(api, username="taster").status_code == 409
    assert register(api, username="TaStEr").status_code == 409


def test_weak_passwords_rejected(api):
    admin_login(api)
    assert register(api, username="weak1", password="short").status_code == 422
    assert register(api, username="weak2", password="alllowercaseonly").status_code == 422


def test_invalid_username_rejected(api):
    admin_login(api)
    assert register(api, username="ab").status_code == 422
    assert register(api, username="has space").status_code == 422
    assert register(api, username="bad!chars").status_code == 422


def test_new_accounts_are_never_admin(api):
    admin_login(api)
    resp = register(api, username="plain")
    assert resp.status_code == 201
    assert resp.json()["is_admin"] is False


def test_password_hash_is_never_returned(api, user):
    body = api.get("/api/auth/me").text
    assert "argon2" not in body
    assert "password" not in body.lower()


def test_state_change_without_csrf_header_is_forbidden(client, api, user):
    resp = client.post("/api/wines", json={"name": "No CSRF", "wine_type": "red"})
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


def test_state_change_with_wrong_csrf_token_is_forbidden(client, api, user):
    resp = client.post(
        "/api/wines",
        json={"name": "Bad CSRF", "wine_type": "red"},
        headers={"X-CSRF-Token": "0" * 64},
    )
    assert resp.status_code == 403


def test_get_requests_do_not_need_csrf(client, api, user):
    assert client.get("/api/wines").status_code == 200


def test_tampered_session_cookie_rejected(client, api, user):
    token = client.cookies.get("winedb_session")
    client.cookies.set("winedb_session", token[:-4] + "aaaa")
    assert client.get("/api/auth/me").status_code == 401


def test_password_change_invalidates_old_session(client, api, user):
    old_session = client.cookies.get("winedb_session")
    resp = api.post(
        "/api/auth/password",
        json={"current_password": GOOD_PASSWORD, "new_password": "Brand-New-Secret-9!"},
    )
    assert resp.status_code == 204

    client.cookies.clear()
    client.cookies.set("winedb_session", old_session)
    assert client.get("/api/auth/me").status_code == 401

    client.cookies.clear()
    assert api.post(
        "/api/auth/login", json={"username": "taster", "password": "Brand-New-Secret-9!"}
    ).status_code == 200


def test_password_change_requires_correct_current_password(api, user):
    resp = api.post(
        "/api/auth/password",
        json={"current_password": "Not-The-Password-1!", "new_password": "Another-Secret-9!"},
    )
    assert resp.status_code == 403


def test_user_can_change_own_display_name(api, user):
    """Any signed-in user may set a display name; it shows up on /me."""
    resp = api.patch("/api/auth/me", json={"display_name": "Oleg's Cellar"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "Oleg's Cellar"

    me = api.get("/api/auth/me").json()
    assert me["display_name"] == "Oleg's Cellar"


def test_clearing_display_name_falls_back_to_username(api, user):
    api.patch("/api/auth/me", json={"display_name": "Temp Name"})
    # Empty / whitespace-only value clears the display name.
    resp = api.patch("/api/auth/me", json={"display_name": "   "})
    assert resp.status_code == 200
    assert resp.json()["display_name"] is None


def test_display_name_change_requires_auth(client):
    assert client.patch("/api/auth/me", json={"display_name": "x"}).status_code == 401


def test_display_name_is_optional_and_immutable_username(api, user):
    """The username is not part of the payload, so it must not change."""
    before = api.get("/api/auth/me").json()["username"]
    api.patch("/api/auth/me", json={"display_name": "Renamed"})
    after = api.get("/api/auth/me").json()
    assert after["username"] == before
    assert after["display_name"] == "Renamed"


# ----------------------------------------------------------- admin authorization


def test_non_admin_cannot_list_users(api, user):
    assert api.get("/api/auth/users").status_code == 403


def test_non_admin_cannot_create_user(api, user):
    assert api.post("/api/auth/users", json={"username": "nope", "password": GOOD_PASSWORD}).status_code == 403


def test_non_admin_cannot_export_backup(api, user, client):
    assert client.get("/api/backup/export").status_code == 403


def test_admin_can_manage_users(api):
    admin_login(api)
    # list initially has just the seeded admin
    resp = api.get("/api/auth/users")
    assert resp.status_code == 200
    names = {u["username"] for u in resp.json()}
    assert "admin" in names

    # create
    created = register(api, username="newbie")
    assert created.status_code == 201
    assert created.json()["username"] == "newbie"

    # the new user now appears in the list
    names = {u["username"] for u in api.get("/api/auth/users").json()}
    assert "newbie" in names

    # delete (cannot delete self or the admin)
    uid = created.json()["id"]
    assert api.delete(f"/api/auth/users/{uid}").status_code == 204
    assert api.delete(f"/api/auth/users/{uid}").status_code == 404


def test_admin_cannot_delete_self_or_admin(api):
    admin_login(api)
    me = api.get("/api/auth/me").json()
    assert api.delete(f"/api/auth/users/{me['id']}").status_code == 403
    # the seeded admin id:
    users = api.get("/api/auth/users").json()
    admin_id = next(u["id"] for u in users if u["is_admin"])
    assert api.delete(f"/api/auth/users/{admin_id}").status_code == 403


def test_extra_fields_rejected_on_create(api):
    admin_login(api)
    resp = api.post(
        "/api/auth/users",
        json={"username": "extra", "password": GOOD_PASSWORD, "is_admin": True},
    )
    assert resp.status_code == 422


def test_login_throttle_locks_after_repeated_failures(api, user):
    for _ in range(10):
        api.post("/api/auth/login", json={"username": "taster", "password": "Wrong-Password-1!"})
    resp = api.post("/api/auth/login", json={"username": "taster", "password": GOOD_PASSWORD})
    assert resp.status_code == 429


# ------------------------------------------------------------- production guard


def test_production_refuses_to_boot_without_admin_hash():
    from app.config import Settings
    from app.db import Base
    from app.models import User
    from app.security import bootstrap_admin
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    s = Settings(
        environment="production",
        admin_username=None,
        admin_password_hash=None,
        database_url="sqlite://",
        data_dir="/tmp",
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    SL = sessionmaker(bind=engine)
    with SL() as db:
        try:
            bootstrap_admin(db, s, lambda *a, **k: None)
        except RuntimeError as e:
            assert "ADMIN" in str(e)
            return
    raise AssertionError("expected RuntimeError when admin hash missing in production")


def test_development_auto_generates_admin_when_missing():
    from app.config import Settings
    from app.db import Base
    from app.models import User
    from app.security import bootstrap_admin
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    s = Settings(
        environment="development",
        admin_username="admin",
        admin_password_hash=None,
        database_url="sqlite://",
        data_dir="/tmp",
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    SL = sessionmaker(bind=engine)
    with SL() as db:
        bootstrap_admin(db, s, lambda *a, **k: None)
        u = db.scalar(select(User).where(User.username == "admin"))
        assert u is not None
        assert u.is_admin is True
