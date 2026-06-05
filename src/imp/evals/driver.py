"""The single primitive every eval agent run funnels through. Runs ONE agent
(reviewer / judge / normalizer) via imp's REAL seam — build_agent_invocation ->
provider.create_runner -> drive_invocation — so there is no scaffolding mismatch
with production (spec Harness §). The model is bound DIRECTLY (provider_name +
model_name), bypassing tier/role inheritance (spec Variant §)."""

from __future__ import annotations

import shutil
import tempfile
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
    # Each eval-agent run is a stateless one-shot, but the provider reconstructs
    # its message transcript from the event log and `classify_resume`s on a
    # non-empty log (providers/anthropic.py). Two hazards if the log is neither
    # fresh nor isolated from project_root:
    #   1. Reuse/collision: judge calls all share project_root=repo-root and
    #      collide on agent_id (same {symbol}:{locus} across items + persisted
    #      across runs) -> the 2nd+ call RESUMES a foreign transcript instead of
    #      judging its own input -> every verdict silently degrades to `none`.
    #   2. Volatility: a reviewer's project_root is a DISPOSABLE worktree that the
    #      paired-run path tears down (and the reviewer's own bash can mutate); an
    #      events file under it can vanish mid-run -> FileNotFoundError on append.
    # Both are solved by giving every call its own throwaway events dir OUTSIDE
    # project_root, torn down once telemetry has been read back.
    events_dir = Path(tempfile.mkdtemp(prefix="imp-eval-"))
    events_path = events_dir / f"{safe_id}.events.jsonl"
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
        transcript_dir=events_dir / "transcripts",
        fs_permissions=(),  # reviewers/judges are read-only; file tools via /project map
        events_path=events_path,
        subagent_defs={},  # reviewers/judges/normalizers have no subagents
        tiers=(),
        max_tool_calls=None,
        agent_id=agent_id,
        event_log=event_log,
        change_state=None,
        first_event_parent_id=None,
        resolved_tiers=None,  # model bound directly; no tier inheritance needed
        coordinator=None,
        non_interactive=True,
    )
    try:
        runner = provider.create_runner(invocation)
        result = await drive_invocation(
            invocation, runner, coordinator=None, log=event_log, emit_events=True
        )
        telem = read_telemetry(events_path, agent_id)
        return result.output, telem
    finally:
        shutil.rmtree(events_dir, ignore_errors=True)


def drive_agent_sync(**kwargs: Any) -> tuple[dict[str, Any] | None, Telemetry]:
    """Blocking wrapper for callers outside an event loop (CLI, tests)."""
    return anyio.run(lambda: drive_agent(**kwargs))
