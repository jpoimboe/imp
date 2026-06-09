# tests/test_subagents.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.runner import (
    ChangeContext,
)
from imp.agents.subagents import (
    TASK_TOOL_SCHEMA,
    TaskMustBeAwaited,
    dispatch_task,
    make_task_tool,
)
from imp.agents.tools.context import ToolContext
from imp.cli.signals import InterruptCoordinator
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


def _agent(name: str) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi {{ change.slug }}",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _agent2(name: str, *, max_tool_calls=None, subagents=(),
            fields=None) -> AgentDef:
    if fields is None:
        fields = (OutputField(name="status", type="enum",
                              values=("Success", "Error"), required=True),)
    return AgentDef(
        name=name, description="", tools=frozenset({"complete_step"}),
        subagents=tuple(subagents),
        output_schema=OutputSchema(fields=fields,
                                   source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi {{ change.slug }}",
        source_path=Path("a.md"), source=SourceLoc(file=Path("a.md"), line=1),
        max_tool_calls=max_tool_calls)


class _FakeRunner:
    """Yields a fixed event list; optionally runs a side effect first and/or
    captures the invocation it was driven with."""
    def __init__(self, events, *, on_run=None, capture=None):
        self._events = events
        self._on_run = on_run
        self._capture = capture
    async def run(self, inv):
        if self._capture is not None:
            self._capture["inv"] = inv
        if self._on_run is not None:
            self._on_run()
        for e in self._events:
            yield e
    async def interrupt(self) -> None: ...


def _spawn_yielding(events, *, on_run=None, capture=None):
    async def _spawn(_inv):
        return _FakeRunner(events, on_run=on_run, capture=capture)
    return _spawn


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _dispatch_ctx(tmp_path: Path) -> ToolContext:
    """Minimal ToolContext that lets dispatch_task reach drive_invocation.
    Mirrors the existing dispatch tests above (spawn_subagent + a 'reviewer'
    def + coordinator + change_context). drive_invocation is monkeypatched by
    the caller, so the spawned runner's events are never consumed."""
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}])
    ctx.change_context = ChangeContext(
        slug="change-42", title="Change 42", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    return ctx


def test_dispatch_task_surfaces_stalled_kind(tmp_path: Path) -> None:
    # PRODUCER: when drive_invocation returns ErrorInfo(category='config',
    # kind='stalled') (the B.2 watchdog outcome), dispatch_task must surface the
    # kind to the parent — its RuntimeError message must carry the '[stalled]'
    # marker so the provider's is_error tool_result content includes it.
    import anyio

    from imp.engine.agent_run import AgentRunResult, ErrorInfo

    ctx = _dispatch_ctx(tmp_path)

    async def _fake_drive(*_a, **_k):
        return AgentRunResult(output=None, errored=True, paused=False,
                              fatal=False, delay=None,
                              error=ErrorInfo("config", "stalled"))

    import imp.agents.subagents as sub_mod
    orig = sub_mod.drive_invocation
    sub_mod.drive_invocation = _fake_drive  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as ei:
            anyio.run(dispatch_task, ctx, "reviewer", "do x")
    finally:
        sub_mod.drive_invocation = orig  # type: ignore[assignment]
    assert "[stalled]" in str(ei.value)


def test_task_tool_invoke_raises_sentinel(tmp_path: Path) -> None:
    """`.invoke()` must never be called — runner special-cases name=='task'."""
    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    tool = make_task_tool()
    with pytest.raises(TaskMustBeAwaited):
        tool.invoke(ctx, {"subagent_name": "x", "task_description": "y"})


def test_task_tool_schema_has_required_args() -> None:
    assert "subagent_name" in TASK_TOOL_SCHEMA["properties"]
    assert "task_description" in TASK_TOOL_SCHEMA["properties"]
    assert set(TASK_TOOL_SCHEMA["required"]) == {"subagent_name", "task_description"}


@pytest.mark.anyio
async def test_dispatch_task_awaits_spawn_subagent(tmp_path: Path) -> None:
    capture: dict = {}
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success", "note": "done"}}],
        capture=capture)
    ctx.change_context = ChangeContext(
        slug="change-42", title="Change 42", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow")

    out = await dispatch_task(ctx, "reviewer", "please review")
    assert json.loads(out) == {"status": "Success", "note": "done"}
    inv = capture["inv"]
    assert inv.agent_def.name == "reviewer"
    assert inv.interactive is False
    assert inv.provider_name == "anthropic"
    assert inv.change_context.slug == "change-42"


@pytest.mark.anyio
async def test_dispatch_task_unknown_subagent_raises(tmp_path: Path) -> None:
    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.subagent_defs = {}
    ctx.spawn_subagent = lambda _inv: None  # type: ignore[assignment]
    with pytest.raises(ValueError, match="not declared"):
        await dispatch_task(ctx, "ghost", "hi")


def test_dispatch_task_renders_subagent_prompt(tmp_path):
    """Carryover important #1: subagent prompt template must be Jinja-rendered."""
    from pathlib import Path

    import anyio

    from imp.agents.runner import ChangeContext
    from imp.agents.subagents import dispatch_task
    from imp.agents.tools.context import ToolContext
    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc

    sub_def = AgentDef(
        name="sub", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(
                name="status", type="enum",
                values=("Success", "Error"), required=True,
            ),),
            source=SourceLoc(file=Path("sub.md"), line=1),
        ),
        prompt_template_source="hello {{ change.slug }}",
        source_path=Path("sub.md"),
        source=SourceLoc(file=Path("sub.md"), line=1),
    )

    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
        subagent_defs={"sub": sub_def},
        provider_name="anthropic", model_name="claude-sonnet-4-6",
        parent_transcript_dir=tmp_path / "parent",
        change_context=ChangeContext(
            slug="change-7", title="Change 7", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
    )
    ctx.coordinator = InterruptCoordinator()

    capture: dict = {}
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)

    result = anyio.run(dispatch_task, ctx, "sub", "do the thing")
    # build_agent_invocation appends the completion instruction after the rendered
    # prompt — check the rendered portion is correct.
    assert capture["inv"].system_prompt.startswith("hello change-7")
    # Sanity: dispatch_task still returns JSON-stringified agent_output.
    assert "Success" in result


