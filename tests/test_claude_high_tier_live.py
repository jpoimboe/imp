"""One real claude turn at BUILTIN_FLOOR's `high` model, driven through
ClaudeAgentRunner.run against a live claude (client_factory=None).

The binary matters as much as the model here. imp pins cli_path to the `claude` on
PATH, and the server refuses a model the CLI is too old to serve with a 400
(`claude_code_version_too_old`) that does NOT raise through the SDK: the turn ends
ResultMessage(is_error=True), which imp classifies into an `error` event. Failing
against a too-old PATH binary is the signal this test exists to raise, so the gate
is PATH presence only — never a version check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.engine.completion import completion_instruction
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers import BUILTIN_FLOOR
from imp.providers.claude import ClaudeAgentRunner
from tests._live import path_claude_skip_reason
from tests.conftest import _init_repo

pytestmark = pytest.mark.live("claude")

# The floor itself, not a copy: this test follows wherever the default moves.
_HIGH_TIER_MODEL = BUILTIN_FLOOR["claude"].tier_defaults["high"]

_NUDGE_TEXT = (
    "Your turn ended without calling complete_step. There is no human to "
    "wait for — call complete_step now with status=Success."
)


def _one_nudge():
    """Absorb one prose-only turn end — a legitimate yield — then fail loud."""
    remaining = 1

    def _cont(_last_text: str) -> tuple[str, bool]:
        nonlocal remaining
        if remaining <= 0:
            raise AssertionError(
                "live model never called complete_step, even after one nudge")
        remaining -= 1
        return (_NUDGE_TEXT, False)

    return _cont


def _inv(tmp_path: Path) -> AgentInvocation:
    agent_def = AgentDef(
        name="pinger", description="",
        tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )
    return AgentInvocation(
        agent_def=agent_def,
        provider_name="claude", model_name=_HIGH_TIER_MODEL,
        # imp's production completion instruction verbatim, as agent_run.py sends it.
        system_prompt=(
            completion_instruction(agent_def.output_schema)
            + "\n\nDo no work and call no other tool. Call complete_step with "
              "status=Success now."
        ),
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        continuation_fn=_one_nudge(),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=(project_state_dir(tmp_path) / "quests" / "test-quest"
                          / "workflow"),
        ),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=InMemoryEventLog(),
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_high_tier_default_completes_a_real_turn(tmp_path: Path) -> None:
    reason = path_claude_skip_reason()
    if reason is not None:
        pytest.skip(reason)
    _init_repo(tmp_path)
    runner = ClaudeAgentRunner(model_name=_HIGH_TIER_MODEL)

    events = [ev async for ev in runner.run(_inv(tmp_path))]

    errors = [e for e in events if e["type"] == "error"]
    assert not errors, (
        f"the high-tier default {_HIGH_TIER_MODEL} failed a real turn through the "
        f"`claude` on PATH. A `claude_code_version_too_old` message means that "
        f"binary cannot serve this model — upgrade it.\n{errors}")
    assert events[-1]["type"] == "agent_result", events[-1]
    assert events[-1]["payload"]["status"] == "Success", events[-1]
