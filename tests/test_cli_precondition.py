from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.main import main


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A non-git directory, isolated from the developer's real machine config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)


def test_log_outside_project_prints_clean_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["log", "abc123"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "imp: not an imp project — run `imp init` to set up imp here" in err
    assert "Traceback" not in err


def test_new_outside_project_prints_clean_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["new", "do a thing"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not an imp project" in err
    assert "Traceback" not in err


def test_status_outside_project_keeps_empty_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No changes." in out


def test_config_outside_project_succeeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["config"])
    assert rc == 0
    assert "Traceback" not in capsys.readouterr().err
