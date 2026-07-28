from __future__ import annotations

import os
from pathlib import Path

import pytest

from imp.cli import main as cli_main


def test_main_installs_pid_keyed_stack_dumper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(cli_main, "_control_root", lambda: tmp_path)
    monkeypatch.setattr("imp.diagnostics.install_stack_dumper", lambda p: calls.append(p))
    monkeypatch.setattr(cli_main, "_dispatch_meta", lambda args, parser: 0)

    rc = cli_main.main([])

    assert rc == 0
    expected = tmp_path / ".imp" / f"cli.{os.getpid()}.stack-dump.txt"
    assert calls == [expected]


def test_main_survives_control_root_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> Path:
        raise RuntimeError("not inside an imp project")

    monkeypatch.setattr(cli_main, "_control_root", _boom)
    monkeypatch.setattr(cli_main, "_dispatch_meta", lambda args, parser: 0)

    rc = cli_main.main([])

    assert rc == 0
