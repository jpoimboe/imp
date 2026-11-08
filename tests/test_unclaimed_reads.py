"""A read-only lookup sees an unclaimed project as empty and claims no state dir."""

from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

import pytest

from imp.engine.archive import check_data_floor, check_index_floor, ensure_current
from imp.engine.archive.read import (
    archived_ids,
    completed_rows,
    is_archived,
    search_archive_content,
    search_archive_events_content,
)
from imp.engine.quest import live_quest_ids
from imp.paths import imp_home, project_state_dir
from imp.providers.effective import FLOOR_SRC, load_effective_config
from imp.providers.paths import global_config_path
from imp.remote.daemon.runner import read_pid, write_pid
from tests.conftest import _init_repo


def _no_projects() -> bool:
    return not (imp_home() / "projects").exists()


def test_live_quest_ids_is_empty_and_claims_nothing(tmp_path: Path) -> None:
    assert live_quest_ids(tmp_path) == []
    assert _no_projects()


def test_archive_readers_are_empty_and_claim_nothing(tmp_path: Path) -> None:
    assert completed_rows(tmp_path) == []
    assert search_archive_content(tmp_path, "foo") == []
    assert search_archive_events_content(tmp_path, "foo") == []
    assert archived_ids(tmp_path) == []
    assert is_archived(tmp_path, "abcd") is False
    check_data_floor(tmp_path)
    check_index_floor(tmp_path)
    ensure_current(tmp_path)
    assert _no_projects()


def test_read_pid_is_none_and_claims_nothing(tmp_path: Path) -> None:
    assert read_pid(tmp_path) is None
    assert _no_projects()


def test_effective_config_skips_the_local_layer_and_claims_nothing(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    project = tmp_path / "imp" / "config.yaml"
    project.write_text("theme: dark\n")
    global_path = global_config_path()
    global_path.parent.mkdir(parents=True)
    global_path.write_text("providers: [openai]\n")

    eff = load_effective_config(control_root=tmp_path)

    assert eff.config.theme == "dark"
    assert eff.config.providers == ("openai",)
    assert {src.file for src in eff.provenance.values()} == {
        FLOOR_SRC.file, project, global_path}
    assert _no_projects()


def test_claimed_live_quest_ids_lists_a_seeded_home(tmp_path: Path) -> None:
    home = project_state_dir(tmp_path) / "quests" / "abcd12"
    home.mkdir(parents=True)
    (home / "quest.json").write_text("{}")
    assert live_quest_ids(tmp_path) == ["abcd12"]


def test_claimed_local_config_still_applies(tmp_path: Path) -> None:
    local = project_state_dir(tmp_path) / "config.yaml"
    local.write_text("theme: light\n")
    eff = load_effective_config(control_root=tmp_path)
    assert eff.config.theme == "light"
    assert eff.provenance[("theme",)].file == local


def test_claimed_read_pid_returns_the_written_pid(tmp_path: Path) -> None:
    project_state_dir(tmp_path)
    write_pid(tmp_path)
    assert read_pid(tmp_path) == os.getpid()


@pytest.mark.parametrize("argv", [
    ["list"], ["list", "--json"], ["show", "abcd"], ["log", "abcd"],
    ["search", "foo"], ["events", "search", "foo"], ["abandon", "abcd"],
    ["resume", "abcd"], ["edit", "abcd"], ["remote-control", "stop"],
    ["remote-control", "start"], ["--help"], [],
], ids=lambda argv: " ".join(argv) or "bare")
def test_read_commands_outside_a_project_claim_no_state_dir(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.main import main

    for name in [k for k in os.environ if k.startswith("IMP_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with contextlib.suppress(SystemExit):
        main(argv)

    assert not (tmp_path / "imphome" / "projects").exists()
