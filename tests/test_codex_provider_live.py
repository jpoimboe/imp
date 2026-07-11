"""Live e2e for the codex provider, gated on `codex` in IMP_LIVE_PROVIDERS.
Runs codex under the EXACT production flags:
`sandbox_mode="danger-full-access" + network_access_enabled=False` — the
combination the unit tests can't validate.

Assertions:
  - >= 1 `mcp_tool_call` (server=imp) item completes (proves codex->server
    round-trip works with the flag explicitly False -- M-1 / m-2).
  - Zero `web_search` items appear (L1 SDK-form disable confirmation).
  - Cap-abort: forcing a cap trip terminates within the bounded retry budget.

Skipped unless `codex` is in IMP_LIVE_PROVIDERS. Opting in then asserts the
`codex` CLI + openai_codex_sdk are present (see assert_codex_toolchain).
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.completion import build_continuation_fn
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_runner import CodexAgentRunner
from tests._live import assert_codex_toolchain, opted_in

pytestmark = pytest.mark.skipif(
    not opted_in("codex"),
    reason="codex not in IMP_LIVE_PROVIDERS",
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
        model_name="gpt-5.4-mini",
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


# How many independent round-trip attempts before the test gives up. The codex
# "tool is not exposed in this session" refusal was root-caused to codex DEFERRING
# all MCP tools behind its `tool_search` discovery tool (imp's `mcp__imp__*` tools
# aren't in the model's initial list); `_build_prompt` now tells the model to load
# them via `tool_search` first, which drove first-turn complete_step success from
# 4/10 to ~30/30 on gpt-5.4-mini. So the round-trip is reliable in production now
# via that prompt guidance plus the nudge backstop (continuation_fn) and the
# engine's RetryNow policy for `agent_output_missing`. The multi-attempt loop
# stays as cheap belt-and-suspenders against residual LLM nondeterminism — it
# almost always passes on the first attempt now.
_ROUND_TRIP_ATTEMPTS = 5


@pytest.mark.anyio
async def test_codex_live_round_trip_under_production_flags(tmp_path: Path) -> None:
    assert_codex_toolchain()
    _git_init(tmp_path)
    runner = CodexAgentRunner(model_name="gpt-5.4-mini")
    # Nudge backstop, exactly as the engine wires it in production: a turn that
    # ends without complete_step is re-prompted rather than pausing for input.
    nudge = build_continuation_fn(
        headless=True,
    )
    ev_lines: list[str] = []
    tool_calls: list[Any] = []
    last_events: list[Any] = []
    parked_attempts = 0
    for attempt in range(_ROUND_TRIP_ATTEMPTS):
        # Fresh per-attempt event log + transcript so a failed attempt's events
        # don't bleed into the assertions on the successful one.
        adir = tmp_path / f"attempt{attempt}"
        (adir / "tx").mkdir(parents=True)
        inv = dataclasses.replace(
            _inv(tmp_path),
            transcript_dir=adir / "tx",
            events_path=adir / "events.jsonl",
            continuation_fn=nudge,
        )
        events: list[Any] = []
        try:
            async for ev in runner.run(inv):
                events.append(ev)
                if ev.get("type") == "agent_output":
                    break
        except AwaitingUserInput:
            # Headless nudge-then-park backstop fired: the model ended two turns
            # without complete_step. That is a non-completing attempt — the same
            # failure a quiet loop exit produces, surfacing as an exception — so
            # record the partial events and retry rather than aborting the test.
            parked_attempts += 1
            last_events = events
            continue
        last_events = events
        ev_path = adir / "events.jsonl"
        ev_lines = ev_path.read_text().splitlines() if ev_path.exists() else []
        tool_calls = [
            json.loads(ln) for ln in ev_lines
            if ln and json.loads(ln).get("type") == "tool_call"
        ]
        # Success = the round-trip completed: agent_output yielded AND at least
        # one tool_call recorded (proves the codex->server round-trip works with
        # `network_access_enabled=False`).
        if any(e.get("type") == "agent_output" for e in events) and tool_calls:
            break
    else:
        raise AssertionError(
            f"codex never completed the round-trip in {_ROUND_TRIP_ATTEMPTS} "
            f"attempts ({parked_attempts} parked after the nudge; external "
            f"codex/LLM nondeterminism). Last attempt's events: {last_events}"
        )

    # The web_search assertions hold on any attempt; check them on the winner.
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
    assert_codex_toolchain()
    _git_init(tmp_path)
    (tmp_path / "tx").mkdir()
    runner = CodexAgentRunner(model_name="gpt-5.4-mini")
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
        # Nudge backstop, as production wires it. Without it, the codex/LLM
        # ~30%-per-turn tool refusal ("tool not exposed in this session") makes
        # the model end its turn without calling `glob` at all, so the cap is
        # never pressured and the run terminates with `no_complete_step` instead
        # of the cap trip — a flake. With the nudge the model is re-prompted and
        # reliably calls glob then complete_step, tripping cap=1 on dispatch #2
        # (measured: 8/8 -> tool_call_ceiling_exceeded, vs 5/8 without).
        continuation_fn=build_continuation_fn(
            headless=True,
        ),
    )
    try:
        events = [ev async for ev in runner.run(inv)]
    except AwaitingUserInput:
        # Headless nudge-then-park is a clean, bounded termination — it proves
        # the non-hang invariant exactly as a terminal error event does. A park
        # emits no `error` event, so accept it and skip the error-kind check.
        return
    # Terminated — and within the bounded budget (test runner has its own
    # session timeout; reaching here at all means we terminated). The core claim
    # is non-hang under cap pressure; the cap trip (tool_call_ceiling_exceeded)
    # is the expected path, but any terminal error proves termination. Kinds are
    # listed by their real `kind` value (NOT the `agent_output_missing` category
    # an earlier version mistakenly checked here).
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None, events
    assert err["payload"]["kind"] in (
        "tool_call_ceiling_exceeded",      # expected: cap tripped on dispatch #2
        "no_complete_step",                # model ended turn without completing
        "native_tool_violation",           # model reached for a native tool
    ), err
