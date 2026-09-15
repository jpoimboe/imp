from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.main import main


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A non-git directory, isolated from the developer's real machine config.
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
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
    assert "No quests yet — start one with imp new" in out


def test_config_outside_project_succeeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["config"])
    assert rc == 0
    assert "Traceback" not in capsys.readouterr().err


def test_empty_providers_prints_a_clean_error_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "imphome"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("providers: []\n")

    rc = main(["config"])

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == (
        f"imp: {home / 'config.yaml'}:1\n"
        "  config.yaml 'providers:' must be a non-empty list of provider names\n"
        "  hint: run 'imp config set providers claude' to fix\n"
    )
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
