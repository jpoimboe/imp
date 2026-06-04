"""The single primitive every eval agent run funnels through. Runs ONE agent
(reviewer / judge / normalizer) via imp's REAL seam — build_agent_invocation ->
provider.create_runner -> drive_invocation — so there is no scaffolding mismatch
with production (spec Harness §). The model is bound DIRECTLY (provider_name +
model_name), bypassing tier/role inheritance (spec Variant §)."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

import anyio

from imp.agents.prompts import build_context, render_prompt
from imp.agents.runner import ChangeContext, Message, TextBlock
from imp.engine.agent_run import build_agent_invocation, drive_invocation
from imp.engine.state import JsonlEventLog
from imp.engine.types import AgentDef
from imp.evals.models import Telemetry
from imp.evals.telemetry import read_telemetry
from imp.providers import register_builtin_providers
from imp.providers.config import ProviderModel
from imp.providers.resolve import provider_from_model

_PROVIDERS_REGISTERED = False


def _ensure_providers() -> None:
    global _PROVIDERS_REGISTERED
    if not _PROVIDERS_REGISTERED:
        register_builtin_providers()
        _PROVIDERS_REGISTERED = True


def _assets_root() -> Path:
    return Path(str(files("imp"))) / "assets" / "workflows" / "default"


def _evals_agents_root() -> Path:
    return Path(str(files("imp"))) / "evals" / "data" / "agents"


def baseline_agent_path(name: str) -> Path:
    """Resolve an agent .md by name: the baseline reviewers live in the asset
    workflow; the eval-only agents (completeness-slice, grader-judge,
    label-normalizer) live under evals/data/agents."""
    asset = _assets_root() / "agents" / f"{name}.md"
    if asset.exists():
        return asset
    return _evals_agents_root() / f"{name}.md"


def _provider_for(provider: str, model: str) -> Any:
    """Indirection seam so tests can stub provider construction."""
    _ensure_providers()
    return provider_from_model(ProviderModel(provider=provider, model=model))


async def drive_agent(
    *,
    agent_def: AgentDef,
    provider_name: str,
    model_name: str,
    project_root: Path,
    opening_message: str,
    agent_id: str,
    workflow_dir: Path | None = None,
    vars: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, Telemetry]:
    """Drive one agent to a terminal outcome. Returns (output, telemetry).
    output is None if the agent errored/paused. Telemetry is read back from the
    events.jsonl this run wrote, filtered to `agent_id`."""
    safe_id = agent_id.replace(":", "_").replace("/", "_")  # loci contain '/' (M7)
    events_path = project_root / ".imp" / "eval" / f"{safe_id}.events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event_log = JsonlEventLog(events_path)

    rendered = render_prompt(
        agent_def.prompt_template_source,
        build_context(
            change_slug="eval",
            change_title="eval",
            event_log=event_log,
            vars=vars or {},
        ),
    )
    provider = _provider_for(provider_name, model_name)
    invocation = build_agent_invocation(
        agent_def=agent_def,
        provider_name=provider.name,
        model_name=provider.model,
        rendered_system_prompt=rendered,
        initial_messages=(Message(role="user", content=(TextBlock(text=opening_message),)),),
        change_context=ChangeContext(
            slug="eval",
            title="eval",
            project_root=project_root,
            workflow_dir=workflow_dir or _assets_root(),
        ),
        interactive=False,
        user_chat_input_fn=None,
        transcript_dir=project_root / ".imp" / "eval" / "transcripts" / safe_id,
        fs_permissions=(),  # reviewers/judges are read-only; file tools via /project map
        events_path=events_path,
        subagent_defs={},  # reviewers/judges/normalizers have no subagents
        model_tiers=(),
        max_tool_calls=None,
        agent_id=agent_id,
        event_log=event_log,
        change_state=None,
        first_event_parent_id=None,
        resolved_roles=None,  # model bound directly; no tier inheritance needed
        coordinator=None,
        non_interactive=True,
    )
    runner = provider.create_runner(invocation)
    result = await drive_invocation(
        invocation, runner, coordinator=None, log=event_log, emit_events=True
    )
    telem = read_telemetry(events_path, agent_id)
    return result.output, telem


def drive_agent_sync(**kwargs: Any) -> tuple[dict[str, Any] | None, Telemetry]:
    """Blocking wrapper for callers outside an event loop (CLI, tests)."""
    return anyio.run(lambda: drive_agent(**kwargs))