@pytest.mark.anyio
async def test_dispatch_task_threads_shared_event_log_and_agent_id(tmp_path) -> None:
    from imp.agents.tools.context import ToolContext
    from imp.engine.state import InMemoryEventLog

    shared = InMemoryEventLog()
    capture: dict = {}

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
        subagent_defs={"helper": _agent("helper")},
        parent_transcript_dir=tmp_path / "steps" / "p" / "s",
    )
    ctx.event_log = shared
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)

    out = await dispatch_task(ctx, "helper", "do it", tool_call_event_id="tc-99")
    assert json.loads(out) == {"status": "Success"}
    inv = capture["inv"]
    assert inv.agent_id == "sub:tc-99"
    assert inv.event_log is shared
    assert inv.first_event_parent_id == "tc-99"


@pytest.mark.anyio
async def test_dispatch_task_uses_subagent_tier(tmp_path: Path) -> None:
    """A subagent whose AgentDef.tier is set resolves to that role's
    provider/model from ctx.resolved_tiers, NOT the parent's."""
    from imp.providers.config import ProviderModel

    capture: dict = {}

    sub = _agent("reviewer")
    sub = AgentDef(  # rebuild with tier set (AgentDef is frozen)
        name=sub.name, description=sub.description, tools=sub.tools,
        subagents=sub.subagents, output_schema=sub.output_schema,
        prompt_template_source=sub.prompt_template_source,
        source_path=sub.source_path, source=sub.source,
        tier="medium")

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"      # parent vendor
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    ctx.resolved_tiers = {"medium": ProviderModel("openai", "gpt-5.2")}

    await dispatch_task(ctx, "reviewer", "review please")
    inv = capture["inv"]
    assert inv.provider_name == "openai"     # jumped vendor, not parent's anthropic
    assert inv.model_name == "gpt-5.2"
    assert inv.resolved_tiers == ctx.resolved_tiers


