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


def test_build_parser_new_rejects_spec_file() -> None:
    with pytest.raises(SystemExit):
        cli_main._build_parser().parse_args(["new", "--spec-file", "draft.md"])


def _new_args(**kw: object) -> argparse.Namespace:
    base = {"opening": None, "title": None, "non_interactive": False,
            "message": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_resolve_positional_is_verbatim_seed() -> None:
    res = cli_main.resolve_spec_seed(_new_args(opening="add a dark-mode toggle"))
    assert res == cli_main.SeedResolution(
        title=None, seed_text="add a dark-mode toggle")


def test_resolve_positional_file_path_is_literal_text(tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("# Draft\nGoal: do the thing\n")
    res = cli_main.resolve_spec_seed(_new_args(opening=str(draft)))
    assert res.title is None
    assert res.seed_text == str(draft)


def test_resolve_title_flag_sets_title_no_seed() -> None:
    res = cli_main.resolve_spec_seed(_new_args(title="dark mode"))
    assert res == cli_main.SeedResolution(title="dark mode", seed_text=None)


def test_resolve_no_args_is_empty() -> None:
    res = cli_main.resolve_spec_seed(_new_args())
    assert res == cli_main.SeedResolution(title=None, seed_text=None)


def test_resolve_whitespace_only_positional_is_empty() -> None:
    res = cli_main.resolve_spec_seed(_new_args(opening="   "))
    assert res == cli_main.SeedResolution(title=None, seed_text=None)


def test_resolve_ignores_spec_file_attribute() -> None:
    # The --spec-file seed path is gone: a stray spec_file attr is never read,
    # so it neither raises a mutual-exclusion error nor wraps the seed.
    res = cli_main.resolve_spec_seed(
        argparse.Namespace(opening="add a flag", title=None, message=None,
                           spec_file="/some/draft.md"))
    assert res.seed_text == "add a flag"


def test_new_positional_delivers_verbatim_seed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object,
                           status: object = None,
                           title: object = None,
                           non_interactive: object = False,
                           autonomous: object = False,
                           model: str | None = None) -> int:
        captured["change"] = change
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    args = _new_args(opening="add a dark-mode toggle", non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    # The intent is persisted durably to change.json, not threaded as a run param.
    assert captured["change"].seed_opening == "add a dark-mode toggle"  # type: ignore[union-attr]
    assert captured["change"].title == ""  # type: ignore[union-attr]


def test_new_title_flag_sets_quest_title(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object,
                           status: object = None,
                           title: object = None,
                           non_interactive: object = False,
                           autonomous: object = False,
                           model: str | None = None) -> int:
        captured["change"] = change
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    args = _new_args(title="dark mode", non_interactive=True)
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    assert captured["change"].seed_opening is None  # type: ignore[union-attr]
    assert captured["change"].title == "dark mode"  # type: ignore[union-attr]


def test_new_positional_with_message_legal(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object,
                           status: object = None,
                           title: object = None,
                           non_interactive: object = False,
                           autonomous: object = False,
                           model: str | None = None) -> int:
        captured["change"] = change
        captured["reply"] = await solicit("")  # type: ignore[operator]
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    args = _new_args(opening="add dark mode", non_interactive=True, message="reply")
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 0
    assert captured["change"].seed_opening == "add dark mode"  # type: ignore[union-attr]
    assert captured["reply"] == "reply"


def test_threshold_opening_conditioned_on_message(tmp_path) -> None:
    body = load_workflow(bundled_workflow_dir("imp")).agents[
        "threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/changes/abc", project_facts={}),
        workflow_dir=bundled_workflow_dir("imp"))
    # Rendered with no opening -> the unseeded (bare-open) branch, no <imp:begin/>.
    assert "<imp:begin/>" not in out
    assert "Open by asking" in out
    # Rendered WITH an opening -> the seeded branch carries the verbatim intent.
    seeded = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/changes/abc", project_facts={},
        opening="wire up telemetry"),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "wire up telemetry" in seeded
    assert "Open by asking" not in seeded


def test_threshold_prompt_has_verbatim_opening_branch(tmp_path) -> None:
    body = load_workflow(bundled_workflow_dir("imp")).agents[
        "threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/changes/abc", project_facts={}),
        workflow_dir=bundled_workflow_dir("imp"))
    # Unseeded render -> the bare-open branch. The seeded branch no longer uses
    # "verbatim opening intent" wording; it is titled "Opening message in hand".
    assert "verbatim opening intent" not in out
    assert "Opening message in hand" not in out
    assert "Open by asking" in out
    seeded = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/changes/abc", project_facts={},
        opening="wire up telemetry"),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "Opening message in hand" in seeded     # seeded branch title
    assert "wire up telemetry" in seeded          # ...and the verbatim opening
    # No spec is inlined into the prompt; the user names a spec file threshold reads.
    assert "imp new <draft>" not in out
    assert "imp new <file>" not in out


def test_threshold_prompt_has_read_and_verify_section(tmp_path) -> None:
    body = load_workflow(bundled_workflow_dir("imp")).agents[
        "threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/changes/abc", project_facts={}),
        workflow_dir=bundled_workflow_dir("imp"))
    # The seeded/adopt marker is gone; a named spec is read and verified instead.
    assert "Reading a referenced spec file" in out
    assert "Implement this spec" not in out


def test_opening_prompt_reply_wires_to_seed_opening(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object,
                           status: object = None,
                           leave_mode: object = None,
                           leave_state: object = None,
                           title: object = None,
                           non_interactive: object = False,
                           autonomous: object = False,
                           model: str | None = None) -> int:
        captured["change"] = change
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    async def _fake_ask(self: object, *, width: object = None,
                        hint: object = None, **kwargs: object) -> str:
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
    assert captured["change"].seed_opening == "add a feature"  # type: ignore[union-attr]


def test_opening_prompt_empty_reply_gives_none_seed_opening(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object,
                           status: object = None,
                           leave_mode: object = None,
                           leave_state: object = None,
                           title: object = None,
                           non_interactive: object = False,
                           autonomous: object = False,
                           model: str | None = None) -> int:
        captured["change"] = change
        return 0

    monkeypatch.setattr(cli_main, "run_session_client", _fake_client)

    async def _fake_ask(self: object, *, width: object = None,
                        hint: object = None, **kwargs: object) -> str:
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
    assert captured["change"].seed_opening is None  # type: ignore[union-attr]


def test_new_creates_unmaterialized_draft(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    async def _fake_client(change: object, *, driver: object, coord: object,
                           solicit: object,
                           status: object = None,
                           title: object = None,
                           non_interactive: object = False,
                           autonomous: object = False,
                           model: str | None = None) -> int:
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
