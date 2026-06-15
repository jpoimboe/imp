from __future__ import annotations

from typing import Any

import imp.cli.main as m


def test_finalize_paused_exit_finishes_driver_before_journaling_pause(monkeypatch) -> None:
    calls: list[str] = []

    class _Driver:
        def finish(self) -> None:
            calls.append("finish")

    def _fake_pause_and_exit(change: Any, *, reason: str) -> None:
        calls.append("pause_and_exit")

    def _fake_emit_paused(change: Any, reason: str | None = None) -> None:
        calls.append("emit_paused")

    monkeypatch.setattr(m, "pause_and_exit", _fake_pause_and_exit)
    monkeypatch.setattr(m, "_emit_paused", _fake_emit_paused)

    rc = m._finalize_paused_exit(object(), _Driver(), object(), "ctrl-c")

    assert rc == 130
    # finish() before pause_and_exit — live driver stops before workflow_paused is written.
    assert calls == ["finish", "pause_and_exit", "emit_paused"]
