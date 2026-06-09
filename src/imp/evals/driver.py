"""The single primitive every eval agent run funnels through. Runs ONE agent
(reviewer / judge / normalizer) via imp's REAL seam — build_agent_invocation ->
provider.create_runner -> drive_invocation — so there is no scaffolding mismatch
with production (spec Harness §). The model is bound DIRECTLY (provider_name +
model_name), bypassing tier/role inheritance (spec Variant §)."""

from __future__ import annotations

import contextlib
import os
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
    """Resolve an agent .md by name: the baseline reviewers (including the
    review-panel slices) live in the asset workflow; the eval-only agents
    (grader-judge, label-normalizer) live under evals/data/agents."""
    asset = _assets_root() / "agents" / f"{name}.md"
    if asset.exists():
        return asset
    return _evals_agents_root() / f"{name}.md"


def _provider_for(provider: str, model: str) -> Any:
    """Indirection seam so tests can stub provider construction."""
    _ensure_providers()
    return provider_from_model(ProviderModel(provider=provider, model=model))


def _eval_log_root() -> Path:
    """Durable root for preserved eval event/transcript logs. Defaults under the
    system temp dir ($TMPDIR/imp-eval-logs) — deliberately OUT of the repo so a
    sweep's logs never clutter or risk being committed to the working tree.
    Override with IMP_EVAL_LOG_DIR to point at a managed path."""
    override = os.environ.get("IMP_EVAL_LOG_DIR")
    return Path(override) if override else Path(tempfile.gettempdir()) / "imp-eval-logs"


def _preserve_eval_dir(
    events_dir: Path, provider_name: str, model_name: str, safe_id: str,
) -> None:
    """Move a finished run's events dir into _eval_log_root() so its
    events.jsonl + transcript survive for inspection instead of being rmtree'd.

    Named `<provider>_<model>_<safe_id>_<unique>` so variants that share an
    agent_id (e.g. cc-opus vs cc-sonnet, both `eval:code-reviewer`) never
    collide. The trailing `<unique>` is the mkdtemp basename, so repeat runs of
    the same variant accumulate side by side rather than overwrite. Falls back
    to rmtree on any failure — a logging hiccup must never fail the eval run."""
    slug = f"{provider_name}_{model_name}_{safe_id}".replace("/", "_").replace(":", "_")
    try:
        dest = _eval_log_root() / f"{slug}_{events_dir.name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(events_dir), str(dest))
    except OSError:
        shutil.rmtree(events_dir, ignore_errors=True)


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
    timeout_s: float = 600.0,
) -> tuple[dict[str, Any] | None, Telemetry]:
    """Drive one agent to a terminal outcome. Returns (output, telemetry).
    output is None if the agent errored/paused. Telemetry is read back from the
    events.jsonl this run wrote, filtered to `agent_id`.

    `timeout_s` bounds the single attempt (`anyio.fail_after`): the SDK seam has no
    timeout of its own, so a hung model call would otherwise deadlock a sweep worker
    forever. On timeout the runner is asked to `force_terminate` IF it supports it —
    to kill the spawned subprocess so it doesn't orphan and keep burning quota.
    NOTE: the claude_code provider's `force_terminate` is currently NOT present (a
    pending follow-up), so on timeout the subprocess may linger; `fail_after` still
    abandons the call and the retry loop moves on."""
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
    # Both are solved by giving every call its own fresh events dir OUTSIDE
    # project_root. After telemetry is read back the dir is PRESERVED (moved into
    # _eval_log_root()) rather than deleted, so a failed/slow/empty run's
    # events.jsonl + transcript can be inspected post hoc without re-running a
    # billable eval. The next call still mkdtemps a brand-new dir, so the
    # resume/collision hazards above never reappear from the preserved logs.
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
        user_chat_input_fn=None,
        transcript_dir=events_dir / "transcripts",
        fs_permissions=(),  # reviewers/judges are read-only; file tools use worktree-relative paths
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
        try:
            with anyio.fail_after(timeout_s):
                result = await drive_invocation(
                    invocation, runner, coordinator=None, log=event_log, emit_events=True
                )
        except TimeoutError:
            # Best-effort: ask the runner to kill its spawned subprocess so a hung
            # call doesn't orphan it (quota leak). force_terminate is provider-optional
            # and currently absent on claude_code (pending follow-up) — absent it the
            # subprocess may linger, but the call is still abandoned and retried.
            ft = getattr(runner, "force_terminate", None)
            if ft is not None:
                with contextlib.suppress(Exception):
                    ft()
            raise
        telem = read_telemetry(events_path, agent_id)
        return result.output, telem
    finally:
        _preserve_eval_dir(events_dir, provider_name, model_name, safe_id)


def drive_agent_sync(**kwargs: Any) -> tuple[dict[str, Any] | None, Telemetry]:
    """Blocking wrapper (CLI / sweep threads) with a per-attempt timeout + bounded
    retry. Each attempt runs in a FRESH `anyio.run` (a cancelled loop must not be
    reused). On exhausting retries it returns `(None, zero)` rather than raising — so
    the caller (drive_review) marks the cell failed and it is dropped, instead of
    crashing the whole sweep. Env: IMP_EVAL_AGENT_TIMEOUT (s, default 600),
    IMP_EVAL_AGENT_ATTEMPTS (default 2)."""
    timeout_s = float(os.environ.get("IMP_EVAL_AGENT_TIMEOUT", "600"))
    attempts = max(1, int(os.environ.get("IMP_EVAL_AGENT_ATTEMPTS", "2")))
    kwargs.setdefault("timeout_s", timeout_s)
    for attempt in range(attempts):
        try:
            return anyio.run(lambda: drive_agent(**kwargs))
        except TimeoutError:
            if attempt == attempts - 1:
                return None, Telemetry.zero()
    return None, Telemetry.zero()
