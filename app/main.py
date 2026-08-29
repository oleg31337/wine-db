"""wine-db application factory.

Security posture (TLS itself is terminated by the reverse proxy):
  * strict CSP, no inline scripts, no external origins
  * HttpOnly + SameSite=Strict session cookie, double-submit CSRF
  * per-IP rate limiting, hard request body cap
  * trusted-host filtering, proxy headers honoured for the client IP
  * every response carries hardening headers; API responses are never cached
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp

from app.config import Settings, get_settings
from app.db import get_sessionmaker, init_db
from app.ratelimit import RateLimitMiddleware, Rule
from app.routers import auth, backup, favorites, scan, wines

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    # Inline style attributes are used throughout the UI (and created at runtime
    # in JS), so 'unsafe-inline' for styles is required for the layout to render.
    # This is low-risk: style-src cannot execute script, and script-src stays
    # locked to 'self', so no injected style can run JavaScript.
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' blob: data:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=(), payment=()",
    "Content-Security-Policy": CSP,
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/api/"):
            # API responses are never cached - every read goes to the server so
            # the UI always shows authoritative data after a write.
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path.startswith("/assets/"):
            # Assets carry Last-Modified/ETag from StaticFiles but no explicit
            # Cache-Control, which lets browsers heuristically cache them. Force
            # revalidation so a freshly rebuilt container always ships its JS -
            # otherwise a stale detail.js (with old refresh logic) could keep
            # running after a deploy. Conditional GET keeps it fast.
            response.headers["Cache-Control"] = "no-cache"
        return response


class BodySizeLimitMiddleware:
    """Reject oversized bodies before they are buffered anywhere."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        limit = 512 * 1024 * 1024 if path.startswith("/api/backup/import") else self.max_bytes

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        declared = headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            await JSONResponse(
                {"detail": "Request body too large"},
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )(scope, receive, send)
            return

        seen = 0
        too_large = False

        async def guarded_receive():
            nonlocal seen, too_large
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, guarded_receive, send)
        if too_large:  # pragma: no cover - client already disconnected
            logger.info("rejected oversized body on %s", path)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Make sure the persistent data directory exists before anything writes
        # to it (DB file, uploads). Never destructive - init_db only creates
        # tables that are missing; it never drops data.
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
        Path(settings.uploads_dir).mkdir(parents=True, exist_ok=True)
        init_db()
        # Provision the admin account from the environment. In production this
        # hard-fails if ADMIN_USERNAME/ADMIN_PASSWORD_HASH are missing.
        SessionLocal = get_sessionmaker()
        with SessionLocal() as db:
            from app.security import bootstrap_admin

            bootstrap_admin(db, settings, logger.info)
        if settings.environment == "production" and len(settings.secret_key) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters in production")
        logger.info("wine-db ready (env=%s)", settings.environment)
        yield

    app = FastAPI(
        title="wine-db",
        description="Private tasting database",
        version="1.0.0",
        lifespan=lifespan,
        # No interactive docs on an internet-exposed surface.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    rate_rules = [
        Rule(
            limit=settings.rate_limit_login_per_minute,
            prefixes=("/api/auth/login", "/api/auth/register", "/api/auth/password"),
            methods=("POST",),
            name="auth",
        ),
        Rule(limit=settings.rate_limit_ai_per_minute, prefixes=("/api/scan/",), name="ai"),
        Rule(limit=6, window=300, prefixes=("/api/backup/",), name="backup"),
    ]
    app.add_middleware(
        RateLimitMiddleware,
        rules=rate_rules,
        default=Rule(limit=settings.rate_limit_api_per_minute, name="default"),
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_upload_bytes + 512 * 1024)
    if settings.trusted_host_list != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Return field-level errors without echoing back raw input."""
        errors = [
            {"field": ".".join(str(p) for p in err.get("loc", [])[1:]), "message": err.get("msg", "")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "Validation failed", "errors": errors},
        )

    app.include_router(auth.router)
    app.include_router(wines.router)
    app.include_router(favorites.router)
    app.include_router(scan.router)
    app.include_router(backup.router)

    @app.get("/api/health", include_in_schema=False)
    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict:
        """Liveness probe for docker/compose. Deliberately reveals nothing."""
        return {"status": "ok"}

    if STATIC_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(
                STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"}
            )

        @app.get("/manifest.webmanifest", include_in_schema=False)
        async def manifest() -> FileResponse:
            return FileResponse(STATIC_DIR / "manifest.webmanifest")

    return app


app = create_app()
