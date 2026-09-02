from __future__ import annotations

from arbor.webui.auth import COOKIE_NAME, ConsoleAuth


def test_anonymous_signed_cookie_round_trip() -> None:
    auth = ConsoleAuth(ttl_seconds=120, secret=b"s" * 32)
    token, issued = auth.issue(now=100)
    verified = auth.verify(token, now=101)
    assert verified == issued
    assert auth.verify(token + "tampered", now=101) is None
    assert auth.verify(token, now=220) is None


def test_cookie_flags_and_cookie_parsing() -> None:
    auth = ConsoleAuth(secret=b"k" * 32, secure_cookie=True)
    token, principal = auth.issue()
    header = auth.set_cookie_header(token)
    assert header.startswith(f"{COOKIE_NAME}=")
    for flag in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"):
        assert flag in header
    assert auth.principal_from_cookie(header).username == principal.username
