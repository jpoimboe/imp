"""Live e2e for the codex provider, gated behind IMP_RUN_CODEX=1 (mirrors
IMP_RUN_DOGFOOD precedent). Runs codex under the EXACT production flags:
`sandbox_mode="danger-full-access" + network_access_enabled=False` — the
combination the unit tests can't validate.

Assertions:
  - >= 1 `mcp_tool_call` (server=imp) item completes (proves codex->server
    round-trip works with the flag explicitly False -- M-1 / m-2).
  - Zero `web_search` items appear (L1 SDK-form disable confirmation).
  - Cap-abort: forcing a cap trip terminates within the bounded retry budget.

Skip conditions:
  - IMP_RUN_CODEX != "1"
  - `codex` binary not on PATH
  - openai_codex_sdk not importable
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_runner import CodexAgentRunner

pytestmark = pytest.mark.skipif(
    os.environ.get("IMP_RUN_CODEX") != "1"
    or shutil.which("codex") is None
    or importlib.util.find_spec("openai_codex_sdk") is None,
    reason="codex live test requires IMP_RUN_CODEX=1 + codex CLI + openai_codex_sdk",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _git_init(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "T"], check=True)
    (p / "README.md").write_text("# t\n")
    subprocess.run(["git", "-C", str(p), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-m", "init", "-q"], check=True)


_SRC = SourceLoc(file=Path("a.md"), line=1)


def _inv(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=AgentDef(
            name="echoer",
            description="repeat the secret word",
            tools=frozenset({"complete_step"}),
            subagents=(),
            output_schema=OutputSchema(
                fields=(OutputField(name="word", type="string", required=True),),
                source=_SRC,
            ),
            prompt_template_source="",
            source_path=Path("a.md"),
            source=_SRC,
            tier=None,
        ),
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt=(
            'Reply by calling complete_step with word="banana". Do not call '
            "any other tools and do not write any files."
        ),
        initial_messages=(),
        change_context=ChangeContext(
            title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1",
        ),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
    )


@pytest.mark.anyio
async def test_codex_live_round_trip_under_production_flags(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "tx").mkdir()
    runner = CodexAgentRunner(model_name="gpt-5.5")
    inv = _inv(tmp_path)
    events: list[Any] = []
    async for ev in runner.run(inv):
        events.append(ev)
        if ev.get("type") == "agent_output":
            break
    # Must yield agent_output, not error.
    assert any(e.get("type") == "agent_output" for e in events), events
    # And at least one tool_call event recorded in events.jsonl — proves the
    # codex->server round-trip works with `network_access_enabled=False`.
    ev_lines = (tmp_path / "events.jsonl").read_text().splitlines()
    tool_calls = [
        json.loads(ln) for ln in ev_lines
        if ln and json.loads(ln).get("type") == "tool_call"
    ]
    assert len(tool_calls) >= 1
    warns = [
        json.loads(ln) for ln in ev_lines
        if ln and json.loads(ln).get("type") == "runner_warning"
    ]
    # No *unexpected web_search tool* tripwire (web_search is disabled via
    # config.toml for this non-declaring agent).
    assert all(
        "unexpected codex web_search" not in str(w.get("payload", {})) for w in warns
    )
    # And the codex >=0.137 `features.web_search_request` deprecation `error`
    # item must NOT appear: imp stopped emitting the deprecated SDK flag and now
    # controls web_search via config.toml's top-level key (Finding A/B fix).
    assert all(not w.get("payload", {}).get("codex_error_item") for w in warns), warns
    assert all(
        "web_search_request" not in str(w.get("payload", {})) for w in warns
    ), warns


@pytest.mark.anyio
async def test_codex_live_cap_abort_terminates_within_budget(tmp_path: Path) -> None:
    """Force a real cap trip end-to-end and assert the loop TERMINATES (does
    not hang) within the bounded retry budget.

    The dispatch cap is "max ALLOWED calls": `dispatch_count > cap` trips, so
    `cap = max_tool_calls = 1` permits exactly one tool call. A single-call
    agent (the round-trip echoer) can never trip it — its lone `complete_step`
    is that one allowed call. (Note `cap = max_tool_calls or 1000` also makes
    cap=0 unreachable.) So to exercise the ceiling we give the agent a second
    read-only tool (`glob`) and instruct it to call that FIRST, then
    `complete_step`: dispatch #1 (`glob`) is allowed, dispatch #2 (`complete_step`)
    trips the cap. The runner then aborts the SDK and yields
    `tool_call_ceiling_exceeded` on its next event iteration — independent of
    what the model does next. The mechanism itself (RMW + abort flag) is
    unit-covered in test_codex_mcp_dispatch.py; this is the live proof that a
    real codex turn terminates under cap pressure rather than looping."""
    _git_init(tmp_path)
    (tmp_path / "tx").mkdir()
    runner = CodexAgentRunner(model_name="gpt-5.5")
    inv = _inv(tmp_path)
    # Give the agent a second (read-only) tool so it can make two calls, and
    # order them so the SECOND call (complete_step) is the one that trips cap=1.
    agent_def = dataclasses.replace(
        inv.agent_def, tools=frozenset({"complete_step", "glob"})
    )
    inv = dataclasses.replace(
        inv,
        agent_def=agent_def,
        max_tool_calls=1,
        system_prompt=(
            "First call the `glob` tool with pattern \"*\". After it returns, call "
            'complete_step with word="banana". Call `glob` before complete_step. '
            "Do not call any other tools and do not write any files."
        ),
    )
    events = [ev async for ev in runner.run(inv)]
    # Terminated — and within the bounded budget (test runner has its own
    # session timeout; reaching here at all means we terminated).
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None, events
    assert err["payload"]["kind"] in (
        "tool_call_ceiling_exceeded", "agent_output_missing", "native_tool_violation",
    ), err