@pytest.mark.anyio
async def test_dispatch_task_roleless_subagent_inherits_parent(tmp_path: Path) -> None:
    capture: dict = {}

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}  # tier None
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")

    await dispatch_task(ctx, "reviewer", "go")
    inv = capture["inv"]
    assert inv.provider_name == "anthropic"  # inherited
    assert inv.model_name == "haiku"


def test_make_task_tool_no_tiers_is_unchanged() -> None:
    """Off-path: the schema is identical to the shared TASK_TOOL_SCHEMA."""
    tool = make_task_tool()
    assert tool.input_schema is TASK_TOOL_SCHEMA
    assert "tier" not in tool.input_schema["properties"]


def test_make_task_tool_with_tiers_adds_optional_enum() -> None:
    tool = make_task_tool(("low", "medium", "high"))
    props = tool.input_schema["properties"]
    assert props["tier"]["type"] == "string"
    assert props["tier"]["enum"] == ["low", "medium", "high"]
    # optional: NOT added to required
    assert "tier" not in tool.input_schema["required"]
    # original required args preserved
    assert set(tool.input_schema["required"]) == {"subagent_name", "task_description"}
    # shared constant not mutated
    assert "tier" not in TASK_TOOL_SCHEMA["properties"]


def test_make_task_tool_openai_strict_round_trip() -> None:
    """The generated tier schema survives the OpenAI strict transform: the
    optional enum becomes nullable AND null lands in the enum (Task 5)."""
    from imp.providers._openai_schema import to_openai_strict
    tool = make_task_tool(("low", "high"))
    strict = to_openai_strict(tool.input_schema)
    tier = strict["properties"]["tier"]
    assert tier["type"] == ["string", "null"]
    assert None in tier["enum"]
    assert set(strict["required"]) == {"subagent_name", "task_description", "tier"}


@pytest.mark.anyio
async def test_dispatch_task_runtime_tier_overrides_frontmatter(tmp_path: Path) -> None:
    """A runtime tier= wins over the subagent's static tier: frontmatter."""
    from imp.providers.config import ProviderModel

    capture: dict = {}

    sub = _agent("reviewer")
    sub = AgentDef(  # rebuild with a static tier set
        name=sub.name, description=sub.description, tools=sub.tools,
        subagents=sub.subagents, output_schema=sub.output_schema,
        prompt_template_source=sub.prompt_template_source,
        source_path=sub.source_path, source=sub.source, tier="medium")

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    ctx.tiers = ("high",)
    ctx.resolved_tiers = {
        "medium": ProviderModel("openai", "gpt-5.2"),     # frontmatter target
        "high": ProviderModel("anthropic", "opus"),        # runtime tier target
    }

    await dispatch_task(ctx, "reviewer", "go", tier="high")
    inv = capture["inv"]
    assert inv.provider_name == "anthropic"   # high, not medium's openai
    assert inv.model_name == "opus"


@pytest.mark.anyio
async def test_dispatch_task_none_tier_falls_through_to_frontmatter(tmp_path: Path) -> None:
    """The model declines to pick a tier (None) -> static tier still applies."""
    from imp.providers.config import ProviderModel

    capture: dict = {}

    sub = _agent("reviewer")
    sub = AgentDef(
        name=sub.name, description=sub.description, tools=sub.tools,
        subagents=sub.subagents, output_schema=sub.output_schema,
        prompt_template_source=sub.prompt_template_source,
        source_path=sub.source_path, source=sub.source, tier="medium")

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    ctx.tiers = ("high",)
    ctx.resolved_tiers = {
        "medium": ProviderModel("openai", "gpt-5.2"),
        "high": ProviderModel("anthropic", "opus"),
    }

    await dispatch_task(ctx, "reviewer", "go", tier=None)
    inv = capture["inv"]
    assert inv.provider_name == "openai"      # fell through to frontmatter tier
    assert inv.model_name == "gpt-5.2"


