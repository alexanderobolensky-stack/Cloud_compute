"""Request guard.

The portal can launch arbitrary commands, so it must not be reachable by other
web pages open in the same browser. Three layers:

* Host header allowlist - defeats DNS rebinding against 127.0.0.1.
* Mutating requests need the custom ``X-Portal-Request`` header, which a
  cross-origin page cannot send without a CORS preflight (which we never grant).
  WebSockets must come from a same-host Origin.
* Optional shared token (``PORTAL_TOKEN``) for setups bound to a non-loopback address.
"""

from __future__ import annotations

import hmac
import json
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_only(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


class GuardMiddleware:
    def __init__(self, app, token: str | None, allowed_hosts: list[str]):
        self.app = app
        self.token = token
        self.allowed = LOCAL_HOSTS | {h.lower() for h in allowed_hosts}

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = _host_only(headers.get("host", ""))
        if host not in self.allowed and "*" not in self.allowed:
            return await self._deny(scope, send, 421, "Host not allowed")
        path = scope.get("path", "")
        protected = path.startswith("/api/") or path.startswith("/ws")
        if scope["type"] == "websocket":
            origin = headers.get("origin")
            if origin and _host_only(urlsplit(origin).netloc) != host:
                return await self._deny(scope, send, 403, "Cross-origin WebSocket")
        elif protected and scope["method"] not in SAFE_METHODS and headers.get("x-portal-request") != "1":
            return await self._deny(scope, send, 403, "Missing X-Portal-Request header")
        if self.token and protected and path != "/api/auth" and not self._authorized(scope, headers):
            return await self._deny(scope, send, 401, "Token required")
        return await self.app(scope, receive, send)

    def _authorized(self, scope, headers: dict) -> bool:
        candidates = []
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            candidates.append(auth[7:].strip())
        if "cookie" in headers:
            cookie = SimpleCookie()
            cookie.load(headers["cookie"])
            if "portal_token" in cookie:
                candidates.append(cookie["portal_token"].value)
        query = parse_qs(scope.get("query_string", b"").decode())
        candidates.extend(query.get("token", []))
        return any(hmac.compare_digest(c.encode(), self.token.encode()) for c in candidates)

    async def _deny(self, scope, send, status: int, detail: str):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4000 + status})
            return
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
