# tests/test_subagents.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.reflow import reflow_markdown
from imp.agents.runner import (
    QuestContext,
)
from imp.agents.subagents import (
    TASK_TOOL_SCHEMA,
    TaskMustBeAwaited,
    dispatch_task,
    make_task_tool,
)
from imp.agents.tools.context import ToolContext
from imp.cli.signals import InterruptCoordinator
from imp.engine.agent_run import _CORE_HEAD, SUBAGENT_DELIVERABLE_NOTE
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir


def _agent(name: str) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi {{ quest.id }}",
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
        prompt_template_source="hi {{ quest.id }}",
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
    def + coordinator + quest_context). drive_invocation is monkeypatched by
    the caller, so the spawned runner's events are never consumed."""
    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}])
    ctx.quest_context = QuestContext(
        id="change-42", title="Quest 42", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow")
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


def test_dispatch_task_suspend_handback_on_budget_ceiling(tmp_path: Path) -> None:
    # PRODUCER: when the retry-budget ceiling is hit during subagent backoff and no
    # human pause is pending (coordinator.should_pause is False), dispatch_task hands
    # the child back as a RuntimeError whose body carries the literal [suspended]
    # marker and the transcript pointer — it no longer raises SubagentSuspended.
    import anyio

    from imp.engine.agent_run import AgentRunResult, ErrorInfo

    ctx = _dispatch_ctx(tmp_path)
    assert ctx.coordinator.should_pause is False

    async def _fake_drive(*_a, **_k):
        # delay far exceeds budget_for("auth") (1800s) -> ceiling on the first pass.
        return AgentRunResult(output=None, errored=True, paused=False,
                              fatal=False, delay=1e9,
                              error=ErrorInfo("auth", None))

    import imp.agents.subagents as sub_mod
    orig = sub_mod.drive_invocation
    sub_mod.drive_invocation = _fake_drive  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as ei:
            anyio.run(dispatch_task, ctx, "reviewer", "do x")
    finally:
        sub_mod.drive_invocation = orig  # type: ignore[assignment]
    body = str(ei.value)
    assert "[suspended]" in body
    assert "[interrupted]" not in body
    assert "transcript.jsonl" in body


def test_dispatch_task_interrupt_handback_on_pause(tmp_path: Path) -> None:
    # PRODUCER: with a human pause pending (coordinator.should_pause True) and the
    # child's drive returning paused, dispatch_task hands back a RuntimeError whose
    # body carries the [interrupted] marker and the transcript pointer.
    import anyio

    from imp.engine.agent_run import AgentRunResult

    ctx = _dispatch_ctx(tmp_path)
    ctx.coordinator._should_pause = True

    async def _fake_drive(*_a, **_k):
        return AgentRunResult(output=None, errored=False, paused=True,
                              fatal=False, delay=None, error=None)

    import imp.agents.subagents as sub_mod
    orig = sub_mod.drive_invocation
    sub_mod.drive_invocation = _fake_drive  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as ei:
            anyio.run(dispatch_task, ctx, "reviewer", "do x")
    finally:
        sub_mod.drive_invocation = orig  # type: ignore[assignment]
    body = str(ei.value)
    assert "[interrupted]" in body
    assert "[suspended]" not in body
    assert "transcript.jsonl" in body


def test_task_tool_invoke_raises_sentinel(tmp_path: Path) -> None:
    """`.invoke()` must never be called — runner special-cases name=='task'."""
    ctx = ToolContext(
        project_root=tmp_path,
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
    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(
        id="change-42", title="Quest 42", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow")

    out = await dispatch_task(ctx, "reviewer", "please review")
    assert out == "ok"
    inv = capture["inv"]
    assert inv.agent_def.name == "reviewer"
    assert inv.user_input_fn is None  # subagents are headless
    assert inv.provider_name == "anthropic"
    assert inv.quest_context.id == "change-42"


@pytest.mark.anyio
async def test_dispatch_task_unknown_subagent_raises(tmp_path: Path) -> None:
    ctx = ToolContext(
        project_root=tmp_path,
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

    from imp.agents.runner import QuestContext
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
        prompt_template_source="hello {{ quest.id }}",
        source_path=Path("sub.md"),
        source=SourceLoc(file=Path("sub.md"), line=1),
    )

    ctx = ToolContext(
        project_root=tmp_path,
events_path=tmp_path / "events.jsonl",
        subagent_defs={"sub": sub_def},
        provider_name="anthropic", model_name="claude-sonnet-4-6",
        parent_transcript_dir=tmp_path / "parent",
        quest_context=QuestContext(
            id="change-7", title="Quest 7", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow",
        ),
    )
    ctx.coordinator = InterruptCoordinator()

    capture: dict = {}
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)

    result = anyio.run(dispatch_task, ctx, "sub", "do the thing")
    # build_agent_invocation appends the deliverable note after the rendered
    # prompt — check the rendered portion is correct.
    assert capture["inv"].system_prompt.startswith(reflow_markdown(_CORE_HEAD))
    assert "hello change-7" in capture["inv"].system_prompt
    # Sanity: dispatch_task returns the raw text from agent_result.
    assert result == "ok"


@pytest.mark.anyio
async def test_dispatch_task_threads_shared_event_log_and_agent_id(tmp_path) -> None:
    from imp.agents.tools.context import ToolContext
    from imp.engine.state import InMemoryEventLog

    shared = InMemoryEventLog()
    capture: dict = {}

    ctx = ToolContext(
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl",
        subagent_defs={"helper": _agent("helper")},
        parent_transcript_dir=tmp_path / "steps" / "p" / "s",
    )
    ctx.event_log = shared
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)

    out = await dispatch_task(ctx, "helper", "do it", tool_call_event_id="tc-99")
    assert out == "ok"
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
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"      # parent vendor
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(
        id="test", title="Test", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow")
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
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}  # tier None
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(
        id="test", title="Test", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow")

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


@pytest.mark.timeout(240)  # wall-clock SIGALRM can false-fire under -n auto saturation
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
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(
        id="test", title="Test", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow")
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
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(
        id="test", title="Test", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow")
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
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(
        id="test", title="Test", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow")
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
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.spawn_subagent = lambda _inv: None  # type: ignore[assignment]
    ctx.quest_context = QuestContext(
        id="test", title="Test", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow")
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
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}   # no static tier
    ctx.provider_name = "anthropic"                         # parent vendor
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(
        id="test", title="Test", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow")
    ctx.tiers = ("low", "medium", "high")
    ctx.resolved_tiers = {
        "low": ProviderModel("anthropic", "claude-haiku-4-5-20251001"),
        "medium": ProviderModel("anthropic", "claude-sonnet-4-6"),
        "high": ProviderModel("openai", "gpt-5.2"),
    }

    out = await dispatch_task(ctx, "reviewer", "hard problem", tier="high")
    assert out == "ok"
    inv = capture["inv"]
    assert inv.provider_name == "openai"
    assert inv.model_name == "gpt-5.2"


@pytest.mark.anyio
async def test_subagent_construction_parity(tmp_path: Path) -> None:
    """Omissions #1/#2/#3 closed: a subagent invocation built by dispatch_task
    carries the deliverable note (no completion instruction), a nudge
    continuation, and a resolved ceiling — parity with a step invocation."""
    capture: dict = {}
    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer", max_tool_calls=9)}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(
        id="c", title="C c", project_root=tmp_path,
        workflow_dir=tmp_path / "wf")

    await dispatch_task(ctx, "reviewer", "review it")
    inv = capture["inv"]
    assert SUBAGENT_DELIVERABLE_NOTE in inv.system_prompt
    assert "complete_step" not in inv.system_prompt
    assert inv.continuation_fn is not None
    assert inv.max_tool_calls == 9


@pytest.mark.anyio
async def test_subagent_error_fails_loudly_to_parent(tmp_path: Path) -> None:
    """A subagent whose provider surfaces a fatal error fails LOUDLY to the
    parent (RuntimeError -> is_error tool_result), not a silent swallow."""
    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding([{"type": "error", "payload": {
        "category": "api",
        "kind": "api_error", "message": "fatal provider error"}}])
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
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
        return _FakeRunner([{"type": "agent_result",
                             "payload": {"text": "ok"}}])

    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")

    out = await dispatch_task(ctx, "reviewer", "review it")
    assert out == "ok"
    # interruptible_sleep drives multiple tick-sized anyio.sleep calls per backoff
    # round, so sleeps will have more than one entry. Verify at least one sleep
    # happened (non-empty) and we retried with a fresh runner.
    assert len(sleeps) >= 1 and all(s > 0 for s in sleeps)
    assert calls["n"] == 2  # fresh runner on retry


@pytest.mark.anyio
async def test_subagent_pause_raises_suspended(tmp_path: Path) -> None:
    """A coordinator pause during a subagent drive hands the child back as a
    RuntimeError carrying the [interrupted] marker (should_pause is set), not a
    SubagentSuspended unwind."""
    coord = InterruptCoordinator()

    def _trip():
        coord._should_pause = True

    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = coord
    ctx.spawn_subagent = _spawn_yielding([{"type": "thinking", "payload": {}}],
                                         on_run=_trip)
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")
    with pytest.raises(RuntimeError, match=r"\[interrupted\]"):
        await dispatch_task(ctx, "reviewer", "review it")


# ── §10 safety regression tests: interruptible backoff + headless ceiling ──


@pytest.mark.anyio
async def test_subagent_backoff_sleep_is_interruptible(tmp_path: Path) -> None:
    """§10 fix: a should_pause mid-backoff returns promptly (within one tick)
    instead of blocking the full delay. Mirrors test_executor_backoff.py."""
    import anyio

    # Verify the backoff loop uses interruptible_sleep, not anyio.sleep directly.
    # We do this by confirming that a coordinator pause during a long backoff
    # hands the child back (a RuntimeError carrying [interrupted]) well before the
    # delay would expire.
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
        return _FakeRunner([{"type": "agent_result",
                             "payload": {"text": "ok"}}])

    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = coord
    ctx.spawn_subagent = _spawn
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            async def _flip() -> None:
                await anyio.sleep(0.05)
                coord._should_pause = True

            tg.start_soon(_flip)
            # A 10 s backoff must abandon well before 10 s once should_pause flips.
            with anyio.fail_after(30):  # deadlock backstop only
                with pytest.raises(RuntimeError, match=r"\[interrupted\]"):
                    await dispatch_task(ctx, "reviewer", "review it")

    await main()


@pytest.mark.anyio
async def test_subagent_backoff_hits_per_category_budget(tmp_path: Path,
                                                         monkeypatch) -> None:
    """Per-category budget: subagent backoff stops at the budget and hands the
    child back as a RuntimeError ([suspended]) — in interactive runs too
    (mode-independent)."""
    import imp.agents.subagents as subagents_mod
    import imp.engine.error_policy as ep_mod

    # dispatch_task lazily does `from imp.engine.error_policy import budget_for`,
    # which reads the attribute off the module at call time, so patching it here
    # is seen by the loop. Tiny budget so delay=15 exceeds it on attempt 1.
    monkeypatch.setattr(ep_mod, "budget_for", lambda category: 10.0)

    async def _no_sleep(delay, coordinator, *, tick=0.1):
        pass

    monkeypatch.setattr(subagents_mod, "_interruptible_sleep", _no_sleep)

    calls = {"n": 0}

    async def _spawn(_inv):
        calls["n"] += 1
        # rate_limit error carrying a 15s server hint: decide() yields
        # delay = min(60, max(2^0, 15)) = 15.0 > budget=10 -> suspend on attempt 1.
        return _FakeRunner([{"type": "error", "payload": {
            "category": "rate_limit", "kind": "429", "message": "slow",
            "detail": {"retry_after_seconds": 15.0}}}])

    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn
    ctx.non_interactive = False  # interactive: the budget still fires
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")

    with pytest.raises(RuntimeError, match=r"\[suspended\]"):
        await dispatch_task(ctx, "reviewer", "review it")

    # The budget check fires BEFORE the sleep on the first retry, so only 1 spawn.
    assert calls["n"] == 1


# ── §10 Fix: non_interactive forwarded to child invocation ──


@pytest.mark.anyio
async def test_dispatch_task_forwards_non_interactive_true(tmp_path: Path) -> None:
    """Fix: dispatch_task must thread ctx.non_interactive into the child
    AgentInvocation so sub-subagent backoff ceilings fire correctly at
    nesting depth >1. This test fails if the forwarding is absent."""
    capture: dict = {}
    ctx = ToolContext(project_root=tmp_path,
                     events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"helper": _agent("helper")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")
    ctx.non_interactive = True  # headless parent

    await dispatch_task(ctx, "helper", "do it")
    assert capture["inv"].headless is True


@pytest.mark.anyio
async def test_dispatch_task_forwards_non_interactive_false(tmp_path: Path) -> None:
    """Symmetry: False propagates False (interactive parent -> interactive child)."""
    capture: dict = {}
    ctx = ToolContext(project_root=tmp_path,
                     events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"helper": _agent("helper")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")
    ctx.non_interactive = False  # interactive parent

    await dispatch_task(ctx, "helper", "do it")
    assert capture["inv"].headless is False


@pytest.mark.anyio
async def test_dispatch_task_threads_sandbox_policy(tmp_path: Path) -> None:
    """Security fix: dispatch_task must thread ctx.sandbox_policy into the child
    AgentInvocation so the subagent's bash tool is sandboxed. Without the fix,
    build_agent_invocation defaults sandbox_policy=None and the OS sandbox is
    silently absent for all dispatched subagents."""
    from imp.engine.sandbox import SandboxPolicy

    capture: dict = {}
    ctx = ToolContext(project_root=tmp_path,
                     events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"helper": _agent("helper")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")
    pol = SandboxPolicy(
        active=True, worktree=str(tmp_path), git_common_dir=str(tmp_path / ".git"),
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(),
        setenv=(), network=True, note="",
    )
    ctx.base_sandbox_policy = pol

    await dispatch_task(ctx, "helper", "do it")
    assert capture["inv"].sandbox_policy is pol


@pytest.mark.anyio
async def test_dispatch_task_inherits_readonly_overlay(tmp_path: Path) -> None:
    """A subagent inherits the parent step's read_only overlay automatically:
    the overlay is folded into sandbox_policy, which dispatch threads verbatim."""
    from imp.engine.sandbox import SandboxPolicy

    capture: dict = {}
    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"helper": _agent("helper")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        capture=capture)
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")
    ctx.base_sandbox_policy = SandboxPolicy(
        active=True, worktree=str(tmp_path), git_common_dir=str(tmp_path / ".git"),
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(), setenv=(), network=True, note="",
        readonly_overlays=(str(tmp_path),),
    )

    await dispatch_task(ctx, "helper", "do it")
    assert capture["inv"].sandbox_policy.readonly_overlays == (str(tmp_path),)


@pytest.mark.anyio
async def test_dispatch_task_holds_limiter_during_drive(tmp_path: Path) -> None:
    """The per-invocation CapacityLimiter is acquired AROUND the drive loop:
    exactly one token is borrowed while the subagent runs, and it is released
    when dispatch_task returns."""
    from imp.agents.parallel import reset_parallelism_cache

    reset_parallelism_cache()
    ctx = _dispatch_ctx(tmp_path)
    seen: dict = {}

    def _capture_borrowed() -> None:
        # Runs inside the spawned runner's body — i.e. inside the limiter scope.
        seen["borrowed"] = ctx.parallel_limiter.borrowed_tokens

    ctx.spawn_subagent = _spawn_yielding(
        [{"type": "agent_result", "payload": {"text": "ok"}}],
        on_run=_capture_borrowed)

    out = await dispatch_task(ctx, "reviewer", "go")
    assert out == "ok"
    assert seen["borrowed"] == 1                     # held during the drive
    assert ctx.parallel_limiter.borrowed_tokens == 0  # released afterwards
    reset_parallelism_cache()


def test_make_task_tool_description_has_parallel_guidance() -> None:
    """§4/AC35: the single task description carries the parallel-dispatch
    guidance every provider advertises (concurrency for independent subtasks,
    sequencing for dependent ones, unspecified task-vs-non-task ordering)."""
    desc = make_task_tool().description
    # Still names what the tool does.
    assert "subagent" in desc
    # Independent subtasks in one message run concurrently.
    assert "concurrently" in desc
    # Dependent subtasks sequence across turns.
    assert "sequentially across turns" in desc
    # task-vs-non-task ordering within one message is not guaranteed.
    assert "unspecified" in desc
    # The tier-bearing variant shares the same description.
    assert make_task_tool(("low", "high")).description == desc


@pytest.mark.anyio
async def test_subagent_auth_budget_hands_back_suspended(tmp_path: Path,
                                                         monkeypatch) -> None:
    """An auth-category subagent budget exhaustion routes through the same
    parent-handback seam as any other category: a RuntimeError carrying the
    generic [suspended] body (no category-specific messaging)."""
    import imp.agents.subagents as subagents_mod
    import imp.engine.error_policy as ep_mod

    monkeypatch.setattr(ep_mod, "budget_for", lambda category: 10.0)

    async def _no_sleep(delay, coordinator, *, tick=0.1):
        pass

    monkeypatch.setattr(subagents_mod, "_interruptible_sleep", _no_sleep)

    calls = {"n": 0}

    async def _spawn(_inv):
        calls["n"] += 1
        # auth error carrying a 15s server hint: decide() yields
        # delay = min(60, max(2^0, 15)) = 15.0 > budget=10 -> suspend on attempt 1.
        return _FakeRunner([{"type": "error", "payload": {
            "category": "auth", "kind": "http_401", "message": "401",
            "detail": {"retry_after_seconds": 15.0}}}])

    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn
    ctx.non_interactive = False
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")

    with pytest.raises(RuntimeError, match=r"\[suspended\]"):
        await dispatch_task(ctx, "reviewer", "review it")
    assert calls["n"] == 1


def test_dispatch_task_signature_has_no_abort_dispatch() -> None:
    import inspect

    import imp.agents.subagents as sub

    params = inspect.signature(sub.dispatch_task).parameters
    assert "abort_dispatch" not in params
    assert not hasattr(sub, "TaskDispatchSkipped")
    assert not hasattr(sub, "TASK_REFUSAL_SENTINEL")
    assert not hasattr(sub, "TaskRefused")


@pytest.mark.anyio
async def test_subagent_hinted_delay_is_budget_exempt(tmp_path: Path,
                                                       monkeypatch) -> None:
    """A hinted reset-wait is exempt from the subagent budget: three 7200s hints
    (each > the 1800s rate_limit budget) all ride, and the 4th drive succeeds."""
    import imp.agents.subagents as subagents_mod
    from imp.engine.state import InMemoryEventLog

    async def _no_sleep(delay, coordinator, *, tick=0.1):
        pass
    monkeypatch.setattr(subagents_mod, "_interruptible_sleep", _no_sleep)

    calls = {"n": 0}

    async def _spawn(_inv):
        i = calls["n"]
        calls["n"] += 1
        if i < 3:
            return _FakeRunner([{"type": "error", "payload": {
                "category": "rate_limit", "kind": "429", "message": "slow",
                "detail": {"retry_after_seconds": 7200.0}}}])
        return _FakeRunner([{"type": "agent_result", "payload": {"text": "ok"}}])

    log = InMemoryEventLog()
    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn
    ctx.event_log = log
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")

    out = await dispatch_task(ctx, "reviewer", "review it")
    assert out == "ok"
    assert calls["n"] == 4
    retries = [e for e in log.read() if e["type"] == "agent_retry"]
    assert len(retries) == 3
    assert all(e["payload"]["hinted"] is True for e in retries)
    assert all(e["payload"]["backoff_seconds"] == 7200.0 for e in retries)


@pytest.mark.anyio
async def test_subagent_fourth_hinted_sleep_is_budgeted(tmp_path: Path,
                                                         monkeypatch) -> None:
    """The 4th hinted subagent sleep falls back to budgeted (cap=3): a 7200s hint
    exceeds the 1800s budget, so the loop suspends after exactly 3 hinted retries."""
    import imp.agents.subagents as subagents_mod
    from imp.engine.state import InMemoryEventLog

    async def _no_sleep(delay, coordinator, *, tick=0.1):
        pass
    monkeypatch.setattr(subagents_mod, "_interruptible_sleep", _no_sleep)

    calls = {"n": 0}

    async def _spawn(_inv):
        calls["n"] += 1
        return _FakeRunner([{"type": "error", "payload": {
            "category": "rate_limit", "kind": "429", "message": "slow",
            "detail": {"retry_after_seconds": 7200.0}}}])

    log = InMemoryEventLog()
    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent2("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "m"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.spawn_subagent = _spawn
    ctx.event_log = log
    ctx.quest_context = QuestContext(id="c", title="C", project_root=tmp_path,
                                       workflow_dir=tmp_path / "wf")

    with pytest.raises(RuntimeError, match=r"\[suspended\]"):
        await dispatch_task(ctx, "reviewer", "review it")
    assert calls["n"] == 4
    retries = [e for e in log.read() if e["type"] == "agent_retry"]
    assert len(retries) == 3
    assert all(e["payload"]["hinted"] is True for e in retries)


def test_task_tool_tier_description_omit_uses_default_not_inherit() -> None:
    """The tier description tells the model that omitting resolves to the
    subagent's own default tier — NOT the caller's model."""
    tool = make_task_tool(("low", "medium", "high"))
    desc = tool.input_schema["properties"]["tier"]["description"]
    assert "Omit to use the subagent's own default tier" in desc
    assert "deliberately override it" in desc
    # The factually-wrong clause is gone.
    assert "inherit the caller's model" not in desc
    assert "Pick by subtask difficulty" not in desc


def test_dispatch_task_hands_back_handoff_note_inline(tmp_path: Path) -> None:
    # Tier A: subagent wrote a handoff as it neared its window. dispatch_task must
    # NOT reincarnate — it hands the note back to the parent as an is_error
    # tool_result inlining the note text, a transcript pointer, the guidance
    # footer, and the [context_exhausted] marker.
    import anyio

    from imp.engine.agent_run import AgentRunResult

    ctx = _dispatch_ctx(tmp_path)

    note_path = tmp_path / "handoff.md"
    note_path.write_text("# handoff\nExplored module X; next wire up Y.\n")

    spawns = {"n": 0}
    orig_spawn = ctx.spawn_subagent
    async def _counting_spawn(inv):
        spawns["n"] += 1
        return await orig_spawn(inv)
    ctx.spawn_subagent = _counting_spawn

    async def _fake_drive(*_a, **_k):
        return AgentRunResult(output=None, errored=False, paused=False,
                              fatal=False, delay=None,
                              handoff_path=str(note_path))

    import imp.agents.subagents as sub_mod
    orig = sub_mod.drive_invocation
    sub_mod.drive_invocation = _fake_drive  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as ei:
            anyio.run(dispatch_task, ctx, "reviewer", "do x")
    finally:
        sub_mod.drive_invocation = orig  # type: ignore[assignment]

    body = str(ei.value)
    assert "reached its context limit and stopped before finishing." in body
    assert "[context_exhausted]" in body
    assert "It wrote this handoff before stopping:" in body
    assert "Explored module X; next wire up Y." in body
    assert "Full prior transcript: " in body
    assert "transcript.jsonl" in body
    assert "You decide what happens next" in body
    assert spawns["n"] == 1  # no reincarnation respawn


def test_dispatch_task_hands_back_hard_context_window(tmp_path: Path) -> None:
    # Tier B: subagent hit the hard context_window fatal error with no handoff.
    # dispatch_task hands back a transcript-only body (not the old generic
    # "failed (context_window)" message) still carrying [context_exhausted].
    import anyio

    from imp.engine.agent_run import AgentRunResult, ErrorInfo

    ctx = _dispatch_ctx(tmp_path)

    async def _fake_drive(*_a, **_k):
        return AgentRunResult(output=None, errored=False, paused=False,
                              fatal=True, delay=None,
                              error=ErrorInfo("context_window", "prompt_too_long"))

    import imp.agents.subagents as sub_mod
    orig = sub_mod.drive_invocation
    sub_mod.drive_invocation = _fake_drive  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as ei:
            anyio.run(dispatch_task, ctx, "reviewer", "do x")
    finally:
        sub_mod.drive_invocation = orig  # type: ignore[assignment]

    body = str(ei.value)
    assert "reached its context limit and stopped before finishing." in body
    assert "[context_exhausted]" in body
    assert "It hit the hard context limit without writing a handoff." in body
    assert "Full prior transcript: " in body
    assert "transcript.jsonl" in body
    assert "It wrote this handoff before stopping:" not in body


def test_dispatch_task_missing_handoff_note_degrades_to_transcript(
        tmp_path: Path) -> None:
    # A handoff_path is set but the note file is absent on disk. dispatch_task
    # must NOT crash — it degrades to the transcript-only body, same as Tier B.
    import anyio

    from imp.engine.agent_run import AgentRunResult

    ctx = _dispatch_ctx(tmp_path)

    async def _fake_drive(*_a, **_k):
        return AgentRunResult(output=None, errored=False, paused=False,
                              fatal=False, delay=None,
                              handoff_path=str(tmp_path / "absent.md"))

    import imp.agents.subagents as sub_mod
    orig = sub_mod.drive_invocation
    sub_mod.drive_invocation = _fake_drive  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as ei:
            anyio.run(dispatch_task, ctx, "reviewer", "do x")
    finally:
        sub_mod.drive_invocation = orig  # type: ignore[assignment]

    body = str(ei.value)
    assert "[context_exhausted]" in body
    assert "It hit the hard context limit without writing a handoff." in body
    assert "It wrote this handoff before stopping:" not in body


def test_dispatch_task_failed_handback_carries_transcript_pointer(
        tmp_path: Path) -> None:
    # PRODUCER: the failed/fatal handback (result.errored, delay=None so no budget
    # ceiling, not paused, no handoff -> not context-ceiling) now hands back a body
    # that carries the transcript pointer + re-dispatch prose so the task-lead can
    # point a fresh subagent at the prior transcript, while preserving the
    # "failed (<cat>) [<kind>]" marker line the _close_subagent_fold scan reads.
    import anyio

    from imp.engine.agent_run import AgentRunResult, ErrorInfo

    ctx = _dispatch_ctx(tmp_path)

    async def _fake_drive(*_a, **_k):
        return AgentRunResult(output=None, errored=True, paused=False,
                              fatal=False, delay=None,
                              error=ErrorInfo("network", "network"))

    import imp.agents.subagents as sub_mod
    orig = sub_mod.drive_invocation
    sub_mod.drive_invocation = _fake_drive  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as ei:
            anyio.run(dispatch_task, ctx, "reviewer", "do x")
    finally:
        sub_mod.drive_invocation = orig  # type: ignore[assignment]
    body = str(ei.value)
    # Marker line preserved (name, category, [kind]).
    assert "subagent 'reviewer' failed (network) [network]" in body
    # New: the work-preserving transcript pointer + re-dispatch prose.
    assert "Full prior transcript: " in body
    assert "transcript.jsonl" in body
    assert "re-dispatch a fresh subagent" in body
    assert "carry on from what you have" in body


def test_dispatch_task_failed_handback_omits_kind_suffix_when_kind_none(
        tmp_path: Path) -> None:
    # When result.error.kind is None the marker suffix is empty ("failed (network)"
    # with no bracket), but the transcript pointer + re-dispatch prose are still
    # present — the work-preserving lines are unconditional, the [kind] marker is not.
    import anyio

    from imp.engine.agent_run import AgentRunResult, ErrorInfo

    ctx = _dispatch_ctx(tmp_path)

    async def _fake_drive(*_a, **_k):
        return AgentRunResult(output=None, errored=True, paused=False,
                              fatal=False, delay=None,
                              error=ErrorInfo("network", None))

    import imp.agents.subagents as sub_mod
    orig = sub_mod.drive_invocation
    sub_mod.drive_invocation = _fake_drive  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as ei:
            anyio.run(dispatch_task, ctx, "reviewer", "do x")
    finally:
        sub_mod.drive_invocation = orig  # type: ignore[assignment]
    body = str(ei.value)
    assert "subagent 'reviewer' failed (network)" in body
    assert "failed (network) [" not in body   # no kind bracket
    assert "Full prior transcript: " in body


def test_dispatch_task_midloop_pause_emits_no_audit_error(tmp_path: Path) -> None:
    # PRODUCER: a human pause signaled DURING the backoff sleep hits the mid-loop
    # should_pause handback (subagents.py:495-497), which hands back via
    # _suspend_handback_body ([interrupted] marker) and — like every pause path —
    # emits NO audit `error` event. The real event_log + the emitted agent_retry
    # make a leaked audit observable (a falsifiable negative).
    import anyio

    from imp.engine.agent_run import AgentRunResult, ErrorInfo
    from imp.engine.state import InMemoryEventLog

    ctx = _dispatch_ctx(tmp_path)
    ctx.event_log = InMemoryEventLog()   # real WRITE log so a leaked audit is observable

    async def _fake_drive(*_a, **_k):
        # A retryable transient opens a backoff sleep; the mid-loop pause fires
        # AFTER the sleep, before any re-drive.
        return AgentRunResult(output=None, errored=True, paused=False,
                              fatal=False, delay=1.0,
                              error=ErrorInfo("network", "network"))

    async def _pause_during_sleep(_delay, coordinator):
        coordinator._should_pause = True

    import imp.agents.subagents as sub_mod
    orig_drive = sub_mod.drive_invocation
    orig_sleep = sub_mod._interruptible_sleep
    sub_mod.drive_invocation = _fake_drive              # type: ignore[assignment]
    sub_mod._interruptible_sleep = _pause_during_sleep  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as ei:
            anyio.run(dispatch_task, ctx, "reviewer", "do x")
    finally:
        sub_mod.drive_invocation = orig_drive           # type: ignore[assignment]
        sub_mod._interruptible_sleep = orig_sleep       # type: ignore[assignment]
    body = str(ei.value)
    assert "[interrupted]" in body                      # mid-loop pause handback
    events = list(ctx.event_log.read())
    assert any(e["type"] == "agent_retry" for e in events)          # log is live
    assert not any(e["type"] == "error" and e["payload"].get("audit")
                   for e in events)                    # no audit on the pause path
