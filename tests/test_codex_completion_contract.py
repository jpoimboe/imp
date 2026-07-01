from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.engine.types import (
    AgentDef,
    OutputField,
    OutputSchema,
    SourceLoc,
)
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import (
    FakeCodex,
    agent_message,
    mcp_call,
    thread_started,
    turn_completed,
)

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _git_init(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(
        ["git", "-C", str(p), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(
        ["git", "-C", str(p), "config", "user.name", "T"], check=True)
    (p / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(p), "add", ".gitkeep"], check=True)
    subprocess.run(
        ["git", "-C", str(p), "commit", "-q", "-m", "init"], check=True)


def _seed_git_and_auth(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)


def _agent_def() -> AgentDef:
    # `status` must be one of {"ok","bad"} — letting us provoke a schema
    # validation failure with an out-of-enum value.
    return AgentDef(
        name="t",
        description="",
        tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(
                OutputField(
                    name="status", type="enum",
                    values=("ok", "bad"), required=True),
            ),
            source=_SRC,
        ),
        prompt_template_source="",
        source_path=Path("a.md"),
        source=_SRC,
        tier=None,
    )


def _inv(
    tmp_path: Path,
) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="sp",
        initial_messages=(),
        change_context=ChangeContext(
            title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
    )


def _inv_with_continuation(
    tmp_path: Path, *, continuation_fn: object,
) -> AgentInvocation:
    """Invocation with a `continuation_fn` for nudges.

    When the codex run finishes a turn without calling `complete_step`, the
    runner calls `continuation_fn(last_agent_text)` and uses the return value
    as the next turn's prompt."""
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="sp",
        initial_messages=(),
        change_context=ChangeContext(
            title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
        continuation_fn=continuation_fn,  # type: ignore[arg-type]
    )


@pytest.mark.anyio
async def test_schema_validation_failure_yields_agent_output_missing(
    tmp_path: Path,
) -> None:
    _seed_git_and_auth(tmp_path)
    scripted = [[
        thread_started("thr-1"),
        mcp_call(name="complete_step",
                 args={"status": "out-of-enum"},  # bad enum
                 result="step complete"),
        turn_completed(),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = tmp_path / "auth.json"
    inv = _inv(tmp_path)
    events = [ev async for ev in runner.run(inv)]
    err = next(e for e in events if e.get("type") == "error")
    assert err["payload"]["category"] == "agent_output_missing"
    assert err["payload"]["kind"] == "output_validation"


@pytest.mark.anyio
async def test_classified_auth_text_wins_over_no_complete_step(
    tmp_path: Path,
) -> None:
    """If the last agent text parses as a known provider error, yield THAT
    instead of `no_complete_step`. Avoids the nudge loop on auth/quota.

    The text "Invalid API key" is one of `_classify_agent_text`'s auth
    patterns → `category=auth`, short-circuiting nudge/no_complete_step."""
    _seed_git_and_auth(tmp_path)
    scripted = [[
        thread_started(),
        agent_message("Invalid API key — please check your credentials"),
        turn_completed(),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = tmp_path / "auth.json"
    inv = _inv(tmp_path)
    events = [ev async for ev in runner.run(inv)]
    err = next(e for e in events if e.get("type") == "error")
    assert err["payload"]["category"] == "auth"


@pytest.mark.anyio
async def test_interactive_nudge_recovers_with_complete_step_on_second_turn(
    tmp_path: Path,
) -> None:
    """Interactive + no complete_step on turn 1 → runner calls
    `continuation_fn(last_text)`, runs a second turn with the returned text,
    and that second turn DOES call complete_step → success."""
    _seed_git_and_auth(tmp_path)
    scripted = [
        [thread_started("thr-1"),
         agent_message("forgot to call complete_step"),
         turn_completed()],
        [thread_started("thr-1"),
         mcp_call(name="complete_step",
                  args={"status": "ok"}, result="step complete"),
         turn_completed()],
    ]
    seen: list[str] = []

    def _cont(last: str) -> str:
        seen.append(last)
        return "please call complete_step now"

    fake = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: fake,
    )
    runner._canonical_auth_override = tmp_path / "auth.json"
    inv = _inv_with_continuation(tmp_path, continuation_fn=_cont)
    events = [ev async for ev in runner.run(inv)]
    # Second turn captured complete_step; runner yields agent_output, not error.
    outs = [e for e in events if e.get("type") == "agent_output"]
    assert len(outs) == 1 and outs[0]["payload"]["status"] == "ok"
    # continuation_fn was called exactly once with turn-1's last text.
    assert seen == ["forgot to call complete_step"]
    # The second turn's run_streamed received the nudge prompt verbatim.
    assert fake.thread is not None
    assert any("please call complete_step now" in str(p)
               for (p, _opt) in fake.thread.run_calls)


def _inv_with_task(
    tmp_path: Path,
    *,
    task_text: str,
    balance_on_resume: bool = False,
    subagent_depth: int = 0,
) -> AgentInvocation:
    """A codex invocation whose task rides in the first user message."""
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="ROLE_HEADER_SENTINEL",
        initial_messages=(
            Message(role="user", content=(TextBlock(text=task_text),)),),
        change_context=ChangeContext(
            title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
        balance_on_resume=balance_on_resume,
        subagent_depth=subagent_depth,
    )


def test_build_prompt_folds_first_user_message_on_fresh_entry(
    tmp_path: Path,
) -> None:
    # Fresh entry: the task must be folded after the role header and tool policy.
    task = (
        "RENDER_BODY_SENTINEL_9f3a\n\n"
        "To finish this step you MUST call the `complete_step` tool with: "
        "`status` (ok|bad)."
    )
    inv = _inv_with_task(tmp_path, task_text=task)
    built = CodexAgentRunner._build_prompt(None, inv)
    assert built.startswith("ROLE_HEADER_SENTINEL")   # role header first
    assert "tool_search" in built                     # tool policy retained
    assert "RENDER_BODY_SENTINEL_9f3a" in built       # rendered body folded in
    assert "complete_step" in built                   # completion contract folded in
    # Fold is appended after the policy, not before it.
    assert built.index("tool_search") < built.index("RENDER_BODY_SENTINEL_9f3a")


def test_build_prompt_omits_first_user_message_on_resume(
    tmp_path: Path,
) -> None:
    # On resume the task must NOT be re-folded (the rollout already carries it).
    task = "RENDER_BODY_SENTINEL_9f3a\n\nresume must not re-deliver this"
    inv = _inv_with_task(tmp_path, task_text=task, balance_on_resume=True)
    built = CodexAgentRunner._build_prompt(None, inv)
    assert built.startswith("ROLE_HEADER_SENTINEL")   # role header + policy still built
    assert "tool_search" in built
    assert "RENDER_BODY_SENTINEL_9f3a" not in built    # task NOT re-folded on resume


def test_build_prompt_folds_subagent_task_with_deliverable_policy(
    tmp_path: Path,
) -> None:
    # A subagent task is folded the same way, with the deliverable-note policy branch.
    inv = _inv_with_task(
        tmp_path, task_text="SUBAGENT_BODY_SENTINEL", subagent_depth=1)
    built = CodexAgentRunner._build_prompt(None, inv)
    assert "SUBAGENT_BODY_SENTINEL" in built           # subagent task folded in
    assert "your entire deliverable" in built          # deliverable-note branch
    assert "complete_step" not in built                # completion line absent for subagents