@pytest.mark.anyio
async def test_dispatch_task_blank_tier_falls_through_to_frontmatter(tmp_path: Path) -> None:
    """Defensive: an empty string is treated as 'inherit' via `tier or None`."""
    from imp.providers.config import ProviderModel

    capture: dict = {}

    sub = _agent("reviewer")
    sub = AgentDef(
        name=sub.name, description=sub.description, tools=sub.tools,
        subagents=sub.subagents, output_schema=sub.output_schema,
        prompt_template_source=sub.prompt_template_source,
        source_path=sub.source_path, source=sub.source, tier="medium")

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    ctx.tiers = ("high",)
    ctx.resolved_tiers = {
        "medium": ProviderModel("openai", "gpt-5.2"),
        "high": ProviderModel("anthropic", "opus"),
    }

    await dispatch_task(ctx, "reviewer", "go", tier="")
    inv = capture["inv"]
    assert inv.provider_name == "openai"      # fell through to frontmatter role
    assert inv.model_name == "gpt-5.2"


@pytest.mark.anyio
async def test_dispatch_task_unknown_tier_raises_listing_valid(tmp_path: Path) -> None:
    from imp.providers.config import ProviderModel

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.spawn_subagent = lambda _inv: None  # type: ignore[assignment]
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    ctx.tiers = ("low", "high")
    ctx.resolved_tiers = {"high": ProviderModel("anthropic", "opus")}

    with pytest.raises(ValueError, match="low, high"):
        await dispatch_task(ctx, "reviewer", "go", tier="turbo")


@pytest.mark.anyio
async def test_dispatch_task_powerful_tier_cross_vendor(tmp_path: Path) -> None:
    """Parent on anthropic; tier='high' resolves to openai/gpt-5.2 —
    the subagent jumps vendor to the tier's resolved model."""
    from imp.providers.config import ProviderModel

    capture: dict = {}

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}   # no static tier
    ctx.provider_name = "anthropic"                         # parent vendor
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    ctx.tiers = ("low", "medium", "high")
    ctx.resolved_tiers = {
        "low": ProviderModel("anthropic", "claude-haiku-4-5-20251001"),
        "medium": ProviderModel("anthropic", "claude-sonnet-4-6"),
        "high": ProviderModel("openai", "gpt-5.2"),
    }

    out = await dispatch_task(ctx, "reviewer", "hard problem", tier="high")
    assert json.loads(out) == {"status": "Success"}
    inv = capture["inv"]
    assert inv.provider_name == "openai"
    assert inv.model_name == "gpt-5.2"


@pytest.mark.anyio
async def test_subagent_construction_parity(tmp_path: Path) -> None:
    """Omissions #1/#2/#3 closed: a subagent invocation built by dispatch_task
    carries the completion instruction, a nudge continuation, and a resolved
    max_tool_calls — parity with a step invocation."""
    capture: dict = {}
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer", max_tool_calls=9)}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)
    ctx.change_context = ChangeContext(
        slug="c", title="C c", project_root=tmp_path,
        workflow_dir=tmp_path / "wf")

    await dispatch_task(ctx, "reviewer", "review it")
    inv = capture["inv"]
    assert "complete_step" in inv.system_prompt
    assert inv.continuation_fn is not None
    assert inv.max_tool_calls == 9


