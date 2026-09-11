from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import imp.cli.main as main
from imp.engine.quest import Quest
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def stub_session(monkeypatch):
    """Collects the quests handed to the session client, which never runs. Every
    _async_cmd_new test takes it: an unstubbed refusal test that regressed would
    reach the real client and hang the suite instead of failing it."""
    seen: list[object] = []

    async def _fake_rsc(quest, **kw):
        seen.append(quest)
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _fake_rsc)
    return seen


def _ns(**over: object) -> argparse.Namespace:
    base = dict(
        base=None, workflow=None, idea=None, title=None,
        non_interactive=True, autonomous=False, message=None,
        opening="do a thing", depends_on=None, provider=None)
    base.update(over)
    return argparse.Namespace(**base)


def _pin_of(quest_dir: Path) -> object:
    return json.loads((quest_dir / "quest.json").read_text())["provider"]


def _subparser(name: str) -> argparse.ArgumentParser:
    parser = main._build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]
    raise AssertionError("no subparsers")


def _provider_help(name: str) -> str:
    sp = _subparser(name)
    for action in sp._actions:
        if "--provider" in (action.option_strings or []):
            return action.help
    raise AssertionError(f"no --provider action on {name!r}")


def test_provider_flag_help_strings_are_verbatim() -> None:
    # The spec pins these strings verbatim and nothing else asserts them.
    shared = "Run this quest on NAME, whatever the configured provider order is."
    assert _provider_help("new") == shared
    assert _provider_help("chat") == shared
    assert _provider_help("resume") == (
        "Switch the quest to NAME for its remaining steps; "
        "the current step picks up from the event log."
    )


def test_new_parser_accepts_the_provider_flag() -> None:
    args = main._build_parser().parse_args(["new", "x", "--provider", "codex"])
    assert args.provider == "codex"


def test_chat_parser_accepts_the_provider_flag() -> None:
    args = main._build_parser().parse_args(["chat", "--provider", "codex"])
    assert args.provider == "codex"


def test_provider_defaults_to_none_on_both_parsers() -> None:
    assert main._build_parser().parse_args(["new", "x"]).provider is None
    assert main._build_parser().parse_args(["chat"]).provider is None


