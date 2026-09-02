"""Anonymous, multi-session Web control console for Arbor.

The console is the only remotely exposed socket.  Each live Arbor child keeps
its existing interactive WebUI bound to loopback; this server gives each
browser a signed CSRF session, lists durable sessions, manages child processes, and proxies the
existing SSE/input contract through a same-origin ``/session/<id>/`` route.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..cli.replay import demo_recording, load_recording
from ..cli.tree_export import build_tree_html
from .auth import ConsoleAuth, Principal
from .manager import RunManager
from .session_source import build_session_snapshot


log = logging.getLogger(__name__)

_CONSOLE_HTML = Path(__file__).parent / "console.html"
_RUN_HTML = Path(__file__).parent / "index.html"
_MAX_JSON_BODY = 1024 * 1024
_MAX_UPLOAD_BODY = 64 * 1024 * 1024


class ControlConsoleServer:
    """Serve the launcher and proxy existing Arbor WebUIs."""

    def __init__(
        self,
        manager: RunManager,
        auth: ConsoleAuth,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.manager = manager
        self.auth = auth
        self.host = host
        self.port = int(port)
        self._httpd: ThreadingHTTPServer | None = None
        self._stop = threading.Event()

    @property
    def url(self) -> str:
        shown_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        return f"http://{shown_host}:{self.port}"

    def start(self) -> bool:
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), _ConsoleHandler)
        except OSError as exc:
            log.warning("control console could not bind %s:%s: %s", self.host, self.port, exc)
            return False
        self._httpd.daemon_threads = True
        self._httpd.console = self  # type: ignore[attr-defined]
        self.port = int(self._httpd.server_address[1])
        threading.Thread(target=self._httpd.serve_forever, name="arbor-console", daemon=True).start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

class _ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "arbor-console/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("console %s - %s", self.client_address[0], fmt % args)

    @property
    def console(self) -> ControlConsoleServer:
        return self.server.console  # type: ignore[attr-defined]

    @property
    def principal(self) -> Principal | None:
        return self.console.auth.principal_from_cookie(self.headers.get("Cookie"))

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/console.html"}:
            self._serve_file(_CONSOLE_HTML)
            return
        if path == "/healthz":
            self._serve_json(200, {"ok": True})
            return
        if path == "/api/me":
            principal = self.principal
            token: str | None = None
            if principal is None:
                token, principal = self.console.auth.issue()
            self.send_response(200)
            self._security_headers()
            if token is not None:
                self.send_header("Set-Cookie", self.console.auth.set_cookie_header(token))
            self._write_json_body({
                "ok": True,
                "csrf_token": principal.csrf_token,
                "workspace_root": str(self.console.manager.workspace_root),
                "default_model": os.environ.get("ARBOR_DEFAULT_MODEL", "qwen3.8-max"),
                "base_url": os.environ.get("OPENAI_BASE_URL", ""),
            })
            return
        if path == "/api/sessions":
            if self._require_auth():
                self._serve_json(200, {"ok": True, "sessions": self.console.manager.list_sessions()})
            return
        if path == "/api/uploads":
            if self._require_auth():
                self._serve_json(200, {"ok": True, "uploads": self.console.manager.list_uploads()})
            return
        if path == "/replay/demo":
            if not self._require_auth():
                return
            try:
                self._serve_html(build_tree_html(demo_recording()))
            except (OSError, ValueError, RuntimeError) as exc:
                self._serve_json(500, {"ok": False, "error": f"demo replay unavailable: {exc}"})
            return
        session_id, suffix = self._session_route(path)
        if session_id is not None:
            if not self._require_auth():
                return
            if suffix in {"", "/"}:
                try:
                    session = self.console.manager.resolve_session(session_id)
                    if not session.is_dir():
                        raise ValueError("session does not exist")
                except ValueError as exc:
                    self._serve_json(404, {"ok": False, "error": str(exc)})
                    return
                self._serve_file(_RUN_HTML)
                return
            if suffix == "/events":
                self._serve_session_events(session_id)
                return
            if suffix == "/replay":
                self._serve_session_replay(session_id)
                return
        self._serve_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        principal = self._require_auth()
        if principal is None:
            return
        if not self._valid_csrf(principal):
            self._serve_json(403, {"ok": False, "error": "invalid csrf token"})
            return
        if path == "/api/runs":
            try:
                job = self.console.manager.start_run(self._read_json())
            except ValueError as exc:
                self._serve_json(400, {"ok": False, "error": str(exc)})
                return
            except OSError as exc:
                log.exception("failed to launch Arbor run")
                self._serve_json(500, {"ok": False, "error": f"launch failed: {type(exc).__name__}"})
                return
            self._serve_json(201, {"ok": True, "run": job.as_dict()})
            return

        if path == "/api/uploads":
            try:
                upload = self._read_upload()
            except ValueError as exc:
                self._serve_json(400, {"ok": False, "error": str(exc)})
                return
            except OSError as exc:
                log.exception("failed to store dataset upload")
                self._serve_json(500, {"ok": False, "error": f"upload failed: {type(exc).__name__}"})
                return
            self._serve_json(201, {"ok": True, "upload": upload})
            return

        if path.startswith("/api/runs/") and path.endswith("/stop"):
            session_id = unquote(path[len("/api/runs/"):-len("/stop")]).strip("/")
            try:
                job = self.console.manager.stop_run(session_id)
            except ValueError as exc:
                self._serve_json(409, {"ok": False, "error": str(exc)})
                return
            self._serve_json(200, {"ok": True, "run": job.as_dict()})
            return

        session_id, suffix = self._session_route(path)
        if session_id is not None and suffix == "/input":
            self._proxy_input(session_id)
            return
        self._serve_json(404, {"ok": False, "error": "not found"})

    def _serve_session_replay(self, session_id: str) -> None:
        try:
            session = self.console.manager.resolve_session(session_id)
            if not session.is_dir():
                raise ValueError("session does not exist")
            html = build_tree_html(load_recording(session))
        except (FileNotFoundError, ValueError) as exc:
            self._serve_json(404, {"ok": False, "error": str(exc)})
            return
        except (OSError, RuntimeError) as exc:
            log.exception("failed to build session replay")
            self._serve_json(500, {"ok": False, "error": f"replay failed: {type(exc).__name__}"})
            return
        self._serve_html(html)

    def _serve_session_events(self, session_id: str) -> None:
        try:
            session = self.console.manager.resolve_session(session_id)
            if not session.is_dir():
                raise ValueError("session does not exist")
        except ValueError as exc:
            self._serve_json(404, {"ok": False, "error": str(exc)})
            return
        job = self.console.manager.get_job(session_id)
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        if job is not None and job.exit_code() is None:
            # Preflight may run for a while before the child binds its loopback
            # WebUI. Surface durable progress meanwhile, then switch to the live
            # interactive stream without making the browser reconnect.
            while job.exit_code() is None and not self.console._stop.is_set():
                if self._child_ready(job.port):
                    self._proxy_events(job.port)
                    return
                snap = build_session_snapshot(session, job.run_name)
                snap["phase"] = snap.get("phase") or "starting"
                snap["interactive"] = False
                if not self._sse_send({"kind": "snapshot", "state": snap}):
                    return
                time.sleep(1.5)

        # Completed/unmanaged sessions are durable read-only views.
        while not self.console._stop.is_set():
            snap = build_session_snapshot(session, session.name)
            snap["interactive"] = False
            if not self._sse_send({"kind": "snapshot", "state": snap}):
                return
            time.sleep(1.5)

    def _proxy_events(self, port: int) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/events")
            response = conn.getresponse()
            if response.status != 200:
                self._sse_send({"kind": "event", "type": "console.proxy_error",
                                "data": {"status": response.status}})
                return
            while not self.console._stop.is_set():
                line = response.fp.readline()  # type: ignore[union-attr]
                if not line:
                    return
                try:
                    self.wfile.write(line)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
        except OSError:
            self._sse_send({"kind": "event", "type": "console.proxy_disconnected", "data": {}})
        finally:
            conn.close()

    def _proxy_input(self, session_id: str) -> None:
        job = self.console.manager.get_job(session_id)
        if job is None or job.exit_code() is not None:
            self._serve_json(409, {"ok": False, "error": "session is not live"})
            return
        conn: http.client.HTTPConnection | None = None
        try:
            raw = json.dumps(self._read_json()).encode("utf-8")
            conn = http.client.HTTPConnection("127.0.0.1", job.port, timeout=10)
            conn.request("POST", "/input", body=raw, headers={
                "Content-Type": "application/json",
                "X-Arbor-Token": job.token,
            })
            response = conn.getresponse()
            body = response.read()
            content_type = response.getheader("Content-Type") or "application/json"
            self.send_response(response.status)
            self._security_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._serve_json(502, {"ok": False, "error": f"live WebUI unavailable: {type(exc).__name__}"})
        finally:
            try:
                if conn is not None:
                    conn.close()
            except OSError:
                pass

    @staticmethod
    def _child_ready(port: int) -> bool:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.4)
        try:
            conn.request("GET", "/healthz")
            return conn.getresponse().status == 200
        except OSError:
            return False
        finally:
            conn.close()

    def _sse_send(self, obj: dict[str, Any]) -> bool:
        try:
            raw = json.dumps(obj).encode("utf-8")
            self.wfile.write(b"data: " + raw + b"\n\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _require_auth(self) -> Principal | None:
        principal = self.principal
        if principal is None:
            self._serve_json(401, {"ok": False, "error": "authentication required"})
        return principal

    def _valid_csrf(self, principal: Principal) -> bool:
        supplied = self.headers.get("X-Arbor-CSRF") or ""
        return bool(supplied) and secrets.compare_digest(supplied, principal.csrf_token)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0 or length > _MAX_JSON_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid json") from exc
        if not isinstance(value, dict):
            raise ValueError("json body must be an object")
        return value

    def _read_upload(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length <= 0:
            raise ValueError("dataset file is empty")
        if length > _MAX_UPLOAD_BODY:
            raise ValueError("dataset file exceeds the 64 MiB limit")
        filename = unquote(self.headers.get("X-Arbor-Filename") or "").strip()
        if not filename:
            raise ValueError("missing dataset filename")
        return self.console.manager.save_upload(filename, self.rfile, length)

    @staticmethod
    def _session_route(path: str) -> tuple[str | None, str]:
        if not path.startswith("/session/"):
            return None, ""
        rest = path[len("/session/"):]
        session_id, sep, tail = rest.partition("/")
        return unquote(session_id) if session_id else None, ("/" + tail if sep else "")

    def _serve_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._serve_json(500, {"ok": False, "error": f"missing asset: {path.name}"})
            return
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self, code: int, obj: Any) -> None:
        self.send_response(code)
        self._security_headers()
        self._write_json_body(obj)

    def _write_json_body(self, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
        )