@pytest.mark.anyio
async def test_subagent_error_fails_loudly_to_parent(tmp_path: Path) -> None:
    """Omission #2 backstop: a prose-only subagent that the provider turns into
    agent_incomplete (a Pause category) fails LOUDLY to the parent (RuntimeError
    -> is_error tool_result), not a silent swallow."""
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding([{"type": "error", "payload": {
        "category": "agent_incomplete",
        "kind": "no_complete_step_after_nudges", "message": "no complete_step"}}])
    ctx.change_context = ChangeContext(slug="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")
    with pytest.raises(RuntimeError):
        await dispatch_task(ctx, "reviewer", "review it")


@pytest.mark.anyio
async def test_subagent_backs_off_on_rate_limit(tmp_path: Path, monkeypatch) -> None:
    """Omission #4 closed: a retryable error backs off (sleeps + re-spawns a
    fresh runner) instead of failing immediately."""
    import imp.engine.backoff as backoff_mod

    sleeps: list[float] = []

    async def _fake_sleep(d):
        sleeps.append(d)

    # Patch anyio.sleep inside the backoff module (where interruptible_sleep lives).
    monkeypatch.setattr(backoff_mod.anyio, "sleep", _fake_sleep)

    calls = {"n": 0}

    async def _spawn(_inv):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeRunner([{"type": "error", "payload": {
                "category": "rate_limit", "kind": "429", "message": "slow"}}])
        return _FakeRunner([{"type": "agent_output",
                             "payload": {"status": "Success"}}])

    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn
    ctx.change_context = ChangeContext(slug="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")

    out = await dispatch_task(ctx, "reviewer", "review it")
    assert json.loads(out) == {"status": "Success"}
    # interruptible_sleep drives multiple tick-sized anyio.sleep calls per backoff
    # round, so sleeps will have more than one entry. Verify at least one sleep
    # happened (non-empty) and we retried with a fresh runner.
    assert len(sleeps) >= 1 and all(s > 0 for s in sleeps)
    assert calls["n"] == 2  # fresh runner on retry


@pytest.mark.anyio
async def test_subagent_pause_raises_suspended(tmp_path: Path) -> None:
    """A coordinator pause during a subagent drive raises SubagentSuspended
    (the unwind signal), not a RuntimeError."""
    from imp.agents.subagents import SubagentSuspended

    coord = InterruptCoordinator()

    def _trip():
        coord._should_pause = True

    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = coord
    ctx.spawn_subagent = _spawn_yielding([{"type": "thinking", "payload": {}}],
                                         on_run=_trip)
    ctx.change_context = ChangeContext(slug="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")
    with pytest.raises(SubagentSuspended):
        await dispatch_task(ctx, "reviewer", "review it")


# ── §10 safety regression tests: interruptible backoff + headless ceiling ──


