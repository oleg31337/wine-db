"""Per-client rate limiting.

A small fixed-window counter kept in-process. wine-db is a single-container
self-hosted app, so there is no need for a shared store; the point is to blunt
credential stuffing and AI-endpoint abuse from the internet.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass
class Rule:
    limit: int
    window: int = 60
    prefixes: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    name: str = "default"


@dataclass
class _Bucket:
    reset_at: float = 0.0
    count: int = 0


@dataclass
class _State:
    buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_sweep: float = 0.0


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, rules: list[Rule], default: Rule) -> None:
        self.app = app
        self.rules = rules
        self.default = default
        self._state = _State()

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _client_ip(scope: Scope) -> str:
        # Use scope["client"], the peer/real-client address as resolved by
        # uvicorn's proxy-headers handling. We deliberately do NOT parse the raw
        # X-Forwarded-For header here: a client can set it to anything, and
        # taking the first comma-separated value would let an attacker claim a
        # fresh arbitrary IP on every request, minting a new rate-limit bucket
        # each time (i.e. fully bypassing rate limiting).
        #
        # scope["client"] is trustworthy because uvicorn only rewrites it from
        # X-Forwarded-For when the direct peer is in --forwarded-allow-ips,
        # which Docker restricts to the reverse proxy (see entrypoint.sh /
        # FORWARDED_ALLOW_IPS). With the proxy fronting the traffic, this is the
        # real client IP; otherwise it falls back to the socket peer.
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _match(self, path: str, method: str) -> Rule:
        for rule in self.rules:
            if rule.methods and method not in rule.methods:
                continue
            if any(path.startswith(prefix) for prefix in rule.prefixes):
                return rule
        return self.default

    def _sweep(self, now: float) -> None:
        if now - self._state.last_sweep < 300:
            return
        self._state.last_sweep = now
        stale = [k for k, b in self._state.buckets.items() if b.reset_at < now]
        for key in stale:
            self._state.buckets.pop(key, None)

    def _consume(self, key: tuple[str, str], rule: Rule) -> tuple[bool, int, int]:
        now = time.monotonic()
        with self._state.lock:
            self._sweep(now)
            bucket = self._state.buckets.get(key)
            if bucket is None or bucket.reset_at <= now:
                bucket = _Bucket(reset_at=now + rule.window, count=0)
                self._state.buckets[key] = bucket
            bucket.count += 1
            remaining = max(0, rule.limit - bucket.count)
            retry_after = max(1, int(bucket.reset_at - now))
            return bucket.count <= rule.limit, remaining, retry_after

    def reset(self) -> None:
        """Test hook."""
        with self._state.lock:
            self._state.buckets.clear()

    # -- ASGI ------------------------------------------------------------
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if path == "/api/health":
            await self.app(scope, receive, send)
            return

        rule = self._match(path, method)
        allowed, remaining, retry_after = self._consume((self._client_ip(scope), rule.name), rule)
        if not allowed:
            response = JSONResponse(
                {"detail": "Rate limit exceeded. Slow down and try again shortly."},
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(rule.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
            await response(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-ratelimit-limit", str(rule.limit).encode()))
                headers.append((b"x-ratelimit-remaining", str(remaining).encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
