from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from arbor.webui.auth import ConsoleAuth
from arbor.webui.console_server import ControlConsoleServer
from arbor.webui.manager import ManagedRun, RunManager


def test_console_html_exposes_open_interactive_controls() -> None:
    html = (Path(__file__).parents[1] / "src" / "webui" / "console.html").read_text(
        encoding="utf-8"
    )
    for marker in (
        "Arbor Experiment Dashboard",
        "Arbor AI Assistant",
        "ASK 询问",
        "STEER 干预",
        "创建并启动",
        'data-gate="approve"',
    ):
        assert marker in html


class _Client:
    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie = ""

    def request(self, path: str, *, method: str = "GET", body: dict | None = None,
                csrf: str = "") -> tuple[int, dict | str, object]:
        headers: dict[str, str] = {}
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-Arbor-CSRF"] = csrf
        if self.cookie:
            headers["Cookie"] = self.cookie
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            payload = response.read().decode("utf-8")
            response_headers = response.headers
            set_cookie = response_headers.get("Set-Cookie")
            if set_cookie:
                self.cookie = "" if "Max-Age=0" in set_cookie else set_cookie.split(";", 1)[0]
            content_type = response_headers.get("Content-Type", "")
            value = json.loads(payload) if "json" in content_type else payload
            return response.status, value, response_headers
        finally:
            connection.close()


class _LiveProcess:
    def poll(self) -> None:
        return None


def test_console_opens_without_login_and_serves_historical_sessions(tmp_path: Path) -> None:
    session = tmp_path / "project" / ".arbor" / "sessions" / "history"
    session.mkdir(parents=True)
    (session / "run_info.json").write_text(
        json.dumps({"run_name": "history", "task": "historical task"}), encoding="utf-8"
    )
    manager = RunManager(tmp_path)
    auth = ConsoleAuth(secret=b"a" * 32)
    server = ControlConsoleServer(manager, auth, port=0)
    if not server.start():
        pytest.skip("localhost sockets are unavailable in this environment")
    client = _Client(server.port)
    try:
        code, body, headers = client.request("/api/me")
        assert code == 200
        assert "HttpOnly" in headers.get("Set-Cookie", "")
        csrf = body["csrf_token"]

        code, body, _ = client.request("/api/sessions")
        assert code == 200
        assert body["sessions"][0]["run_name"] == "history"
        session_id = body["sessions"][0]["id"]
        code, page, headers = client.request(f"/session/{session_id}/")
        assert code == 200 and "<title>Arbor</title>" in page
        assert headers.get("X-Frame-Options") == "DENY"

        code, _, _ = client.request("/api/runs", method="POST", body={})
        assert code == 403
        assert csrf
    finally:
        server.stop()


def test_console_proxies_interactive_input_with_the_internal_run_token(tmp_path: Path) -> None:
    received: dict = {}
    internal_token = "child-only-token"

    class ChildHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200 if self.path == "/healthz" else 404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            received["token"] = self.headers.get("X-Arbor-Token")
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            raw = json.dumps({"ok": received["token"] == internal_token}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    child = ThreadingHTTPServer(("127.0.0.1", 0), ChildHandler)
    threading.Thread(target=child.serve_forever, daemon=True).start()
    session = tmp_path / "project" / ".arbor" / "sessions" / "live"
    session.mkdir(parents=True)
    manager = RunManager(tmp_path)
    session_id = manager.session_id(session)
    job = ManagedRun(
        session_id=session_id,
        run_name="live",
        project_dir=session.parents[2],
        session_dir=session,
        port=int(child.server_address[1]),
        token=internal_token,
        pid=12345,
        started_at="2026-08-30T00:00:00+00:00",
        log_path=session / "web_console.log",
        process=_LiveProcess(),  # type: ignore[arg-type]
    )
    manager._jobs[session_id] = job
    console = ControlConsoleServer(
        manager, ConsoleAuth(secret=b"b" * 32), port=0
    )
    assert console.start()
    client = _Client(console.port)
    try:
        _, session, _ = client.request("/api/me")
        code, body, _ = client.request(
            f"/session/{session_id}/input",
            method="POST",
            body={"type": "gate", "node_id": "n1", "value": "approve"},
            csrf=session["csrf_token"],
        )
        assert code == 200 and body == {"ok": True}
        assert received == {
            "token": internal_token,
            "body": {"type": "gate", "node_id": "n1", "value": "approve"},
        }
    finally:
        console.stop()
        child.shutdown()
        child.server_close()
