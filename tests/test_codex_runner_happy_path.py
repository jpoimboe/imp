from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch as _patch

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.state import InMemoryEventLog
from imp.engine.types import (
    AgentDef,
    FsRule,
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
    # An initial commit so `git stash create` (the snapshot substrate) has a
    # HEAD to work against.
    (p / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(p), "add", ".gitkeep"], check=True)
    subprocess.run(
        ["git", "-C", str(p), "commit", "-q", "-m", "init"], check=True)


def _seed_git_and_auth(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t",
        description="",
        tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC,
        ),
        prompt_template_source="",
        source_path=Path("a.md"),
        source=_SRC,
        tier=None,
    )


def _build_invocation(
    tmp_path: Path, *, event_log: object = None,
) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="you are a test agent",
        initial_messages=(),
        change_context=ChangeContext(
            slug="s", title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        fs_permissions=(
            FsRule(operations=("read", "write"), paths=("**",),
                   mode="allow", source=_SRC),
        ),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
        event_log=event_log,
    )


@pytest.mark.anyio
async def test_run_yields_agent_output_when_complete_step_captured(
    tmp_path: Path,
) -> None:
    _seed_git_and_auth(tmp_path)
    canonical = tmp_path / "auth.json"
    elog = InMemoryEventLog()

    scripted = [[
        thread_started("thr-1"),
        agent_message("ok, calling complete_step"),
        mcp_call(
            name="complete_step",
            args={"answer": "42"},
            result="step complete",
            item_id="cs-1",
        ),
        turn_completed(input_tokens=100, output_tokens=50),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = canonical  # test seam

    inv = _build_invocation(tmp_path, event_log=elog)

    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    kinds = [e.get("type") for e in events]
    assert "agent_output" in kinds, events
    out = next(e for e in events if e.get("type") == "agent_output")
    assert out["payload"] == {"answer": "42"}


@pytest.mark.anyio
async def test_drive_turns_sweeps_bg_per_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-API-site analog: one sweep per streamed SDK event, so a
    backgrounded flooder is cap-checked across turns with no imp dispatch."""
    import imp.agents.tools.bg as bg_mod

    calls = {"n": 0}
    real = bg_mod._sweep_bg

    async def counting(ctx):
        calls["n"] += 1
        await real(ctx)

    monkeypatch.setattr(bg_mod, "_sweep_bg", counting)
    _seed_git_and_auth(tmp_path)
    canonical = tmp_path / "auth.json"
    elog = InMemoryEventLog()

    scripted = [[
        thread_started("thr-1"),
        agent_message("ok, calling complete_step"),
        mcp_call(
            name="complete_step",
            args={"answer": "42"},
            result="step complete",
            item_id="cs-1",
        ),
        turn_completed(input_tokens=100, output_tokens=50),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = canonical  # test seam

    inv = _build_invocation(tmp_path, event_log=elog)

    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_output"
    assert calls["n"] >= 2


@pytest.mark.anyio
async def test_run_fails_fast_with_network_error_when_self_ping_fails(
    tmp_path: Path,
) -> None:
    """Spec run() flow step 2: imp pings its own MCP endpoint and fails fast
    with a `network` ProviderError when unreachable, BEFORE provisioning
    CODEX_HOME or starting any SDK turn."""
    _seed_git_and_auth(tmp_path)

    fake = FakeCodex(scripted_turns=[[]])  # won't get touched
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: fake,
    )
    runner._canonical_auth_override = tmp_path / "auth.json"
    inv = _build_invocation(tmp_path)

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("connection refused (synthetic)")

    with _patch("imp.providers.codex_runner._ping_mcp_server", new=_boom):
        events = []
        async for ev in runner.run(inv):
            events.append(ev)

    assert events, "expected at least one yielded event"
    err = events[-1]
    assert err["type"] == "error"
    assert err["payload"]["category"] == "network"
    assert "ping failed" in err["payload"]["message"]
    # Codex was never started (fail-fast before SDK turn).
    assert fake.start_thread_calls == []
