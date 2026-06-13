"""Helpers for live elicitation and concurrency tests (IMP_RUN_LIVE_ELICIT /
IMP_RUN_CODEX / IMP_RUN_DOGFOOD gated)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from imp.agents.runner import ContentBlock
from imp.agents.subagents import TASK_TOOL_SCHEMA, make_task_tool


def _anthropic_key() -> str | None:
    # Check key file before env so the conftest "sk-test" stub doesn't shadow it.
    key_file = Path.home() / ".anthropic-key"
    if key_file.exists():
        val = key_file.read_text().strip()
        if val:
            return val
    return os.environ.get("ANTHROPIC_API_KEY") or None


def _openai_key() -> str | None:
    key_file = Path.home() / ".openai-key"
    if key_file.exists():
        val = key_file.read_text().strip()
        if val:
            return val
    return os.environ.get("OPENAI_API_KEY") or None


async def collect_first_assistant_tool_uses(
    *,
    provider: str,
    prompt: str,
) -> list[ContentBlock]:
    """Make ONE real LLM call with the task tool; return the first assistant
    message's content blocks WITHOUT dispatching the tool calls."""
    task_tool = make_task_tool()
    if provider == "anthropic":
        return await _call_anthropic(prompt, task_tool.description)
    if provider == "openai":
        return await _call_openai(prompt, task_tool.description)
    raise ValueError(f"unsupported provider: {provider!r}")


async def _call_anthropic(prompt: str, tool_description: str) -> list[ContentBlock]:
    import anthropic

    from imp.providers.anthropic import (
        _parse_anthropic_message,
        _serialize_anthropic_response,
    )

    key = _anthropic_key()
    assert key is not None, (
        "anthropic credentials missing: set ANTHROPIC_API_KEY or create ~/.anthropic-key"
    )
    client = anthropic.AsyncAnthropic(api_key=key)
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="You have two subagents named 'a' and 'b'. Use the task tool.",
            tools=[{
                "name": "task",
                "description": tool_description,
                "input_schema": TASK_TOOL_SCHEMA,
            }],
            messages=[{"role": "user", "content": prompt}],
            tool_choice={"type": "auto", "disable_parallel_tool_use": False},
        )
        raw = _serialize_anthropic_response(response)
        msg = _parse_anthropic_message(raw)
        return list(msg.content)
    finally:
        await client.close()


async def _call_openai(prompt: str, tool_description: str) -> list[ContentBlock]:
    import openai

    from imp.providers._openai_schema import to_openai_strict
    from imp.providers.openai import (
        _parse_responses_message,
        _serialize_responses_response,
    )

    key = _openai_key()
    assert key is not None, (
        "openai credentials missing: set OPENAI_API_KEY or create ~/.openai-key"
    )
    client = openai.AsyncOpenAI(api_key=key)
    try:
        response = await client.responses.create(
            model="gpt-4o-mini",
            instructions="You have two subagents named 'a' and 'b'. Use the task tool.",
            input=[{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }],
            tools=[{
                "type": "function",
                "name": "task",
                "description": tool_description,
                "parameters": to_openai_strict(TASK_TOOL_SCHEMA),
                "strict": True,
            }],
            store=False,
            parallel_tool_calls=True,
        )
        raw = _serialize_responses_response(response)
        msg = _parse_responses_message(raw)
        return list(msg.content)
    finally:
        await client.close()


def _git_init_repo(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "T"], check=True)
    (p / "README.md").write_text("# t\n")
    subprocess.run(["git", "-C", str(p), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-m", "init", "-q"], check=True)


async def run_parallel_task_turn(provider: str) -> list[dict]:
    """Drive ONE live turn through the named provider's production runner with a
    task-capable parent and two stub subagents that call complete_step immediately.

    Returns all event-log records from the run as a list of dicts. The events
    include tool_call/tool_result for both task dispatches, which the caller
    uses to assert concurrency shape (both calls precede either result).
    """
    from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
    from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc

    _SRC = SourceLoc(file=Path("a.md"), line=1)
    _FS = (FsRule(operations=("read", "write"), paths=("**",),
                  mode="allow", source=_SRC),)

    worker_schema = OutputSchema(
        fields=(OutputField(name="status", type="enum",
                            values=("Success",), required=True),),
        source=_SRC,
    )
    _worker_prompt = (
        "Call complete_step immediately with status='Success'. "
        "To finish this step you MUST call the `complete_step` tool with: "
        "`status` (Success). Do not answer in prose — call the tool."
    )
    worker_a = AgentDef(
        name="worker_a", description="trivial stub worker",
        tools=frozenset({"complete_step"}), subagents=(),
        output_schema=worker_schema,
        prompt_template_source=_worker_prompt,
        source_path=Path("a.md"), source=_SRC,
    )
    worker_b = AgentDef(
        name="worker_b", description="trivial stub worker",
        tools=frozenset({"complete_step"}), subagents=(),
        output_schema=worker_schema,
        prompt_template_source=_worker_prompt,
        source_path=Path("a.md"), source=_SRC,
    )
    parent_schema = OutputSchema(
        fields=(OutputField(name="status", type="enum",
                            values=("Success",), required=True),),
        source=_SRC,
    )
    parent_def = AgentDef(
        name="coordinator", description="dispatches two stub workers in parallel",
        tools=frozenset({"complete_step", "task"}),
        subagents=("worker_a", "worker_b"),
        output_schema=parent_schema,
        prompt_template_source="",
        source_path=Path("a.md"), source=_SRC,
    )

    model_name = "gpt-5.5" if provider == "codex" else "claude-sonnet-4-5"

    with tempfile.TemporaryDirectory() as _tmpdir:
        tmp_path = Path(_tmpdir)
        _git_init_repo(tmp_path)
        (tmp_path / "tx").mkdir()

        inv = AgentInvocation(
            agent_def=parent_def,
            provider_name=provider,
            model_name=model_name,
            system_prompt=(
                "You have two independent workers: 'worker_a' and 'worker_b'. "
                "Issue BOTH as parallel task calls in a SINGLE message right now. "
                "Ask worker_a to 'do task A' and worker_b to 'do task B'. "
                "After both complete, call complete_step with status='Success'. "
                "To finish: MUST call `complete_step` with `status` (Success)."
            ),
            initial_messages=(Message(
                role="user",
                content=(TextBlock(
                    text=(
                        "Dispatch worker_a and worker_b in parallel now, "
                        "in a single message."
                    )),),
            ),),
            change_context=ChangeContext(
                slug="s", title="parallel concurrency test",
                project_root=tmp_path, workflow_dir=tmp_path, id="c1",
            ),
            transcript_dir=tmp_path / "tx",
            fs_permissions=_FS,
            events_path=tmp_path / "events.jsonl",
            agent_id="step:coord",
            subagent_defs={"worker_a": worker_a, "worker_b": worker_b},
        )

        if provider == "codex":
            from imp.providers.codex_runner import (
                CodexAgentRunner,
            )
            runner = CodexAgentRunner(model_name=model_name)
        else:
            from imp.providers.claude_code import (
                ClaudeCodeAgentRunner,
            )
            runner = ClaudeCodeAgentRunner(model_name=model_name)

        async for _ in runner.run(inv):
            pass

        events_file = tmp_path / "events.jsonl"
        if not events_file.exists():
            return []
        events: list[dict] = []
        for line in events_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events