@pytest.mark.anyio
async def test_subagent_backoff_sleep_is_interruptible(tmp_path: Path) -> None:
    """§10 fix: a should_pause mid-backoff returns promptly (within one tick)
    instead of blocking the full delay. Mirrors test_executor_backoff.py."""
    import anyio

    from imp.agents.subagents import SubagentSuspended

    # Verify the backoff loop uses interruptible_sleep, not anyio.sleep directly.
    # We do this by confirming that a coordinator pause during a long backoff
    # raises SubagentSuspended well before the delay would expire.
    coord = InterruptCoordinator()

    calls = {"n": 0}

    async def _spawn(_inv):
        calls["n"] += 1
        if calls["n"] == 1:
            # First call: rate_limit with a very long delay.
            return _FakeRunner([{"type": "error", "payload": {
                "category": "rate_limit", "kind": "429", "message": "slow",
                "detail": {"retry_after_seconds": None}}}])
        # Second call would succeed, but coordinator should interrupt before we get here.
        return _FakeRunner([{"type": "agent_output",
                             "payload": {"status": "Success"}}])

    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = coord
    ctx.spawn_subagent = _spawn
    ctx.change_context = ChangeContext(slug="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            async def _flip() -> None:
                await anyio.sleep(0.05)
                coord._should_pause = True

            tg.start_soon(_flip)
            # A 10 s backoff must abandon well before 10 s once should_pause flips.
            with anyio.fail_after(2.0):
                with pytest.raises(SubagentSuspended):
                    await dispatch_task(ctx, "reviewer", "review it")

    await main()


@pytest.mark.anyio
async def test_subagent_headless_backoff_hits_ceiling(tmp_path: Path,
                                                      monkeypatch) -> None:
    """§12 fix: headless (non_interactive=True) subagent backoff stops at the
    ceiling and raises SubagentSuspended rather than looping forever."""
    import imp.agents.subagents as subagents_mod
    from imp.agents.subagents import SubagentSuspended

    # Monkeypatch the ceiling to a tiny value so the test is fast and deterministic.
    monkeypatch.setattr(subagents_mod, "_HEADLESS_RETRY_CEILING_SECONDS", 10.0)

    # Also patch _interruptible_sleep so we don't actually sleep.
    async def _no_sleep(delay, coordinator, *, tick=0.1):
        pass

    monkeypatch.setattr(subagents_mod, "_interruptible_sleep", _no_sleep)

    calls = {"n": 0}

    async def _spawn(_inv):
        calls["n"] += 1
        # Always return a rate_limit with delay=15 > ceiling=10.
        return _FakeRunner([{"type": "error", "payload": {
            "category": "rate_limit", "kind": "429", "message": "slow",
            "detail": {"retry_after_seconds": 15.0}}}])

    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn
    ctx.non_interactive = True  # headless
    ctx.change_context = ChangeContext(slug="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")

    with pytest.raises(SubagentSuspended, match="retry ceiling"):
        await dispatch_task(ctx, "reviewer", "review it")

    # The ceiling check fires BEFORE the sleep on the first retry, so only 1 spawn.
    assert calls["n"] == 1


# ── §10 Fix: non_interactive forwarded to child invocation ──


@pytest.mark.anyio
async def test_dispatch_task_forwards_non_interactive_true(tmp_path: Path) -> None:
    """Fix: dispatch_task must thread ctx.non_interactive into the child
    AgentInvocation so sub-subagent backoff ceilings fire correctly at
    nesting depth >1. This test fails if the forwarding is absent."""
    capture: dict = {}
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                     events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"helper": _agent("helper")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)
    ctx.change_context = ChangeContext(slug="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")
    ctx.non_interactive = True  # headless parent

    await dispatch_task(ctx, "helper", "do it")
    assert capture["inv"].non_interactive is True


@pytest.mark.anyio
async def test_dispatch_task_forwards_non_interactive_false(tmp_path: Path) -> None:
    """Symmetry: False propagates False (interactive parent -> interactive child)."""
    capture: dict = {}
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                     events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"helper": _agent("helper")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)
    ctx.change_context = ChangeContext(slug="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")
    ctx.non_interactive = False  # interactive parent

    await dispatch_task(ctx, "helper", "do it")
    assert capture["inv"].non_interactive is False


@pytest.mark.anyio
async def test_dispatch_task_threads_sandbox_policy(tmp_path: Path) -> None:
    """Security fix: dispatch_task must thread ctx.sandbox_policy into the child
    AgentInvocation so the subagent's bash tool is sandboxed. Without the fix,
    build_agent_invocation defaults sandbox_policy=None and the OS sandbox is
    silently absent for all dispatched subagents."""
    from imp.engine.sandbox import SandboxPolicy

    capture: dict = {}
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                     events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"helper": _agent("helper")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_output", "payload": {"status": "Success"}}],
        capture=capture)
    ctx.change_context = ChangeContext(slug="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")
    pol = SandboxPolicy(
        active=True, worktree=str(tmp_path), git_common_dir=str(tmp_path / ".git"),
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        tmp_host_dir=str(tmp_path / "tmp"), secret_dirs=(), secret_files=(),
        setenv=(), network=True, note="",
    )
    ctx.sandbox_policy = pol

    await dispatch_task(ctx, "helper", "do it")
    assert capture["inv"].sandbox_policy is pol
