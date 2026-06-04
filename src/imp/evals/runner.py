"""run_review: drive ONE reviewer agent against ONE fixture, end to end. Sets up
the disposable tree, injects task_context as the opening message (the reviewer
also runs `git show HEAD` for the diff), drives via the real seam, parses
findings, and always tears the tree down."""

from __future__ import annotations

from pathlib import Path

from imp.engine.loader import parse_agent_file
from imp.engine.types import AgentDef
from imp.evals.driver import baseline_agent_path, drive_agent_sync
from imp.evals.findings_parse import parse_review_findings
from imp.evals.models import Fixture, RunResult, Telemetry
from imp.evals.tree import materialize_fixture_tree, teardown_fixture_tree

_OPENING = """\
You are reviewing a single committed change. The change is already committed at
HEAD of this repository — run `git show HEAD` (or `git diff HEAD~1..HEAD`) to see
the diff, and grep the wider tree for callers/parallel paths as needed.

## Task context (what this change was supposed to do)
{task_context}

Review the change and report findings per your output schema."""


def _agent_def(agent_name: str, override_path: str | None) -> AgentDef:
    path = Path(override_path) if override_path else baseline_agent_path(agent_name)
    return parse_agent_file(path)


def _inject_review_context(worktree: Path, fixture: Fixture) -> None:
    """Write the fixture's task_context where the reviewer prompt looks for it, so
    its 'read the spec/plan/progress' step (code-reviewer.md:15-27) doesn't
    silently no-op against a slug='eval' tree that has none of those files. This
    narrows — but does NOT close — the context gap vs production (one paragraph is
    not a full spec + plans + progress log). ABSOLUTE recall from this harness is
    therefore NOT expected to transfer to production; only RELATIVE comparisons
    between variants AT EQUAL CONTEXT are valid (see grader §)."""
    spec = worktree / "imp" / "specs" / "eval.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(f"# Change: eval\n\n{fixture.task_context}\n")
    progress = worktree / ".imp" / "change" / "progress.md"
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text("# Progress\n\nReviewed change is committed at HEAD.\n")


def drive_review(
    worktree: Path,
    fixture: Fixture,
    agent_name: str,
    provider_name: str,
    model_name: str,
    *,
    repeat_index: int = 0,
    override_path: str | None = None,
    variant_id: str | None = None,
) -> RunResult:
    """Drive one reviewer in an ALREADY-materialized worktree. Used by panels so
    N members share one tree."""
    vid = variant_id or f"{agent_name}@{provider_name}/{model_name}"
    agent_def = _agent_def(agent_name, override_path)
    _inject_review_context(worktree, fixture)
    output, telem = drive_agent_sync(
        agent_def=agent_def,
        provider_name=provider_name,
        model_name=model_name,
        project_root=worktree,
        opening_message=_OPENING.format(task_context=fixture.task_context),
        agent_id=f"eval:{agent_name}",
    )
    prose = (output or {}).get("findings", "") if output else ""
    return RunResult(
        variant_id=vid,
        fixture_id=fixture.id,
        repeat_index=repeat_index,
        raw_review=prose,
        findings=parse_review_findings(prose),
        telemetry=telem or Telemetry.zero(),
    )


def run_review(
    control_root: Path,
    fixture: Fixture,
    agent_name: str,
    provider_name: str,
    model_name: str,
    *,
    repeat_index: int = 0,
    override_path: str | None = None,
    variant_id: str | None = None,
) -> RunResult:
    """Materialize the fixture tree, drive `agent_name` under (provider, model),
    return a RunResult. variant_id defaults to a self-describing single-agent id."""
    worktree = materialize_fixture_tree(control_root, fixture)
    try:
        return drive_review(
            worktree, fixture, agent_name, provider_name, model_name,
            repeat_index=repeat_index, override_path=override_path, variant_id=variant_id,
        )
    finally:
        teardown_fixture_tree(control_root, fixture)
