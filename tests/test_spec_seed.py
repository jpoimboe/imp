from __future__ import annotations

import argparse
import sys
from pathlib import Path

import anyio
import pytest

import imp.cli.prompt as prompt_mod
from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.cli import main as cli_main
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog
from tests.conftest import _git, _seed_control


def test_build_parser_new_positional_is_opening() -> None:
    ns = cli_main._build_parser().parse_args(["new", "add a dark-mode toggle"])
    assert ns.opening == "add a dark-mode toggle"
    assert ns.title is None


def test_build_parser_new_title_flag() -> None:
    ns = cli_main._build_parser().parse_args(["new", "--title", "dark mode"])
    assert ns.title == "dark mode"
    assert ns.opening is None


def test_build_parser_new_positional_and_title_flag() -> None:
    ns = cli_main._build_parser().parse_args(
        ["new", "add dark mode", "--title", "dark mode"])
    assert ns.opening == "add dark mode"
    assert ns.title == "dark mode"


def test_build_parser_new_accepts_spec_file() -> None:
    ns = cli_main._build_parser().parse_args(["new", "--spec-file", "draft.md"])
    assert ns.spec_file == "draft.md"


def test_build_parser_new_spec_file_defaults_none() -> None:
    ns = cli_main._build_parser().parse_args(["new"])
    assert ns.spec_file is None


