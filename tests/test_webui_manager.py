from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from arbor.webui.manager import RunManager


class _FakeProcess:
    pid = 43210

    def poll(self) -> None:
        return None


def test_manager_bounds_paths_and_discovers_history(tmp_path: Path) -> None:
    project = tmp_path / "project"
    session = project / ".arbor" / "sessions" / "old_run"
    session.mkdir(parents=True)
    (session / "run_info.json").write_text(
        json.dumps({"run_name": "old_run", "task": "test task", "model": "qwen"}),
        encoding="utf-8",
    )
    manager = RunManager(tmp_path)

    session_id = manager.session_id(session)
    assert manager.resolve_session(session_id) == session.resolve()
    assert manager.list_sessions()[0]["task"] == "test task"
    with pytest.raises(ValueError, match="inside"):
        manager.validate_project(str(tmp_path.parent))
    with pytest.raises(ValueError, match="invalid session id"):
        manager.resolve_session("not-base64!")


def test_start_run_uses_loopback_webui_and_does_not_persist_cloud_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    captured: dict = {}

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-written")
    manager = RunManager(tmp_path, popen_factory=fake_popen, port_allocator=lambda: 45678)
    job = manager.start_run({
        "project": "project",
        "run_name": "from_web",
        "prompt": "Improve the benchmark without changing its evaluator.",
        "interaction_mode": "collaborative",
        "max_cycles": 8,
    })

    command = captured["command"]
    assert command[command.index("--webui-host") + 1] == "127.0.0.1"
    assert command[command.index("--interaction-mode") + 1] == "collaborative"
    assert command[command.index("--max-cycles") + 1] == "8"
    control = job.session_dir / ".webui-control.json"
    body = control.read_text(encoding="utf-8")
    assert "must-not-be-written" not in body
    assert json.loads(body)["port"] == 45678
    if os.name == "posix":
        assert stat.S_IMODE(control.stat().st_mode) == 0o600
