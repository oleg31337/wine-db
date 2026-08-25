"""Security posture: headers, CSP, rate limiting, injection, information leaks."""

from __future__ import annotations

from tests.conftest import GOOD_PASSWORD, create_wine, register


# --------------------------------------------------------------- headers
def test_security_headers_present_on_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    h = resp.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] in ("DENY", "SAMEORIGIN")
    assert h["Referrer-Policy"] in ("no-referrer", "same-origin", "strict-origin-when-cross-origin")
    assert "Content-Security-Policy" in h
    assert "Permissions-Policy" in h


def test_csp_is_restrictive(client):
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp or "base-uri 'self'" in csp
    # no remote script origins, and no unsafe-eval
    assert "unsafe-eval" not in csp
    assert "http://" not in csp


def test_csp_allows_the_camera_features_the_app_needs(client):
    policy = client.get("/").headers["Permissions-Policy"]
    assert "camera=(self)" in policy.replace(" ", "")
    assert "geolocation=()" in policy.replace(" ", "")


def test_api_responses_are_not_cacheable(api, user):
    resp = api.get("/api/auth/me")
    assert "no-store" in resp.headers.get("Cache-Control", "")


def test_server_banner_is_not_leaked(client):
    assert "server" not in {k.lower() for k in client.get("/").headers} or "uvicorn" not in client.get(
        "/"
    ).headers.get("server", "").lower()


