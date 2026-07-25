"""One-shot startup migration of the global config file (spec scope 5, step 1).

The steps are plain functions over explicit paths, so the criteria are exercised
directly rather than by hoping a main([...]) call fires them."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.config_migrate import (
    display_path,
    migrate_global_config,
    run_startup_migrations,
)


def _legacy(tmp_path: Path, body: str = "providers: [openai]\n") -> Path:
    p = tmp_path / "legacy" / "imp" / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(body)
    return p


def test_migrates_once_and_reports_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = _legacy(tmp_path)
    new = tmp_path / "home" / "config.yaml"
    migrate_global_config(legacy, new)
    assert new.read_text() == "providers: [openai]\n"
    assert not legacy.exists()
    assert not legacy.parent.exists()      # the emptied legacy dir is removed
    # Spec scope 9 pins this line verbatim, prefix and connector included. The
    # paths go through display_path because a tmp dir may itself live under $HOME.
    err = capsys.readouterr().err
    assert err == f"imp: moved {display_path(legacy)} to {display_path(new)}\n"


def test_second_call_is_a_silent_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = _legacy(tmp_path)
    new = tmp_path / "home" / "config.yaml"
    migrate_global_config(legacy, new)
    capsys.readouterr()
    migrate_global_config(legacy, new)
    assert capsys.readouterr().err == ""
    assert new.read_text() == "providers: [openai]\n"


def test_existing_new_file_wins_and_legacy_is_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = _legacy(tmp_path)
    new = tmp_path / "home" / "config.yaml"
    new.parent.mkdir(parents=True)
    new.write_text("providers: [anthropic]\n")
    migrate_global_config(legacy, new)
    assert new.read_text() == "providers: [anthropic]\n"
    assert legacy.read_text() == "providers: [openai]\n"
    assert capsys.readouterr().err == ""


def test_absent_legacy_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    new = tmp_path / "home" / "config.yaml"
    migrate_global_config(tmp_path / "legacy" / "imp" / "config.yaml", new)
    assert not new.parent.exists()
    assert capsys.readouterr().err == ""


def test_non_empty_legacy_dir_survives_the_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = _legacy(tmp_path)
    (legacy.parent / "other.yaml").write_text("x: 1\n")
    new = tmp_path / "home" / "config.yaml"
    migrate_global_config(legacy, new)
    assert new.is_file()
    assert (legacy.parent / "other.yaml").is_file()
    assert capsys.readouterr().err.startswith("imp: moved ")


def test_run_startup_migrations_resolves_the_defaults_from_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "home"))
    legacy = _legacy(tmp_path, "theme: dark\n")
    run_startup_migrations()
    assert (tmp_path / "home" / "config.yaml").read_text() == "theme: dark\n"
    assert not legacy.exists()


def test_run_startup_migrations_swallows_a_failing_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A migration that cannot run is retried next invocation; it must never be
    # the reason a command dies. Here IMP_HOME's parent is a regular file, so
    # creating the new home raises NotADirectoryError.
    (tmp_path / "blocked").write_text("not a directory")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "blocked" / "home"))
    legacy = _legacy(tmp_path)
    run_startup_migrations()   # must not raise
    assert legacy.is_file()


def test_legacy_global_config_path_still_honors_xdg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The migration's source, and the only remaining XDG_CONFIG_HOME reader in src/.
    from imp.providers.effective import legacy_global_config_path

    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/x")
    assert legacy_global_config_path() == Path("/tmp/x/imp/config.yaml")


def test_display_path_is_home_relative_under_home_and_absolute_outside() -> None:
    # Together with the notice assertion above, this is what pins the mandated
    # copy `imp: moved ~/.config/imp/config.yaml to ~/.imp/config.yaml`.
    assert display_path(Path.home() / ".imp" / "config.yaml") == "~/.imp/config.yaml"
    assert display_path(Path.home() / ".config" / "imp" / "config.yaml") == (
        "~/.config/imp/config.yaml")
    assert display_path(Path("/etc/imp/config.yaml")) == "/etc/imp/config.yaml"


def test_entrypoint_migrates_before_delegating_to_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal

    calls: list[str] = []
    monkeypatch.setattr("imp.cli.config_migrate.run_startup_migrations",
                        lambda: calls.append("migrate"))
    monkeypatch.setattr("imp.cli.main.main", lambda: calls.append("cli") or 0)
    # Importing the entrypoint installs its module-level SIGINT handler; restore
    # the prior one so Ctrl-C behavior doesn't leak into the rest of the session.
    prior = signal.getsignal(signal.SIGINT)
    try:
        import imp.__main__ as entrypoint
        assert entrypoint.main() == 0
    finally:
        signal.signal(signal.SIGINT, prior)
    assert calls == ["migrate", "cli"]


def test_cli_main_does_not_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The suite drives imp.cli.main.main in-process at 168 sites; a migration
    # there would rearrange the developer's own files mid-run.
    from imp.cli.main import main as cli_main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "legacy"))
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "home"))
    legacy = _legacy(tmp_path)
    assert cli_main([]) == 0
    capsys.readouterr()
    assert legacy.is_file()
    assert not (tmp_path / "home").exists()
