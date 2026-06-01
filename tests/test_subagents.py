# tests/test_subagents.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
)
from imp.agents.subagents import (
    TASK_TOOL_SCHEMA,
    TaskMustBeAwaited,
    dispatch_task,
    make_task_tool,
)
from imp.agents.tools.context import ToolContext
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


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
    captured: dict = {}

    async def fake_spawn(sub_invocation: AgentInvocation):
        captured["invocation"] = sub_invocation

        async def _gen():
            yield {"type": "agent_output", "payload": {"status": "Success", "note": "done"}}

        return _gen()

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.spawn_subagent = fake_spawn
    ctx.change_context = ChangeContext(
        slug="change-42", title="Change 42", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow",
    )

    out = await dispatch_task(ctx, "reviewer", "please review")
    parsed = json.loads(out)
    assert parsed == {"status": "Success", "note": "done"}
    inv = captured["invocation"]
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

    captured: dict = {}

    async def fake_spawn(inv: AgentInvocation):
        captured["system_prompt"] = inv.system_prompt

        async def _gen():
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        return _gen()

    ctx.spawn_subagent = fake_spawn

    result = anyio.run(dispatch_task, ctx, "sub", "do the thing")
    assert captured["system_prompt"] == "hello change-7"
    # Sanity: dispatch_task still returns JSON-stringified agent_output.
    assert "Success" in result


@pytest.mark.anyio
async def test_dispatch_task_threads_shared_event_log_and_agent_id(tmp_path) -> None:
    from imp.agents.tools.context import ToolContext
    from imp.engine.state import InMemoryEventLog

    shared = InMemoryEventLog()
    captured: dict = {}

    async def fake_spawn(sub_inv):
        captured["agent_id"] = sub_inv.agent_id
        captured["event_log"] = sub_inv.event_log
        captured["first_event_parent_id"] = sub_inv.first_event_parent_id

        async def _gen():
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        return _gen()

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
        subagent_defs={"helper": _agent("helper")},
        parent_transcript_dir=tmp_path / "steps" / "p" / "s",
    )
    ctx.event_log = shared
    ctx.spawn_subagent = fake_spawn

    out = await dispatch_task(ctx, "helper", "do it", tool_call_event_id="tc-99")
    assert json.loads(out) == {"status": "Success"}
    assert captured["agent_id"] == "sub:tc-99"
    assert captured["event_log"] is shared
    assert captured["first_event_parent_id"] == "tc-99"


@pytest.mark.anyio
async def test_dispatch_task_uses_subagent_model_role(tmp_path: Path) -> None:
    """A subagent whose AgentDef.model_role is set resolves to that role's
    provider/model from ctx.resolved_roles, NOT the parent's."""
    from imp.providers.config import ProviderModel

    captured: dict = {}

    async def fake_spawn(sub_invocation: AgentInvocation):
        captured["inv"] = sub_invocation

        async def _gen():
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        return _gen()

    sub = _agent("reviewer")
    sub = AgentDef(  # rebuild with model_role set (AgentDef is frozen)
        name=sub.name, description=sub.description, tools=sub.tools,
        subagents=sub.subagents, output_schema=sub.output_schema,
        prompt_template_source=sub.prompt_template_source,
        source_path=sub.source_path, source=sub.source,
        model_role="code_review")

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"      # parent vendor
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.spawn_subagent = fake_spawn
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    ctx.resolved_roles = {"code_review": ProviderModel("openai", "gpt-5.2")}

    await dispatch_task(ctx, "reviewer", "review please")
    inv = captured["inv"]
    assert inv.provider_name == "openai"     # jumped vendor, not parent's anthropic
    assert inv.model_name == "gpt-5.2"
    assert inv.resolved_roles == ctx.resolved_roles


@pytest.mark.anyio
async def test_dispatch_task_roleless_subagent_inherits_parent(tmp_path: Path) -> None:
    captured: dict = {}

    async def fake_spawn(sub_invocation: AgentInvocation):
        captured["inv"] = sub_invocation

        async def _gen():
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        return _gen()

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}  # model_role None
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.spawn_subagent = fake_spawn
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")

    await dispatch_task(ctx, "reviewer", "go")
    inv = captured["inv"]
    assert inv.provider_name == "anthropic"  # inherited
    assert inv.model_name == "haiku"


def test_make_task_tool_no_tiers_is_unchanged() -> None:
    """Off-path: the schema is identical to the shared TASK_TOOL_SCHEMA."""
    tool = make_task_tool()
    assert tool.input_schema is TASK_TOOL_SCHEMA
    assert "model_tier" not in tool.input_schema["properties"]


def test_make_task_tool_with_tiers_adds_optional_enum() -> None:
    tool = make_task_tool(("fast", "balanced", "powerful"))
    props = tool.input_schema["properties"]
    assert props["model_tier"]["type"] == "string"
    assert props["model_tier"]["enum"] == ["fast", "balanced", "powerful"]
    # optional: NOT added to required
    assert "model_tier" not in tool.input_schema["required"]
    # original required args preserved
    assert set(tool.input_schema["required"]) == {"subagent_name", "task_description"}
    # shared constant not mutated
    assert "model_tier" not in TASK_TOOL_SCHEMA["properties"]


def test_make_task_tool_openai_strict_round_trip() -> None:
    """The generated tier schema survives the OpenAI strict transform: the
    optional enum becomes nullable AND null lands in the enum (Task 5)."""
    from imp.providers._openai_schema import to_openai_strict
    tool = make_task_tool(("fast", "powerful"))
    strict = to_openai_strict(tool.input_schema)
    tier = strict["properties"]["model_tier"]
    assert tier["type"] == ["string", "null"]
    assert None in tier["enum"]
    assert set(strict["required"]) == {"subagent_name", "task_description", "model_tier"}