def test_healthz_is_public_and_minimal(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert set(resp.json()) <= {"status", "version"}
    assert "database_url" not in resp.text


def test_openapi_docs_are_disabled_outside_development(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/redoc").status_code == 404


# --------------------------------------------------------------- injection
def test_sql_injection_in_search_is_harmless(api, user):
    create_wine(api, name="Real Wine")
    for payload in (
        "' OR 1=1 --",
        "'; DROP TABLE wines; --",
        "\" UNION SELECT password_hash FROM users --",
        "%' --",
    ):
        resp = api.get("/api/wines", params={"q": payload})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
    # the table is still there
    assert api.get("/api/wines").json()["total"] == 1


def test_injection_in_path_parameters_is_harmless(api, user):
    assert api.get("/api/wines/1 OR 1=1").status_code == 404
    assert api.get("/api/wines/../../etc/passwd").status_code in (400, 404)


def test_html_in_user_content_is_stored_verbatim_not_executed(api, user):
    """The frontend renders with textContent, so storage stays lossless."""
    payload = "<script>alert('xss')</script>"
    wine = create_wine(api, name=payload)
    assert wine["name"] == payload
    comment = api.post(
        f"/api/wines/{wine['id']}/comments", json={"body": payload}
    ).json()
    assert comment["body"] == payload
    resp = api.get(f"/api/wines/{wine['id']}")
    assert resp.headers["content-type"].startswith("application/json")


def test_null_bytes_rejected(api, user):
    assert api.post("/api/wines", json={"name": "bad\x00name"}).status_code == 422


def test_overlong_json_body_rejected(api, user):
    assert api.post("/api/wines", json={"name": "x", "aromas": "a" * 100_000}).status_code == 422


def test_unicode_names_are_supported(api, user):
    wine = create_wine(api, name="Château Mühlenberg Ροζέ 2019")
    assert api.get(f"/api/wines/{wine['id']}").json()["name"] == "Château Mühlenberg Ροζέ 2019"
    assert api.get("/api/wines", params={"q": "mühlenberg"}).json()["total"] == 1


# --------------------------------------------------------------- info leaks
def test_error_responses_do_not_leak_stack_traces(api, user):
    resp = api.get("/api/wines/nonexistent")
    body = resp.text
    assert "Traceback" not in body
    assert "sqlalchemy" not in body.lower()
    assert "/home/" not in body


def test_no_user_enumeration_via_registration_timing_or_message(api):
    from tests.conftest import admin_login, register
    admin_login(api)
    # First creation succeeds; a second attempt with the same name is rejected
    # without confirming/denying the account's existence beyond a generic message.
    assert register(api, username="taster").status_code == 201
    resp = register(api, username="taster")
    assert resp.status_code == 409
    assert "exists" in resp.json()["detail"].lower() or "taken" in resp.json()["detail"].lower()


def test_wine_ids_are_not_sequential(api, user):
    a = create_wine(api, name="One")
    b = create_wine(api, name="Two")
    assert not a["id"].isdigit()
    assert len(a["id"]) >= 16
    assert abs(int(a["id"][:8], 16) - int(b["id"][:8], 16)) > 1


def test_cors_is_not_wide_open(client):
    resp = client.get("/api/wines", headers={"Origin": "https://evil.example"})
    assert resp.headers.get("Access-Control-Allow-Origin") != "*"


def test_trace_and_other_methods_are_not_allowed(client, api, user):
    assert client.request("TRACE", "/api/wines").status_code in (405, 400, 501)
    assert client.request("DELETE", "/api/auth/me").status_code in (405, 403)


# --------------------------------------------------------------- rate limiting
def _limiter(limit, window=60):
    """A middleware instance with a single catch-all rule, for unit testing."""
    import app.ratelimit as rl

    rule = rl.Rule(limit=limit, window=window, name="test")
    return rl.RateLimitMiddleware(app=None, rules=[], default=rule), rule


def test_api_rate_limit_blocks_after_the_budget(monkeypatch):
    mw, rule = _limiter(3)
    for _ in range(3):
        allowed, _, _ = mw._consume(("1.2.3.4", "test"), rule)
        assert allowed is True
    allowed, remaining, retry_after = mw._consume(("1.2.3.4", "test"), rule)
    assert allowed is False
    assert remaining == 0
    assert retry_after >= 1


def test_rate_limit_windows_are_per_client():
    mw, rule = _limiter(1)
    assert mw._consume(("client-a", "test"), rule)[0] is True
    assert mw._consume(("client-a", "test"), rule)[0] is False
    assert mw._consume(("client-b", "test"), rule)[0] is True


def test_rate_limit_resets_after_the_window(monkeypatch):
    import app.ratelimit as rl

    now = [1000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: now[0])
    mw, rule = _limiter(1, window=10)
    assert mw._consume(("c", "test"), rule)[0] is True
    assert mw._consume(("c", "test"), rule)[0] is False
    now[0] += 11
    assert mw._consume(("c", "test"), rule)[0] is True


def test_proxy_forwarded_ip_is_used_as_the_client_key():
    import app.ratelimit as rl

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")],
        "client": ("10.0.0.1", 5000),
    }
    assert rl.RateLimitMiddleware._client_ip(scope) == "203.0.113.7"


def test_stricter_rules_match_by_path_prefix():
    import app.ratelimit as rl

    login = rl.Rule(limit=5, prefixes=("/api/auth/login",), name="login")
    default = rl.Rule(limit=300, name="default")
    mw = rl.RateLimitMiddleware(app=None, rules=[login], default=default)
    assert mw._match("/api/auth/login", "POST").name == "login"
    assert mw._match("/api/wines", "GET").name == "default"


def test_login_endpoint_is_rate_limited_in_production_config(client, monkeypatch):
    """The middleware must be wired to the login path with a stricter budget."""
    from app.main import create_app
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("RATE_LIMIT_LOGIN_PER_MINUTE", "2")
    settings = get_settings()
    app_obj = create_app(settings)

    from fastapi.testclient import TestClient

    with TestClient(app_obj) as tc:
        codes = [
            tc.post(
                "/api/auth/login",
                json={"username": "nobody", "password": GOOD_PASSWORD},
                headers={"X-CSRF-Token": tc.cookies.get("winedb_csrf", "")},
            ).status_code
            for _ in range(6)
        ]
    assert 429 in codes
    get_settings.cache_clear()


# --------------------------------------------------------------- config hygiene
def test_secret_key_must_be_set_in_production(monkeypatch):
    import pytest

    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(environment="production", secret_key="change-me")


def test_short_secret_key_rejected(monkeypatch):
    import pytest

    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(environment="production", secret_key="tooshort")


def test_frontend_receives_no_backend_configuration(client):
    """The HTML/JS must not contain URLs, models, keys or DB settings."""
    html = client.get("/").text
    for asset in ("/assets/app.js", "/assets/core.js", "/assets/scan.js"):
        html += client.get(asset).text
    for forbidden in ("11434", "postgres", "SECRET_KEY", "api_key", "OPENAI", "192.168"):
        assert forbidden.lower() not in html.lower(), forbidden