def _new_args(**kw: object) -> argparse.Namespace:
    base = {"opening": None, "title": None, "non_interactive": False,
            "message": None, "tui": False, "spec_file": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_resolve_positional_is_verbatim_seed() -> None:
    res = cli_main.resolve_spec_seed(_new_args(opening="add a dark-mode toggle"))
    assert res == cli_main.SeedResolution(
        title=None, seed_text="add a dark-mode toggle",
        opening_intent="add a dark-mode toggle")
    assert cli_main.SPEC_SEED_MARKER not in res.seed_text


def test_resolve_positional_file_path_is_literal_text(tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("# Draft\nGoal: do the thing\n")
    res = cli_main.resolve_spec_seed(_new_args(opening=str(draft)))
    assert res.title is None
    assert res.seed_text == str(draft)
    assert cli_main.SPEC_SEED_MARKER not in res.seed_text


def test_resolve_title_flag_sets_title_no_seed() -> None:
    res = cli_main.resolve_spec_seed(_new_args(title="dark mode"))
    assert res == cli_main.SeedResolution(title="dark mode", seed_text=None)


def test_resolve_no_args_is_empty() -> None:
    res = cli_main.resolve_spec_seed(_new_args())
    assert res == cli_main.SeedResolution(title=None, seed_text=None)


def test_resolve_whitespace_only_positional_is_empty() -> None:
    res = cli_main.resolve_spec_seed(_new_args(opening="   "))
    assert res == cli_main.SeedResolution(title=None, seed_text=None)


def test_resolve_positional_and_spec_file_mutually_exclusive(
        tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: x\n")
    with pytest.raises(cli_main.SeedError):
        cli_main.resolve_spec_seed(
            _new_args(opening="add dark mode", spec_file=str(draft)))


def test_resolve_spec_file_wraps_seed(tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: ship it\n")
    res = cli_main.resolve_spec_seed(_new_args(spec_file=str(draft)))
    assert res.title is None
    assert res.seed_text is not None
    assert res.seed_text.startswith(cli_main.SPEC_SEED_MARKER)
    assert str(draft) in res.seed_text
    assert "Goal: ship it" in res.seed_text


def test_resolve_title_flag_with_spec_file(tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: ship it\n")
    res = cli_main.resolve_spec_seed(
        _new_args(title="my-name", spec_file=str(draft)))
    assert res.title == "my-name"
    assert res.seed_text is not None
    assert str(draft) in res.seed_text
    assert "Goal: ship it" in res.seed_text


def test_resolve_missing_spec_file_errors(tmp_path: Path) -> None:
    with pytest.raises(cli_main.SeedError):
        cli_main.resolve_spec_seed(
            _new_args(spec_file=str(tmp_path / "nope.md")))


def test_resolve_spec_file_and_message_mutually_exclusive(tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: x\n")
    with pytest.raises(cli_main.SeedError):
        cli_main.resolve_spec_seed(
            _new_args(spec_file=str(draft), message="hi"))


def test_new_positional_delivers_verbatim_seed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object, seed_opening: object,
                           status: object = None,
                           opening_intent: object = None) -> int:
        captured["change"] = change
        captured["seed_opening"] = seed_opening
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    args = _new_args(opening="add a dark-mode toggle", non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    assert captured["seed_opening"] == "add a dark-mode toggle"
    assert cli_main.SPEC_SEED_MARKER not in captured["seed_opening"]  # type: ignore[operator]
    assert captured["change"].title == ""  # type: ignore[union-attr]


def test_new_title_flag_sets_change_title(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object, seed_opening: object,
                           status: object = None,
                           opening_intent: object = None) -> int:
        captured["change"] = change
        captured["seed_opening"] = seed_opening
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    args = _new_args(title="dark mode", non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    assert captured["seed_opening"] is None
    assert captured["change"].title == "dark mode"  # type: ignore[union-attr]


def test_new_spec_file_delivers_wrapped_seed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text("# My draft\nGoal: do X\n")

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object, seed_opening: object,
                           status: object = None,
                           opening_intent: object = None) -> int:
        captured["change"] = change
        captured["seed_opening"] = seed_opening
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    args = _new_args(spec_file=str(draft), non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    seed = captured["seed_opening"]
    assert isinstance(seed, str)
    assert seed.startswith(cli_main.SPEC_SEED_MARKER)
    assert str(draft) in seed
    assert "# My draft" in seed
    assert captured["change"].title == ""  # type: ignore[union-attr]


def test_new_positional_and_spec_file_rc2(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: x\n")

    args = _new_args(opening="add dark mode", spec_file=str(draft),
                     non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_new_missing_spec_file_rc2_no_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    args = _new_args(spec_file=str(tmp_path / "nope.md"), non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 2
    assert "nope.md" in capsys.readouterr().err
    worktrees = tmp_path / ".imp" / "worktrees"
    assert not worktrees.exists() or not any(worktrees.iterdir())


def test_new_spec_file_and_message_rc2(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: x\n")

    args = _new_args(spec_file=str(draft), message="hi", non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_new_positional_with_message_legal(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object, seed_opening: object,
                           status: object = None,
                           opening_intent: object = None) -> int:
        captured["seed_opening"] = seed_opening
        captured["reply"] = await solicit("")  # type: ignore[operator]
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    args = _new_args(opening="add dark mode", non_interactive=True, message="reply")
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    assert captured["seed_opening"] == "add dark mode"
    assert captured["reply"] == "reply"


def test_new_tui_delivers_seed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text("Goal: x\n")

    captured: dict[str, object] = {}

    async def _fake_tui(change: object, *, seed_opening: str | None = None,
                        resume: bool = False,
                        opening_intent: str | None = None,
                        leave_mode: str | None = None) -> int:
        captured["seed_opening"] = seed_opening
        return 0

    monkeypatch.setattr(cli_main, "_run_tui_session", _fake_tui)
    monkeypatch.setattr(cli_main, "_tui_requested", lambda _a: True)

    args = _new_args(spec_file=str(draft), tui=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    seed = captured["seed_opening"]
    assert isinstance(seed, str)
    assert seed.startswith(cli_main.SPEC_SEED_MARKER)
    assert "Goal: x" in seed
    assert "--tui does not deliver" not in capsys.readouterr().err


def test_new_non_interactive_spec_seed_via_session_client(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    draft = tmp_path / "draft.md"
    draft.write_text("# My draft\nGoal: do X\n")

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object, seed_opening: object,
                           status: object = None,
                           opening_intent: object = None) -> int:
        captured["seed_opening"] = seed_opening
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    args = _new_args(spec_file=str(draft), non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    seed = captured["seed_opening"]
    assert isinstance(seed, str)
    assert seed.startswith(cli_main.SPEC_SEED_MARKER)
    assert str(draft) in seed


def test_front_door_opening_conditioned_on_message(tmp_path) -> None:
    body = load_workflow(bundled_workflow_dir("imp")).agents[
        "front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))
    # Opening behavior must branch on the opening message: the unseeded branch
    # keys on the begin token, the seeded branch on the seed marker.
    assert "<imp:begin/>" in out


def test_front_door_prompt_has_verbatim_opening_branch(tmp_path) -> None:
    body = load_workflow(bundled_workflow_dir("imp")).agents[
        "front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "verbatim opening intent" in out
    # The bare positional no longer delivers a spec-seed FILE; only --spec-file does.
    assert "imp new <draft>" not in out
    assert "imp new <file>" not in out


def test_front_door_prompt_has_seed_branch(tmp_path) -> None:
    body = load_workflow(bundled_workflow_dir("imp")).agents[
        "front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))
    # Seed-marker invariant: the adopt branch must key on the SAME phrase the CLI
    # prefixes (_build_seed).
    assert cli_main.SPEC_SEED_MARKER in out


def test_opening_prompt_reply_wires_to_seed_opening(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object, seed_opening: object = None,
                           status: object = None, opening_intent: object = None,
                           leave_mode: object = None) -> int:
        captured["seed_opening"] = seed_opening
        captured["opening_intent"] = opening_intent
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    async def _fake_ask(self: object, *, question: object = None,
                        width: object = None, prompt_text: object = None,
                        hint: object = None) -> str:
        return "add a feature"

    monkeypatch.setattr(prompt_mod.Prompt, "ask_async", _fake_ask)

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        args = _new_args(opening=None, non_interactive=False)
        rc = anyio.run(cli_main._async_cmd_new, args)
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 0
    assert captured["seed_opening"] == "add a feature"
    assert captured["opening_intent"] == "add a feature"


def test_opening_prompt_empty_reply_gives_none_seed_opening(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object, seed_opening: object = None,
                           status: object = None, opening_intent: object = None,
                           leave_mode: object = None) -> int:
        captured["seed_opening"] = seed_opening
        captured["opening_intent"] = opening_intent
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    async def _fake_ask(self: object, *, question: object = None,
                        width: object = None, prompt_text: object = None,
                        hint: object = None) -> str:
        return "   "

    monkeypatch.setattr(prompt_mod.Prompt, "ask_async", _fake_ask)

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        args = _new_args(opening=None, non_interactive=False)
        rc = anyio.run(cli_main._async_cmd_new, args)
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 0
    assert captured["seed_opening"] is None
    assert captured["opening_intent"] is None


def test_new_creates_unmaterialized_draft(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object, seed_opening: object,
                           status: object = None,
                           opening_intent: object = None) -> int:
        captured["change"] = change
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    args = _new_args(opening="add a dark-mode toggle", non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)
    assert rc == 0

    change = captured["change"]
    cid = change.id  # type: ignore[union-attr]
    assert change.materialized is False                                  # type: ignore[union-attr]
    assert not (tmp_path / ".imp" / "worktrees" / cid).exists()          # no worktree
    assert _git(tmp_path, "branch", "--list", f"imp/{cid}").stdout.strip() == ""  # no branch
    assert (tmp_path / ".imp" / "changes" / cid / "change.json").exists()  # permanent home
