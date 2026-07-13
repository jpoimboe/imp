from __future__ import annotations

import types
from pathlib import Path

import pytest

from imp.remote.session import daemon


def test_main_installs_stack_dumper_at_quest_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []
    quest_dir = tmp_path / ".imp" / "quests" / "q"
    monkeypatch.setattr(daemon, "warm_module_graph", lambda: None)
    monkeypatch.setattr(
        daemon.Quest, "load",
        classmethod(lambda cls, cr, qid: types.SimpleNamespace(quest_dir=quest_dir)))
    monkeypatch.setattr(daemon, "install_stack_dumper", lambda p: calls.append(p))
    monkeypatch.setattr(daemon.anyio, "run", lambda *a, **k: object())

    rc = daemon.main(["--control-root", str(tmp_path), "--quest-id", "q"])

    assert rc == 0
    assert calls == [quest_dir / "stack-dump.txt"]
