"""Process and durable-session management for the authenticated Web console."""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


_RUN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_CONTROL_FILE = ".webui-control.json"
_SCAN_SKIP = {".git", ".venv", "venv", "node_modules", "data", "__pycache__"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _encode_relative(path: Path, root: Path) -> str:
    rel = str(path.relative_to(root)).encode("utf-8")
    return base64.urlsafe_b64encode(rel).decode("ascii").rstrip("=")


def _decode_relative(value: str, root: Path) -> Path:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except Exception as exc:
        raise ValueError("invalid session id") from exc
    path = (root / raw).resolve()
    if not _inside(path, root):
        raise ValueError("session escapes workspace root")
    parts = path.relative_to(root).parts
    if len(parts) < 3 or parts[-3:-1] != (".arbor", "sessions"):
        raise ValueError("not an Arbor session path")
    return path


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.15):
            return True
    except OSError:
        return False


def _pid_alive(pid: int, session_dir: Path | None = None) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # On Linux, guard against PID reuse before treating a recovered job as ours.
    cmdline = Path(f"/proc/{pid}/cmdline")
    if session_dir is not None and cmdline.exists():
        try:
            text = cmdline.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            return str(session_dir) in text and "arbor.cli.app" in text
        except OSError:
            return False
    return session_dir is None


@dataclass
class ManagedRun:
    session_id: str
    run_name: str
    project_dir: Path
    session_dir: Path
    port: int
    token: str
    pid: int
    started_at: str
    log_path: Path
    process: subprocess.Popen | None = field(default=None, repr=False)
    stopped_by_user: bool = False

    def exit_code(self) -> int | None:
        if self.process is not None:
            return self.process.poll()
        return None if _pid_alive(self.pid, self.session_dir) else -1

    def status(self) -> str:
        code = self.exit_code()
        if code is None:
            return "running" if _port_ready(self.port) else "starting"
        if self.stopped_by_user:
            return "stopped"
        return "completed" if code == 0 else "failed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "run_name": self.run_name,
            "project": str(self.project_dir),
            "session_dir": str(self.session_dir),
            "status": self.status(),
            "interactive": self.exit_code() is None,
            "started_at": self.started_at,
            "log_path": str(self.log_path),
        }