@pytest.mark.anyio
async def test_dispatch_task_runtime_tier_overrides_frontmatter(tmp_path: Path) -> None:
    """A runtime model_tier wins over the subagent's static model_role."""
    from imp.providers.config import ProviderModel

    captured: dict = {}

    async def fake_spawn(sub_invocation: AgentInvocation):
        captured["inv"] = sub_invocation

        async def _gen():
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        return _gen()

    sub = _agent("reviewer")
    sub = AgentDef(  # rebuild with a static model_role
        name=sub.name, description=sub.description, tools=sub.tools,
        subagents=sub.subagents, output_schema=sub.output_schema,
        prompt_template_source=sub.prompt_template_source,
        source_path=sub.source_path, source=sub.source, model_role="code_review")

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.spawn_subagent = fake_spawn
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    ctx.model_tiers = ("powerful",)
    ctx.resolved_roles = {
        "code_review": ProviderModel("openai", "gpt-5.2"),     # frontmatter target
        "powerful": ProviderModel("anthropic", "opus"),        # runtime tier target
    }

    await dispatch_task(ctx, "reviewer", "go", model_tier="powerful")
    inv = captured["inv"]
    assert inv.provider_name == "anthropic"   # powerful, not code_review's openai
    assert inv.model_name == "opus"


@pytest.mark.anyio
async def test_dispatch_task_none_tier_falls_through_to_frontmatter(tmp_path: Path) -> None:
    """The model declines to pick a tier (None) -> static model_role still applies."""
    from imp.providers.config import ProviderModel

    captured: dict = {}

    async def fake_spawn(sub_invocation: AgentInvocation):
        captured["inv"] = sub_invocation

        async def _gen():
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        return _gen()

    sub = _agent("reviewer")
    sub = AgentDef(
        name=sub.name, description=sub.description, tools=sub.tools,
        subagents=sub.subagents, output_schema=sub.output_schema,
        prompt_template_source=sub.prompt_template_source,
        source_path=sub.source_path, source=sub.source, model_role="code_review")

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.spawn_subagent = fake_spawn
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    ctx.model_tiers = ("powerful",)
    ctx.resolved_roles = {
        "code_review": ProviderModel("openai", "gpt-5.2"),
        "powerful": ProviderModel("anthropic", "opus"),
    }

    await dispatch_task(ctx, "reviewer", "go", model_tier=None)
    inv = captured["inv"]
    assert inv.provider_name == "openai"      # fell through to frontmatter role
    assert inv.model_name == "gpt-5.2"


@pytest.mark.anyio
async def test_dispatch_task_blank_tier_falls_through_to_frontmatter(tmp_path: Path) -> None:
    """Defensive: an empty string is treated as 'inherit' via `model_tier or None`."""
    from imp.providers.config import ProviderModel

    captured: dict = {}

    async def fake_spawn(sub_invocation: AgentInvocation):
        captured["inv"] = sub_invocation

        async def _gen():
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        return _gen()

    sub = _agent("reviewer")
    sub = AgentDef(
        name=sub.name, description=sub.description, tools=sub.tools,
        subagents=sub.subagents, output_schema=sub.output_schema,
        prompt_template_source=sub.prompt_template_source,
        source_path=sub.source_path, source=sub.source, model_role="code_review")

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": sub}
    ctx.provider_name = "anthropic"
    ctx.model_name = "haiku"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.spawn_subagent = fake_spawn
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    ctx.model_tiers = ("powerful",)
    ctx.resolved_roles = {
        "code_review": ProviderModel("openai", "gpt-5.2"),
        "powerful": ProviderModel("anthropic", "opus"),
    }

    await dispatch_task(ctx, "reviewer", "go", model_tier="")
    inv = captured["inv"]
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
    ctx.model_tiers = ("fast", "powerful")
    ctx.resolved_roles = {"powerful": ProviderModel("anthropic", "opus")}

    with pytest.raises(ValueError, match="fast, powerful"):
        await dispatch_task(ctx, "reviewer", "go", model_tier="turbo")


@pytest.mark.anyio
async def test_dispatch_task_powerful_tier_cross_vendor(tmp_path: Path) -> None:
    """Parent on anthropic; model_tier='powerful' resolves to openai/gpt-5.2 —
    the subagent jumps vendor to the tier's resolved model."""
    from imp.providers.config import ProviderModel

    captured: dict = {}

    async def fake_spawn(sub_invocation: AgentInvocation):
        captured["inv"] = sub_invocation

        async def _gen():
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        return _gen()

    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent("reviewer")}   # no static model_role
    ctx.provider_name = "anthropic"                         # parent vendor
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.spawn_subagent = fake_spawn
    ctx.change_context = ChangeContext(slug="test", title="Test", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    ctx.model_tiers = ("fast", "balanced", "powerful")
    ctx.resolved_roles = {
        "fast": ProviderModel("anthropic", "claude-haiku-4-5-20251001"),
        "balanced": ProviderModel("anthropic", "claude-sonnet-4-6"),
        "powerful": ProviderModel("openai", "gpt-5.2"),
    }

    out = await dispatch_task(ctx, "reviewer", "hard problem", model_tier="powerful")
    assert json.loads(out) == {"status": "Success"}
    inv = captured["inv"]
    assert inv.provider_name == "openai"
    assert inv.model_name == "gpt-5.2"
