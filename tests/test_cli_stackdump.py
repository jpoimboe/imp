from __future__ import annotations

import os
from pathlib import Path

import pytest

from imp.cli import main as cli_main
from imp.paths import imp_home, project_state_dir


def test_main_installs_pid_keyed_stack_dumper_in_a_claimed_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("")
    state = project_state_dir(tmp_path)
    calls: list[Path] = []
    monkeypatch.setattr(cli_main, "_control_root", lambda: tmp_path)
    monkeypatch.setattr("imp.diagnostics.install_stack_dumper", lambda p: calls.append(p))
    monkeypatch.setattr(cli_main, "_dispatch_meta", lambda args, parser: 0)

    rc = cli_main.main([])

    assert rc == 0
    assert calls == [state / f"cli.{os.getpid()}.stack-dump.txt"]


def test_main_in_an_unclaimed_project_installs_no_dumper_and_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("")
    calls: list[Path] = []
    monkeypatch.setattr(cli_main, "_control_root", lambda: tmp_path)
    monkeypatch.setattr("imp.diagnostics.install_stack_dumper", lambda p: calls.append(p))
    monkeypatch.setattr(cli_main, "_dispatch_meta", lambda args, parser: 0)

    rc = cli_main.main([])

    assert rc == 0
    assert calls == []
    assert not (imp_home() / "projects").exists()


def test_main_with_a_missing_state_dir_override_installs_no_dumper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("")
    override = tmp_path / "elsewhere" / "state"
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    calls: list[Path] = []
    monkeypatch.setattr(cli_main, "_control_root", lambda: tmp_path)
    monkeypatch.setattr("imp.diagnostics.install_stack_dumper", lambda p: calls.append(p))
    monkeypatch.setattr(cli_main, "_dispatch_meta", lambda args, parser: 0)

    rc = cli_main.main([])

    assert rc == 0
    assert calls == []
    assert not override.exists()


def test_main_outside_an_imp_project_installs_no_dumper_and_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(cli_main, "_control_root", lambda: tmp_path)
    monkeypatch.setattr("imp.diagnostics.install_stack_dumper", lambda p: calls.append(p))
    monkeypatch.setattr(cli_main, "_dispatch_meta", lambda args, parser: 0)

    rc = cli_main.main([])

    assert rc == 0
    assert calls == []
    assert not (imp_home() / "projects").exists()


def test_main_survives_control_root_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> Path:
        raise RuntimeError("not inside an imp project")

    monkeypatch.setattr(cli_main, "_control_root", _boom)
    # The fallback root may be this checkout (it has imp/config.yaml): no real handler.
    monkeypatch.setattr("imp.diagnostics.install_stack_dumper", lambda _p: None)
    monkeypatch.setattr(cli_main, "_dispatch_meta", lambda args, parser: 0)

    rc = cli_main.main([])

    assert rc == 0
