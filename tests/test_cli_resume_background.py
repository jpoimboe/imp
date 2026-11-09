"""imp resume --background: validate in the parent, then spawn one detached resume."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

import imp.cli.main as m
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.remote.daemon import spawn
from tests.conftest import _seed_control
from tests.test_archive_floor_fallbacks import _archive_completed

_REFUSAL = ("imp: quest {id} is already running in a live session; attach with "
            "`imp resume {id}`, or pause it first.\n")


def _launched(quest_id: str, *, autonomous: bool) -> str:
    mode = "autonomously " if autonomous else ""
    return (f"Resumed quest {quest_id} (running {mode}in the background; appears "
            f"in the active list).\nIf it needs your answer it waits: imp list "
            f"shows it as waiting, and imp resume {quest_id} answers it.\n")


@pytest.fixture(autouse=True)
def _in_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)

    async def _boom(*a: Any, **k: Any) -> int:
        raise AssertionError("a background resume must not run in the parent")

    monkeypatch.setattr(m, "_run_fresh_session", _boom)
    monkeypatch.setattr(m, "_resume_inert", _boom)
    monkeypatch.setattr(m, "_clear_loop", _boom)
    monkeypatch.setattr("imp.cli.session_run.run_session_client", _boom)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    argvs: list[list[str]] = []
    monkeypatch.setattr(
        spawn, "_spawn_detached", lambda argv, env, cwd: argvs.append(argv))
    return argvs


def _paused(tmp_path: Path, *, live: bool = False, status: str = "paused") -> Quest:
    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="demo", quest_id="feat", base_branch="main")
    state = State.load(quest.state_dir)
    state.status = status
    state.has_started = True
    state.save(quest.state_dir)
    if live:
        (quest.quest_dir / "lock").write_text(str(os.getpid()))
    return quest


def _fresh(tmp_path: Path, *, live: bool = False,
           depends_on: list[str] | None = None) -> Quest:
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="demo", quest_id="seed1", base_branch="main",
        seed_opening="do the thing", depends_on=depends_on)
    if live:
        (quest.quest_dir / "lock").write_text(str(os.getpid()))
    return quest


def _pin_of(quest: Quest) -> object:
    return json.loads((quest.quest_dir / "quest.json").read_text())["provider"]


def _resume(*argv: str) -> int:
    return anyio.run(m._async_cmd_resume, m._build_parser().parse_args(["resume", *argv]))


def _resume_action(dest: str) -> argparse.Action:
    parser = m._build_parser()
    sub = next(a for a in parser._actions
               if getattr(a, "choices", None) and "resume" in a.choices)
    return next(a for a in sub.choices["resume"]._actions if a.dest == dest)


def test_resume_help_lists_background_and_the_approved_wording(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        m._build_parser().parse_args(["resume", "-h"])
    assert "--background" in capsys.readouterr().out
    assert _resume_action("background").help == (
        "Continue the quest detached in the background with no live prompt. "
        "Combine with --message to answer the pending question first.")
    assert _resume_action("autonomous").help == (
        "Take imp's recommendation at routine decision points instead of asking "
        "you; questions only you can answer still pause for you. Combine with "
        "--background for a hands-off run.")


@pytest.mark.parametrize(("argv", "flags"), [
    ([], []),
    (["--autonomous"], ["--autonomous"]),
    (["--message", "yes, ship it"], ["--message=yes, ship it"]),
    (["--provider", "codex"], ["--provider=codex"]),
    (["--autonomous", "--message", "go", "--provider", "codex"],
     ["--autonomous", "--message=go", "--provider=codex"]),
])
def test_background_spawns_one_resume_carrying_exactly_the_given_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], floor_availability: Any,
    spawned: list[list[str]], argv: list[str], flags: list[str],
) -> None:
    quest = _paused(tmp_path)
    floor_availability({"claude", "codex"})

    assert _resume("feat", "--background", *argv) == 0

    assert spawned == [[sys.executable, "-m", "imp", "resume", "feat", *flags]]
    out = capsys.readouterr()
    assert out.out == _launched("feat", autonomous="--autonomous" in argv)
    assert out.err == ""
    assert _pin_of(quest) is None


def test_a_message_that_starts_with_a_dash_reaches_the_child_intact(
    tmp_path: Path, spawned: list[list[str]],
) -> None:
    _paused(tmp_path)

    assert _resume("feat", "--background", "--message=-y") == 0

    (argv,) = spawned
    child = m._build_parser().parse_args(argv[3:])
    assert (child.token, child.message) == ("feat", "-y")


def test_background_on_a_live_quest_refuses_and_spawns_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], spawned: list[list[str]],
) -> None:
    _paused(tmp_path, live=True)

    assert _resume("feat", "--background") == 2

    out = capsys.readouterr()
    assert (out.out, out.err) == ("", _REFUSAL.format(id="feat"))
    assert spawned == []


def test_with_no_id_on_a_tty_the_picked_quest_is_backgrounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]],
) -> None:
    _paused(tmp_path)
    Quest.create_draft(tmp_path, title="Other work", quest_id="oth001",
                       base_branch="main", origin="idea")
    monkeypatch.setattr(m, "_interactive", lambda: True)
    offered: list[str] = []

    async def _pick(candidates: list[Any], **kw: Any) -> str:
        offered.extend(c.quest_id for c in candidates)
        return "feat"

    monkeypatch.setattr("imp.cli.resume_select.run_picker", _pick)

    assert _resume("--background") == 0

    assert sorted(offered) == ["feat", "oth001"]
    assert spawned == [[sys.executable, "-m", "imp", "resume", "feat"]]


def test_a_merged_archived_id_keeps_its_message_and_spawns_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], spawned: list[list[str]],
) -> None:
    _seed_control(tmp_path)
    _archive_completed(tmp_path, "arqu99")

    assert _resume("arqu99", "--background") == 0

    out = capsys.readouterr()
    assert (out.out, out.err) == ("Archived Dep is already merged; nothing to resume.\n", "")
    assert spawned == []


def test_an_invalid_provider_keeps_its_error_and_spawns_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], floor_availability: Any,
    spawned: list[list[str]],
) -> None:
    _paused(tmp_path)
    floor_availability({"claude", "codex"})

    assert _resume("feat", "--background", "--provider", "bogus") == 2

    assert capsys.readouterr().err == (
        "imp: --provider: unknown provider 'bogus' "
        "(also installed: claude, codex; needs setup: anthropic, openai)\n")
    assert spawned == []


def test_a_non_resumable_status_keeps_its_error_and_spawns_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], spawned: list[list[str]],
) -> None:
    _paused(tmp_path, status="completed")

    assert _resume("feat", "--background") == 2

    assert capsys.readouterr().err == (
        "imp: quest feat status is 'completed'; nothing to resume\n")
    assert spawned == []


def test_a_fresh_draft_with_a_live_lock_refuses_and_writes_no_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], floor_availability: Any,
    spawned: list[list[str]],
) -> None:
    quest = _fresh(tmp_path, live=True)
    floor_availability({"claude", "codex"})

    assert _resume("seed1", "--background", "--provider", "codex") == 2

    out = capsys.readouterr()
    assert (out.out, out.err) == ("", _REFUSAL.format(id="seed1"))
    assert spawned == []
    assert _pin_of(quest) is None


def test_a_blocked_fresh_draft_warns_in_the_parent_then_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], floor_availability: Any,
) -> None:
    quest = _fresh(tmp_path, depends_on=["2026-06-16-ghost1"])
    floor_availability({"claude", "codex"})
    at_spawn: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        spawn, "_spawn_detached",
        lambda argv, env, cwd: at_spawn.append((argv, capsys.readouterr().err)))

    assert _resume("seed1", "--background", "--provider", "codex") == 0

    assert at_spawn == [(
        [sys.executable, "-m", "imp", "resume", "seed1", "--provider=codex"],
        "imp: backlog quest seed1 is blocked on unmerged dep(s): 2026-06-16-ghost1; "
        "starting anyway.\n")]
    assert capsys.readouterr().out == _launched("seed1", autonomous=False)
    assert _pin_of(quest) is None
