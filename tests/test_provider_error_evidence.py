"""Session-level: an agent essay that quotes a provider error string survives its
turn, genuine CLI chrome still terminates, and the recorded evidence never leaks
from one turn to the next."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import TextBlock
from imp.agents.transcript import Transcript
from imp.engine.state import JsonlEventLog
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import FakeClaudeSDKClient, assistant, chrome, result, text, tool_use
from tests.test_claude_code_runner import _inv

QUOTA_CHROME = "You have reached your usage limit"
# zopo54: the quota pattern sits in the opening markdown paragraph, so the
# first-line anchor alone does not save it.
ESSAY = (
    'A true quota error (`"You have reached your usage limit"`) maps to an immediate '
    "pause, so the classifier must not fire on prose that merely discusses it.\n\n"
    "The fix gates the scan on recorded evidence of who wrote the message."
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    return list(JsonlEventLog(tmp_path / "e.jsonl").read())


def _cc_runner(scripted: list[Any]) -> ClaudeCodeAgentRunner:
    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    return ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)


def _nudging(inv: Any) -> Any:
    return replace(inv, continuation_fn=lambda _last: ("please call complete_step", False))


def _assistant_texts(transcript_dir: Path) -> list[str]:
    return [b.text
            for m in Transcript(transcript_dir / "transcript.jsonl").load()
            if m.role == "assistant"
            for b in m.content if isinstance(b, TextBlock)]


@pytest.mark.anyio
async def test_claude_essay_quoting_a_quota_pattern_is_not_an_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "art").mkdir()
    elog = JsonlEventLog(tmp_path / "e.jsonl")
    inv = _nudging(_inv(tmp_path, event_log=elog))
    scripted = [
        assistant(text(ESSAY), usage={"output_tokens": 1763}, stop_reason="end_turn"),
        result(),
        assistant(tool_use("c1", "complete_step", {"status": "Success", "note": "ok"})),
        result(),
    ]

    events = [e async for e in _cc_runner(scripted).run(inv)]

    assert [e for e in events if e.get("type") == "error"] == []
    assert events[-1]["type"] == "agent_result"
    # The turn was not rolled back: the essay is still in the transcript.
    assert ESSAY in _assistant_texts(inv.transcript_dir)
    msgs = [r for r in _rows(tmp_path) if r["type"] == "agent_message"]
    assert "provider_error" not in msgs[0]["payload"]


@pytest.mark.anyio
async def test_claude_genuine_chrome_still_errors_and_rolls_back(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    elog = JsonlEventLog(tmp_path / "e.jsonl")
    inv = _inv(tmp_path, event_log=elog)
    scripted = [chrome(QUOTA_CHROME), result()]

    events = [e async for e in _cc_runner(scripted).run(inv)]

    errs = [e for e in events if e.get("type") == "error"]
    assert [e["payload"]["category"] for e in errs] == ["quota"]
    loaded = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    assert [m.role for m in loaded] == ["user"]
    msgs = [r for r in _rows(tmp_path) if r["type"] == "agent_message"]
    assert msgs[-1]["payload"]["provider_error"] is True


@pytest.mark.anyio
async def test_claude_chrome_evidence_does_not_leak_into_the_next_turn(
    tmp_path: Path,
) -> None:
    # Turn 1 carries chrome evidence but matches no pattern, so the run continues
    # (run_agent_loop returns on the first Error). Turn 2 quotes a pattern and
    # records no usage: only a stale stash could classify it.
    (tmp_path / "art").mkdir()
    elog = JsonlEventLog(tmp_path / "e.jsonl")
    inv = _nudging(_inv(tmp_path, event_log=elog))
    scripted = [
        chrome("Turn one: nothing to report."), result(),
        assistant(text(ESSAY)), result(),
        assistant(tool_use("c1", "complete_step", {"status": "Success", "note": "ok"})),
        result(),
    ]

    events = [e async for e in _cc_runner(scripted).run(inv)]

    assert [e for e in events if e.get("type") == "error"] == []
    assert events[-1]["type"] == "agent_result"
    assert ESSAY in _assistant_texts(inv.transcript_dir)
