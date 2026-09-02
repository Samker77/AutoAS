"""Small, dependency-free browser-session helper for the Web console.

The console intentionally has no account/password login.  A browser that opens
the homepage receives a short-lived, HMAC-signed anonymous session cookie and a
per-session CSRF token used by the JSON mutation endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any


COOKIE_NAME = "arbor_console_session"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class Principal:
    username: str
    csrf_token: str
    expires_at: int


class ConsoleAuth:
    """Issue and validate signed anonymous browser sessions."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 8 * 60 * 60,
        secret: bytes | None = None,
        secure_cookie: bool = False,
    ) -> None:
        self.username = "anonymous"
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._secret = secret or secrets.token_bytes(32)
        self.secure_cookie = secure_cookie

    def issue(self, *, now: int | None = None) -> tuple[str, Principal]:
        issued_at = int(time.time() if now is None else now)
        principal = Principal(
            username=self.username,
            csrf_token=secrets.token_urlsafe(24),
            expires_at=issued_at + self.ttl_seconds,
        )
        payload = {
            "u": principal.username,
            "c": principal.csrf_token,
            "exp": principal.expires_at,
            "n": secrets.token_urlsafe(8),
        }
        encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = _b64encode(hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}", principal

    def verify(self, token: str | None, *, now: int | None = None) -> Principal | None:
        if not token or "." not in token:
            return None
        encoded, supplied_signature = token.rsplit(".", 1)
        expected = _b64encode(hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied_signature, expected):
            return None
        try:
            payload: dict[str, Any] = json.loads(_b64decode(encoded))
            principal = Principal(
                username=str(payload["u"]),
                csrf_token=str(payload["c"]),
                expires_at=int(payload["exp"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        current = int(time.time() if now is None else now)
        if principal.username != self.username or principal.expires_at <= current:
            return None
        return principal

    def principal_from_cookie(self, cookie_header: str | None) -> Principal | None:
        if not cookie_header:
            return None
        jar = SimpleCookie()
        try:
            jar.load(cookie_header)
        except Exception:
            return None
        morsel = jar.get(COOKIE_NAME)
        return self.verify(morsel.value if morsel else None)

    def set_cookie_header(self, token: str) -> str:
        parts = [
            f"{COOKIE_NAME}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={self.ttl_seconds}",
        ]
        if self.secure_cookie:
            parts.append("Secure")
        return "; ".join(parts)
