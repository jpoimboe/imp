"""Live drift canary for codex's rollout `token_count` record, gated behind
`--run-live` (codex). Drives a turn with several sequential model requests and
asserts the recorded occupancy sits far below that turn's billing prompt sum —
the whole reason imp reads the rollout instead of `turn.completed.usage`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, QuestContext
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.completion import build_continuation_fn
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_runner import CodexAgentRunner
from tests._live import stale_codex_oauth_skip_reason

pytestmark = pytest.mark.live("codex")

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _git_init(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t.t"],
                   check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "T"], check=True)
    (p / "README.md").write_text("# t\n")
    subprocess.run(["git", "-C", str(p), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-m", "init", "-q"],
                   check=True)


def _inv(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=AgentDef(
            name="globber", description="list files a few times",
            tools=frozenset({"complete_step", "glob"}), subagents=(),
            output_schema=OutputSchema(
                fields=(OutputField(name="word", type="string", required=True),),
                source=_SRC),
            prompt_template_source="", source_path=Path("a.md"), source=_SRC,
            tier=None),
        provider_name="codex", model_name="gpt-5.6-luna",
        system_prompt=(
            'Call the `glob` tool with pattern "*" and wait for its result. '
            'Then call `glob` again with pattern "**/*.md" and wait. Then call '
            '`glob` a third time with pattern "**/*.py" and wait. Only after '
            'all three results have come back, call complete_step with '
            'word="banana". Do not write any files.'),
        initial_messages=(),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx", events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
        continuation_fn=build_continuation_fn(headless=True, has_schema=True))


def _prompt_sum(usage: dict[str, Any]) -> int:
    return (usage["input_tokens"] + usage["cache_read_input_tokens"]
            + usage["cache_creation_input_tokens"])


@pytest.mark.anyio
async def test_codex_live_occupancy_is_far_below_the_turn_billing_sum(
        tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "tx").mkdir()
    runner = CodexAgentRunner(model_name="gpt-5.6-luna")
    try:
        events = [ev async for ev in runner.run(_inv(tmp_path))]
    except AwaitingUserInput:
        pytest.skip("codex parked the run before finishing the turn")
    reason = stale_codex_oauth_skip_reason(events)
    if reason is not None:
        pytest.skip(reason)

    rows = [json.loads(ln)
            for ln in (tmp_path / "events.jsonl").read_text().splitlines() if ln]
    # Group by turn: a usage-patch row closes the turn whose tool_calls precede
    # it. AC 11 wants a turn of >= 3 sequential model requests, i.e. >= 2 tool
    # calls in that one turn — two calls spread over two turns proves nothing.
    multi: list[dict[str, Any]] = []
    calls_this_turn = 0
    for r in rows:
        if r.get("type") == "tool_call":
            calls_this_turn += 1
        elif r.get("type") == "agent_message_usage_patch":
            if calls_this_turn >= 2:
                multi.append(r["payload"])
            calls_this_turn = 0
    if not multi:
        pytest.skip("codex never made 2+ tool calls within one turn — too few "
                    "sequential model requests to compare against the sum")

    readings = [(p.get("context_tokens"), p.get("context_window"),
                 _prompt_sum(p["usage"])) for p in multi]
    # A reading exists at all — the record shape still parses. A turn whose
    # reading lagged records neither field and is not drift, so it is skipped
    # rather than failed.
    assert any(t for t, _w, _s in readings), readings
    # (b) every reading present carries a populated, positive window.
    assert all(w and w > 0 for t, w, _s in readings if t), readings
    # (a) the tool-heavy turn summed several requests, so its occupancy must be
    # a fraction of the billing sum, not merely below it.
    assert any(t and t * 2 <= s for t, _w, s in readings), readings
