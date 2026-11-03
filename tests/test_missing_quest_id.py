from __future__ import annotations

from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli.main import main
from imp.engine import worktree
from tests.conftest import _seed_control
from tests.test_cli_list_filters import _archive_status
from tests.test_inflight_sort import _seed_inflight

_COMMANDS = ("log", "events", "show")
_NO_QUESTS_COPY = "No quests yet — start one with imp new"


def _with_quests(cmd: str) -> str:
    return f"imp: {cmd} needs a quest id — see imp list\n"


def _no_quests(cmd: str) -> str:
    return f"imp: {cmd} needs a quest id\n  No quests yet — start one with imp new\n"


def _not_a_repo() -> Path:
    raise worktree.PreconditionError("not an imp project")


def _run(monkeypatch, capsys, control_root, argv) -> tuple[int, str, str]:
    monkeypatch.setattr(m, "_control_root", control_root)
    rc = main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("cmd", _COMMANDS)
def test_bare_command_with_live_quest_points_at_list(
    cmd, interactive, tmp_path: Path, monkeypatch, capsys,
) -> None:
    _seed_control(tmp_path)
    _seed_inflight(tmp_path, "live01", created="2026-06-01T00:00:00+00:00",
                   started="2026-06-01T00:00:00+00:00")
    monkeypatch.setattr(m, "_interactive", lambda: interactive)
    assert _run(monkeypatch, capsys, lambda: tmp_path, [cmd]) == (2, "", _with_quests(cmd))


@pytest.mark.parametrize("cmd", _COMMANDS)
def test_bare_command_in_empty_project_says_start_one(
    cmd, tmp_path: Path, monkeypatch, capsys,
) -> None:
    _seed_control(tmp_path)
    assert _run(monkeypatch, capsys, lambda: tmp_path, [cmd]) == (2, "", _no_quests(cmd))


@pytest.mark.parametrize("status", ["abandoned", "archived"])
@pytest.mark.parametrize("cmd", _COMMANDS)
def test_bare_command_with_only_history_quest_points_at_list(
    cmd, status, tmp_path: Path, monkeypatch, capsys,
) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "gone01", status)
    assert _run(monkeypatch, capsys, lambda: tmp_path, [cmd]) == (2, "", _with_quests(cmd))


@pytest.mark.parametrize("cmd", _COMMANDS)
def test_bare_command_outside_repo_says_start_one(cmd, monkeypatch, capsys) -> None:
    assert _run(monkeypatch, capsys, _not_a_repo, [cmd]) == (2, "", _no_quests(cmd))


def test_no_quests_copy_has_one_source() -> None:
    main_py = Path(m.__file__).resolve()
    hits = [p for p in main_py.parents[1].rglob("*.py")
            if _NO_QUESTS_COPY in p.read_text(encoding="utf-8")]
    assert hits == [main_py]
    assert main_py.read_text(encoding="utf-8").count(_NO_QUESTS_COPY) == 1


@pytest.mark.parametrize(("argv", "error"), [
    (["log", "--show", "bogus"],
     "imp: unknown category 'bogus' (choose from: thinking, tools, subagents, prompts, tiers)\n"),
    (["log", "--summary", "--show", "tools"],
     "imp: --show/--hide not allowed with -s/--summary\n"),
    (["log", "-f", "--summary"],
     "imp: -f/--follow not allowed with --summary\n"),
])
def test_bare_log_flag_errors_win_over_missing_id(argv, error, monkeypatch, capsys) -> None:
    # Pure flag validation runs before any repo lookup, so it wins even outside a repo.
    assert _run(monkeypatch, capsys, _not_a_repo, argv) == (2, "", error)