class RunManager:
    """Launch Arbor runs under a bounded workspace and enumerate sessions."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        python_executable: str | None = None,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        port_allocator: Callable[[], int] = _allocate_port,
        max_scan_depth: int = 4,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"workspace root does not exist: {root}")
        self.workspace_root = root
        self.python_executable = python_executable or sys.executable
        self._popen = popen_factory
        self._port_allocator = port_allocator
        self.max_scan_depth = max(1, max_scan_depth)
        self._jobs: dict[str, ManagedRun] = {}
        self._lock = threading.RLock()

    def validate_project(self, raw: str) -> Path:
        value = Path(raw or ".").expanduser()
        path = (self.workspace_root / value).resolve() if not value.is_absolute() else value.resolve()
        if not path.exists() or not path.is_dir():
            raise ValueError("project directory does not exist")
        if not _inside(path, self.workspace_root):
            raise ValueError("project directory must be inside the configured workspace root")
        return path

    def session_id(self, session_dir: Path) -> str:
        path = Path(session_dir).resolve()
        if not _inside(path, self.workspace_root):
            raise ValueError("session is outside workspace root")
        return _encode_relative(path, self.workspace_root)

    def resolve_session(self, session_id: str) -> Path:
        return _decode_relative(session_id, self.workspace_root)

    def start_run(self, payload: dict[str, Any]) -> ManagedRun:
        project = self.validate_project(str(payload.get("project") or "."))
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        if len(prompt) > 32_768:
            raise ValueError("prompt is too long")

        requested = str(payload.get("run_name") or "").strip()
        run_name = requested or f"web_{datetime.now():%Y%m%d_%H%M%S}"
        if not _RUN_NAME_RE.fullmatch(run_name):
            raise ValueError("run_name may contain only letters, digits, '.', '_' and '-'")

        session_dir = (project / ".arbor" / "sessions" / run_name).resolve()
        if not _inside(session_dir, project):
            raise ValueError("invalid session path")
        if session_dir.exists() and any(session_dir.iterdir()):
            raise ValueError("a session with this run_name already exists")
        session_dir.mkdir(parents=True, exist_ok=True)

        config_raw = str(payload.get("config") or "").strip()
        config_path: Path | None = None
        if config_raw:
            candidate = Path(config_raw).expanduser()
            config_path = (project / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
            if not config_path.is_file() or not _inside(config_path, project):
                raise ValueError("config file must exist inside the selected project")

        mode = str(payload.get("interaction_mode") or "review").strip().lower()
        if mode not in {"auto", "direction", "review", "collaborative"}:
            raise ValueError("invalid interaction_mode")
        max_cycles = payload.get("max_cycles")
        if max_cycles not in (None, ""):
            try:
                max_cycles = int(max_cycles)
            except (TypeError, ValueError) as exc:
                raise ValueError("max_cycles must be an integer") from exc
            if not 1 <= max_cycles <= 10_000:
                raise ValueError("max_cycles must be between 1 and 10000")

        port = self._port_allocator()
        token = os.urandom(24).hex()
        session_id = self.session_id(session_dir)
        log_path = session_dir / "web_console.log"

        command = [
            self.python_executable,
            "-m",
            "arbor.cli.app",
            "run",
            prompt,
            "--cwd",
            str(project),
            "--yes",
            "--yes-cwd",
            str(project),
            "--run-name",
            run_name,
            "--workspace-dir",
            str(session_dir),
            "--webui-port",
            str(port),
            "--webui-host",
            "127.0.0.1",
            "--interaction-mode",
            mode,
            "--no-followup",
        ]
        if config_path is not None:
            command.extend(["--config", str(config_path)])
        if max_cycles not in (None, ""):
            command.extend(["--max-cycles", str(max_cycles)])
        if bool(payload.get("allow_non_base_branch")):
            command.append("--allow-non-base-branch")

        env = os.environ.copy()
        env["ARBOR_WEBUI_TOKEN"] = token
        env.setdefault("ARBOR_DASHBOARD_INPUT_MODE", "line")
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(log_fd, 0o600)
        with os.fdopen(log_fd, "ab", buffering=0) as log_file:
            process = self._popen(
                command,
                cwd=str(project),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        job = ManagedRun(
            session_id=session_id,
            run_name=run_name,
            project_dir=project,
            session_dir=session_dir,
            port=port,
            token=token,
            pid=int(process.pid),
            started_at=_utc_now(),
            log_path=log_path,
            process=process,
        )
        self._write_control(job)
        with self._lock:
            self._jobs[session_id] = job
        return job

    def stop_run(self, session_id: str, *, timeout: float = 8.0) -> ManagedRun:
        job = self.get_job(session_id)
        if job is None or job.exit_code() is not None:
            raise ValueError("session is not managed or is no longer running")
        job.stopped_by_user = True
        try:
            if os.name == "posix":
                os.killpg(job.pid, signal.SIGTERM)
            elif job.process is not None:
                job.process.terminate()
        except ProcessLookupError:
            pass
        if job.process is not None:
            try:
                job.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(job.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    job.process.kill()
                job.process.wait(timeout=2)
        self._write_control(job)
        return job

    def get_job(self, session_id: str) -> ManagedRun | None:
        with self._lock:
            job = self._jobs.get(session_id)
        if job is not None:
            return job
        try:
            session = self.resolve_session(session_id)
        except ValueError:
            return None
        recovered = self._recover_control(session)
        if recovered is not None:
            with self._lock:
                self._jobs[session_id] = recovered
        return recovered

    def list_sessions(self) -> list[dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for session in self._discover_session_dirs():
            sid = self.session_id(session)
            info = self._session_record(session, sid)
            records[sid] = info
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            base = records.get(job.session_id, {})
            base.update(job.as_dict())
            base.setdefault("task", self._read_run_info(job.session_dir).get("task", ""))
            base.setdefault("model", self._read_run_info(job.session_dir).get("model", ""))
            records[job.session_id] = base
        return sorted(
            records.values(),
            key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""),
            reverse=True,
        )

    def _discover_session_dirs(self) -> list[Path]:
        found: list[Path] = []
        root_depth = len(self.workspace_root.parts)
        for current, dirs, _files in os.walk(self.workspace_root):
            current_path = Path(current)
            depth = len(current_path.parts) - root_depth
            dirs[:] = [d for d in dirs if d not in _SCAN_SKIP and not d.startswith(".worktree")]
            if depth > self.max_scan_depth:
                dirs[:] = []
                continue
            if ".arbor" in dirs:
                sessions_root = current_path / ".arbor" / "sessions"
                if sessions_root.is_dir():
                    found.extend(p.resolve() for p in sessions_root.iterdir() if p.is_dir())
                dirs.remove(".arbor")
        return found

    def _session_record(self, session: Path, session_id: str) -> dict[str, Any]:
        info = self._read_run_info(session)
        job = self.get_job(session_id)
        if job is not None:
            return {**info, **job.as_dict(), "updated_at": self._mtime(session)}
        return {
            "id": session_id,
            "run_name": str(info.get("run_name") or session.name),
            "project": str(session.parents[2]),
            "session_dir": str(session),
            "task": str(info.get("task") or ""),
            "model": str(info.get("model") or ""),
            "status": "history",
            "interactive": False,
            "updated_at": self._mtime(session),
        }

    @staticmethod
    def _read_run_info(session: Path) -> dict[str, Any]:
        try:
            value = json.loads((session / "run_info.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _mtime(path: Path) -> str:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            return ""

    def _write_control(self, job: ManagedRun) -> None:
        payload = {
            "run_name": job.run_name,
            "project": str(job.project_dir),
            "session_dir": str(job.session_dir),
            "port": job.port,
            "token": job.token,
            "pid": job.pid,
            "started_at": job.started_at,
            "log_path": str(job.log_path),
            "stopped_by_user": job.stopped_by_user,
        }
        path = job.session_dir / _CONTROL_FILE
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as control_file:
                fd = -1
                json.dump(payload, control_file, indent=2)
        finally:
            if fd >= 0:
                os.close(fd)

    def _recover_control(self, session: Path) -> ManagedRun | None:
        path = session / _CONTROL_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data["pid"])
            if not _pid_alive(pid, session):
                return None
            project = Path(str(data["project"])).resolve()
            if not _inside(project, self.workspace_root):
                return None
            return ManagedRun(
                session_id=self.session_id(session),
                run_name=str(data.get("run_name") or session.name),
                project_dir=project,
                session_dir=session,
                port=int(data["port"]),
                token=str(data["token"]),
                pid=pid,
                started_at=str(data.get("started_at") or ""),
                log_path=Path(str(data.get("log_path") or session / "web_console.log")),
                process=None,
                stopped_by_user=bool(data.get("stopped_by_user")),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
