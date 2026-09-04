"""Emit-time tagging: an agent_message whose prose anchor-classifies as a provider
error carries `provider_error`, and the per-turn journaled row sequence puts only
bookkeeping rows between that message and the error."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.engine.state import JsonlEventLog
from imp.providers.claude_code import ClaudeCodeAgentRunner
from imp.providers.codex_runner import CodexAgentRunner
from imp.providers.observe_classify import classifies_as_provider_error
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use
from tests.codex_fakes import (
    FakeCodex,
    agent_message,
    mcp_call,
    thread_started,
    turn_completed,
)
from tests.test_claude_code_runner import _inv
from tests.test_codex_cancel_paths import _invocation, _seed_git_and_auth

AUTH_PROSE = "Failed to authenticate. API Error: 401 OAuth access token has been revoked."
ORDINARY = "Reading the config file and planning the edits."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    return list(JsonlEventLog(tmp_path / "e.jsonl").read())


def test_predicate_is_anchored_and_matches_the_error_path() -> None:
    assert classifies_as_provider_error(AUTH_PROSE) is True
    assert classifies_as_provider_error(ORDINARY) is False
    # Anchored: prose that merely quotes the pattern mid-document is not an error.
    assert classifies_as_provider_error(f"Here is my review of auth.\n{AUTH_PROSE}") is False


@pytest.mark.anyio
async def test_claude_code_tags_the_failing_turn_and_pins_the_row_order(
    tmp_path: Path,
) -> None:
    (tmp_path / "art").mkdir()
    elog = JsonlEventLog(tmp_path / "e.jsonl")
    scripted = [assistant(text(AUTH_PROSE)), result(is_error=False)]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(model_name="m", client_factory=factory)
    events = [e async for e in runner.run(_inv(tmp_path, event_log=elog))]

    rows = _rows(tmp_path)
    # The forced auth probe journals its outcome after the turn's query_result.
    assert [r["type"] for r in rows[-3:]] == [
        "agent_message", "query_result", "token_refresh"]
    assert rows[-3]["payload"]["provider_error"] is True
    errs = [e for e in events if e.get("type") == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["detail"]["last_agent_text"] == AUTH_PROSE


@pytest.mark.anyio
async def test_claude_code_splits_the_group_at_the_error_fragment(
    tmp_path: Path,
) -> None:
    # A classifying fragment triggers a flush so it gets its own tagged node;
    # the preceding ordinary fragment must keep its own untagged node.
    (tmp_path / "art").mkdir()
    elog = JsonlEventLog(tmp_path / "e.jsonl")
    scripted = [assistant(text(ORDINARY)), assistant(text(AUTH_PROSE)),
                result(is_error=False)]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(model_name="m", client_factory=factory)
    events = [e async for e in runner.run(_inv(tmp_path, event_log=elog))]

    msgs = [r for r in _rows(tmp_path) if r["type"] == "agent_message"]
    assert len(msgs) == 2                        # split at the classifying boundary
    assert [b["text"] for b in msgs[0]["payload"]["raw"]["content"]] == [ORDINARY]
    assert "provider_error" not in msgs[0]["payload"]
    assert [b["text"] for b in msgs[1]["payload"]["raw"]["content"]] == [AUTH_PROSE]
    assert msgs[1]["payload"]["provider_error"] is True
    assert [e["payload"]["category"] for e in events
            if e.get("type") == "error"] == ["auth"]


@pytest.mark.anyio
async def test_claude_code_leaves_ordinary_prose_untagged(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    elog = JsonlEventLog(tmp_path / "e.jsonl")
    scripted = [
        assistant(text(ORDINARY),
                  tool_use("c1", "complete_step", {"status": "Success", "note": "ok"})),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(model_name="m", client_factory=factory)
    [e async for e in runner.run(_inv(tmp_path, event_log=elog))]

    msgs = [r for r in _rows(tmp_path) if r["type"] == "agent_message"]
    assert len(msgs) == 1
    assert "provider_error" not in msgs[0]["payload"]


@pytest.mark.anyio
async def test_codex_tags_the_failing_turn_and_pins_the_row_order(tmp_path: Path) -> None:
    auth = _seed_git_and_auth(tmp_path)
    elog = JsonlEventLog(tmp_path / "e.jsonl")
    scripted = [[thread_started("t"), agent_message(AUTH_PROSE),
                 turn_completed(output_tokens=1)]]
    fake = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(model_name="gpt-5.5",
                              client_factory=lambda options=None: fake)
    runner._canonical_auth_override = auth

    events = [e async for e in runner.run(_invocation(tmp_path, event_log=elog))]

    # The fake writes no rollout, so every turn also emits the once-per-run
    # no-context-reading canary; it is not part of the ordering invariant.
    rows = [r for r in _rows(tmp_path) if r["type"] != "runner_warning"]
    # codex's bookkeeping row is the usage patch, written straight to disk.
    assert [r["type"] for r in rows[-2:]] == ["agent_message", "agent_message_usage_patch"]
    assert rows[-2]["payload"]["provider_error"] is True
    errs = [e for e in events if e.get("type") == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]


@pytest.mark.anyio
async def test_codex_splits_the_flush_so_ordinary_prose_keeps_its_own_node(
    tmp_path: Path,
) -> None:
    # A classifying item triggers a flush before being buffered; the preceding
    # ordinary item must keep its own untagged node.
    auth = _seed_git_and_auth(tmp_path)
    elog = JsonlEventLog(tmp_path / "e.jsonl")
    scripted = [[thread_started("t"), agent_message(ORDINARY), agent_message(AUTH_PROSE),
                 turn_completed(output_tokens=1)]]
    fake = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(model_name="gpt-5.5",
                              client_factory=lambda options=None: fake)
    runner._canonical_auth_override = auth

    events = [e async for e in runner.run(_invocation(tmp_path, event_log=elog))]

    msgs = [r for r in _rows(tmp_path) if r["type"] == "agent_message"]
    assert len(msgs) == 2                        # split at the classifying boundary
    assert [b["text"] for b in msgs[0]["payload"]["raw"]["content"]] == [ORDINARY]
    assert "provider_error" not in msgs[0]["payload"]
    assert [b["text"] for b in msgs[1]["payload"]["raw"]["content"]] == [AUTH_PROSE]
    assert msgs[1]["payload"]["provider_error"] is True
    assert [e["payload"]["category"] for e in events
            if e.get("type") == "error"] == ["auth"]


@pytest.mark.anyio
async def test_codex_leaves_ordinary_prose_untagged(tmp_path: Path) -> None:
    auth = _seed_git_and_auth(tmp_path)
    elog = JsonlEventLog(tmp_path / "e.jsonl")
    scripted = [[thread_started("t"), agent_message(ORDINARY),
                 mcp_call("complete_step", args={"answer": "ok"},
                          result={"ok": True}, item_id="i1"),
                 turn_completed(output_tokens=1)]]
    fake = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(model_name="gpt-5.5",
                              client_factory=lambda options=None: fake)
    runner._canonical_auth_override = auth

    [e async for e in runner.run(_invocation(tmp_path, event_log=elog))]

    msgs = [r for r in _rows(tmp_path) if r["type"] == "agent_message"]
    assert len(msgs) == 1
    assert "provider_error" not in msgs[0]["payload"]
