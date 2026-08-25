"""Shared test fixtures. Every test runs against a fresh SQLite DB + temp uploads.

Settings are driven through the real environment so that `get_settings()` returns
the same object everywhere - both for `Depends(get_settings)` and for the direct
calls made by the DB layer.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.db as db_module
from app.config import Settings, get_settings
from app.routers import auth as auth_router

ENV = {
    "ENVIRONMENT": "test",
    "SECRET_KEY": "test-secret-key-that-is-long-enough-1234567890",
    "COOKIE_SECURE": "false",
    "WEB_SEARCH_ENABLED": "false",
    "ALLOW_OPEN_REGISTRATION": "true",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD_HASH": "$argon2id$v=19$m=65536,t=3,p=2$3gxbO/mGFtaJxhh4FNwsJg$YfadqH4wo0PmN5YhOQSC3csbtBkz0uiYIGlOJ4C0pFk",
    "RATE_LIMIT_API_PER_MINUTE": "100000",
    "RATE_LIMIT_LOGIN_PER_MINUTE": "100000",
    "RATE_LIMIT_AI_PER_MINUTE": "100000",
    "OLLAMA_BASE_URL": "",
    "OPENAI_BASE_URL": "",
}


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    (data_dir / "uploads").mkdir(parents=True)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    # Settings reads a .env file by default; make sure the repo's own .env cannot
    # leak into a test run.
    monkeypatch.chdir(tmp_path)

    get_settings.cache_clear()
    resolved = get_settings()
    assert resolved.database_url.startswith("sqlite:///")
    yield resolved
    get_settings.cache_clear()


@pytest.fixture
def client(settings: Settings):
    db_module.reset_engine()
    auth_router.reset_throttle()

    from app.main import create_app

    application = create_app(settings)
    with TestClient(application, base_url="http://testserver") as test_client:
        yield test_client

    db_module.reset_engine()


class Api:
    """Thin wrapper that always sends the CSRF header, like the real frontend."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"X-CSRF-Token": self.client.cookies.get("winedb_csrf", "")}
        headers.update(extra or {})
        return headers

    def get(self, url, **kw):
        return self.client.get(url, **kw)

    def post(self, url, **kw):
        kw["headers"] = self._headers(kw.pop("headers", None))
        return self.client.post(url, **kw)

    def put(self, url, **kw):
        kw["headers"] = self._headers(kw.pop("headers", None))
        return self.client.put(url, **kw)

    def patch(self, url, **kw):
        kw["headers"] = self._headers(kw.pop("headers", None))
        return self.client.patch(url, **kw)

    def delete(self, url, **kw):
        kw["headers"] = self._headers(kw.pop("headers", None))
        return self.client.delete(url, **kw)


@pytest.fixture
def api(client: TestClient) -> Api:
    return Api(client)


GOOD_PASSWORD = "Tasting-Room-2026!"

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = GOOD_PASSWORD


def admin_login(api: Api) -> dict:
    """Log in as the seeded admin (provisioned from the environment in tests)."""
    resp = api.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def register(api: Api, username: str = "taster", password: str = GOOD_PASSWORD, **extra):
    """Create a non-admin account via the admin endpoint.

    The caller's `api` must already be authenticated as the admin. Returns the
    raw response so tests can assert status codes.
    """
    payload = {"username": username, "password": password}
    payload.update(extra)
    return api.post("/api/auth/users", json=payload)


def login(api: Api, username: str = "taster", password: str = GOOD_PASSWORD):
    return api.post("/api/auth/login", json={"username": username, "password": password})


@pytest.fixture
def user(api: Api) -> dict:
    """Seed the admin, create a 'taster' account, then leave the session as taster."""
    admin_login(api)
    resp = register(api, username="taster")
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert login(api, "taster").status_code == 200
    return data


@pytest.fixture
def admin(api: Api) -> dict:
    """Log in as the seeded admin and leave the session as admin."""
    return admin_login(api)


@pytest.fixture
def second_user(api: Api, user: dict) -> dict:
    """Creates a second account via admin, then logs the FIRST user back in."""
    admin_login(api)
    resp = register(api, username="second")
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert login(api, "taster").status_code == 200
    return data


def make_image(size=(600, 800), color=(120, 40, 60), fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


def wine_payload(**overrides) -> dict:
    payload = {
        "name": "Château Test",
        "maker": "Domaine Pytest",
        "wine_type": "red",
        "country": "France",
        "region": "Bordeaux",
        "vintage": 2019,
        "grape": "Cabernet Sauvignon",
        "alcohol_pct": 13.5,
        "sugar_g_l": 2.0,
        "aromas": "blackcurrant, cedar",
        "acidity": 3,
        "sweetness": 1,
        "body": 4,
        "mouthfeel": 3,
        "wood": 2,
    }
    payload.update(overrides)
    return payload


def create_wine(api: Api, **overrides) -> dict:
    resp = api.post("/api/wines", json=wine_payload(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()