@pytest.mark.anyio
async def test_an_unknown_provider_exits_two_and_names_both_groups(
    tmp_path, monkeypatch, capsys, floor_availability, stub_session,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    floor_availability({"claude_code", "codex"})

    rc = await main._async_cmd_new(_ns(provider="bogus"))

    assert rc == 2
    assert capsys.readouterr().err == (
        "imp: --provider: unknown provider 'bogus' (available here: claude_code, "
        "codex; needs setup: anthropic, openai)\n")
    assert stub_session == []          # refused before any quest was created


@pytest.mark.anyio
async def test_every_vendor_available_drops_the_needs_setup_clause(
    tmp_path, monkeypatch, capsys, floor_availability, stub_session,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    floor_availability({"anthropic", "openai", "claude_code", "codex"})

    rc = await main._async_cmd_new(_ns(provider="bogus"))

    assert rc == 2
    assert capsys.readouterr().err == (
        "imp: --provider: unknown provider 'bogus' (available here: anthropic, "
        "openai, claude_code, codex)\n")
    assert stub_session == []


@pytest.mark.anyio
async def test_scripted_is_refused_like_any_unknown_name(
    tmp_path, monkeypatch, capsys, floor_availability, stub_session,
) -> None:
    # tier_defaults is None, so it must never reach resolve_run.
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    floor_availability({"claude_code", "codex"})

    rc = await main._async_cmd_new(_ns(provider="scripted"))

    assert rc == 2
    assert "unknown provider 'scripted'" in capsys.readouterr().err
    assert stub_session == []


@pytest.mark.anyio
async def test_a_selectable_but_unavailable_vendor_gets_its_auth_hint(
    tmp_path, monkeypatch, capsys, floor_availability, stub_session,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    floor_availability({"claude_code"})

    rc = await main._async_cmd_new(_ns(provider="codex"))

    assert rc == 2
    assert capsys.readouterr().err == (
        "imp: --provider: codex is not available — run `codex login`\n")
    assert stub_session == []


@pytest.mark.anyio
async def test_new_writes_the_pin_to_the_record_before_the_run_starts(
    tmp_path, monkeypatch, floor_availability, stub_session,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    floor_availability({"claude_code", "codex"})
    monkeypatch.setattr(main, "_select_headless", lambda args: (True, False))

    rc = await main._async_cmd_new(_ns(provider="codex"))

    assert rc == 0
    # Read from disk, not off the instance: the daemon that runs this quest is a
    # different process and quest.json is all it gets.
    assert _pin_of(stub_session[0].quest_dir) == "codex"


@pytest.mark.anyio
async def test_a_pin_is_honored_when_the_configured_vendor_is_unavailable(
    tmp_path, monkeypatch, floor_availability, stub_session,
) -> None:
    # The headline case: _seed_control configures `providers: [anthropic]`, and
    # only codex is available. Reaching for a vendor you don't normally use is
    # the point, so the configured order's own availability must not veto it.
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    floor_availability({"codex"})
    monkeypatch.setattr(main, "_select_headless", lambda args: (True, False))

    rc = await main._async_cmd_new(_ns(provider="codex"))

    assert rc == 0
    assert _pin_of(stub_session[0].quest_dir) == "codex"


@pytest.mark.anyio
async def test_an_unpinned_run_still_needs_a_configured_vendor(
    tmp_path, monkeypatch, capsys, floor_availability, stub_session,
) -> None:
    # The control for the test above: with no pin the preflight still refuses,
    # so the skip is scoped to the pin rather than deleting the check.
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    floor_availability({"codex"})

    rc = await main._async_cmd_new(_ns())

    assert rc == 2
    assert "no authenticated provider" in capsys.readouterr().err
    assert stub_session == []


@pytest.mark.anyio
async def test_chat_writes_the_pin_through_its_own_draft_path(
    tmp_path, monkeypatch, floor_availability, stub_session,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    floor_availability({"claude_code", "codex"})
    monkeypatch.setattr(main, "_select_headless", lambda args: (True, False))

    rc = await main._async_cmd_new(_ns(provider="codex"), leave_mode="chat")

    assert rc == 0
    meta = json.loads((stub_session[0].quest_dir / "quest.json").read_text())
    assert meta["provider"] == "codex"
    assert meta["create_mode"] == "chat"     # the chat draft, not the new one


@pytest.mark.anyio
async def test_no_flag_leaves_the_pin_null(
    tmp_path, monkeypatch, floor_availability, stub_session,
) -> None:
    # anthropic is available here because with no pin this path still clears the
    # preflight, which asks about the configured order.
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    floor_availability({"anthropic", "claude_code", "codex"})
    monkeypatch.setattr(main, "_select_headless", lambda args: (True, False))

    rc = await main._async_cmd_new(_ns())

    assert rc == 0
    assert _pin_of(stub_session[0].quest_dir) is None


@pytest.mark.anyio
async def test_a_backlogged_quest_keeps_the_pin_for_its_later_resume(
    tmp_path, monkeypatch, floor_availability, stub_session,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    floor_availability({"claude_code", "codex"})

    # non_interactive stays False: --backlog refuses to be combined with a
    # run-only flag (_backlog_run_only_flag reads --headless first).
    rc = await main._async_cmd_new(
        _ns(idea="file this", opening=None, non_interactive=False,
            provider="codex"))

    assert rc == 0
    (quest_id,) = main._live_quest_ids(tmp_path)
    assert Quest.load(tmp_path, quest_id).provider == "codex"


def test_apply_new_records_the_pin_on_the_quest_it_files(tmp_path) -> None:
    # The non-interactive create the mutation proxy and --background both go
    # through; dropping the pin here would make `imp new` inside a session lie.
    _seed_control(tmp_path)

    rc, _message = main.apply_new(
        tmp_path, opening="x", title="t", workflow=None, base=None, idea=None,
        depends_on=None, provider="codex")

    assert rc == 0
    (quest_id,) = main._live_quest_ids(tmp_path)
    assert Quest.load(tmp_path, quest_id).provider == "codex"


def test_apply_new_background_records_the_pin_and_calls_spawn(
    tmp_path, monkeypatch,
) -> None:
    _seed_control(tmp_path)
    spawned: list[tuple] = []
    monkeypatch.setattr(
        "imp.remote.daemon.spawn.start_quest",
        lambda control, quest_id: spawned.append((control, quest_id)))

    rc, _message = main.apply_new(
        tmp_path, opening="x", title="t", workflow=None, base=None, idea=None,
        depends_on=None, background=True, provider="codex")

    assert rc == 0
    (quest_id,) = main._live_quest_ids(tmp_path)
    assert Quest.load(tmp_path, quest_id).provider == "codex"
    assert spawned, "spawn.start_quest was not called"


def test_new_parser_rejects_the_removed_model_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main._build_parser().parse_args(
            ["new", "x", "--model", "anthropic:claude-fable-5"])
    assert exc_info.value.code == 2
    assert "unrecognized arguments: --model" in capsys.readouterr().err


def test_chat_parser_rejects_the_removed_model_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main._build_parser().parse_args(["chat", "--model", "claude-fable-5"])
    assert exc_info.value.code == 2
    assert "unrecognized arguments: --model" in capsys.readouterr().err


def test_the_model_arg_type_validator_is_gone() -> None:
    assert not hasattr(main, "_model_arg_type")
