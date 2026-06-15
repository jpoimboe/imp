from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from imp.engine.change import Change
from imp.remote.session.attach import spawn_session_daemon


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_change(tmp_path: Path, change_id: str) -> Change:
    return Change(
        id=change_id,
        title="T",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        change_dir=tmp_path / ".imp" / "change",
    )


def test_spawn_session_daemon_launches_detached_module_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = _make_change(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> object:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setenv("IMP_VERIFY_COMMAND", "leaked-gate")
    monkeypatch.setenv("IMP_REMOTE_CONTROL_SOCK", "/tmp/stale.sock")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_session_daemon(change)

    assert captured["argv"] == [
        sys.executable, "-m", "imp.remote.session.daemon",
        "--control-root", str(tmp_path),
        "--change-id", "2026-06-15-abc",
    ]
    kw = captured["kwargs"]
    assert kw["cwd"] == str(tmp_path)
    assert kw["start_new_session"] is True
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"] == subprocess.DEVNULL
    assert kw["stderr"] == subprocess.DEVNULL
    assert "IMP_VERIFY_COMMAND" not in kw["env"]
    assert "IMP_REMOTE_CONTROL_SOCK" not in kw["env"]
