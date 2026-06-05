"""Multi-agent review structures (spec Harness §). A structure runs N single
agents in ONE materialized worktree and unions their findings through the SHARED
`dedup_findings` — the same `(symbol, locus)` semantics the grader uses, so a
panel's union and the grader agree on 'same finding'. monolithic = exactly one
member; slice-panel = many distinct reviewer agents. The completeness slice is
just a panel member with an override prompt (Task 24). (ensemble — same agent xN
under different models — is deferred; see STRUCTURES note in models.py.)"""

from __future__ import annotations

from pathlib import Path

from imp.evals.matching import dedup_findings
from imp.evals.models import (
    PER_TASK_STRUCTURES,
    Fixture,
    ReviewFinding,
    RunResult,
    Telemetry,
    Variant,
)
from imp.evals.runner import drive_review
from imp.evals.tree import (
    materialize_fixture_tree,
    materialize_task_tree,
    teardown_fixture_tree,
)


def run_variant(
    control_root: Path,
    variant: Variant,
    fixture: Fixture,
    *,
    repeat_index: int = 0,
) -> RunResult:
    """Run every member in `variant.role_to_model_map` against one shared
    worktree; union + dedup findings; sum telemetry. Member findings are tagged
    by agent_id at the telemetry layer (distinct events.jsonl per member)."""
    if variant.structure in PER_TASK_STRUCTURES:
        return _run_variant_per_task(
            control_root, variant, fixture, repeat_index=repeat_index
        )
    worktree = materialize_fixture_tree(control_root, fixture)
    all_findings: list[ReviewFinding] = []
    prose_parts: list[str] = []
    telem = Telemetry.zero()
    total_dollars = 0.0
    try:
        for agent_name, (provider, model) in variant.role_to_model_map.items():
            member = drive_review(
                worktree,
                fixture,
                agent_name,
                provider,
                model,
                repeat_index=repeat_index,
                override_path=variant.prompt_overrides.get(agent_name),
                variant_id=variant.id,
            )
            all_findings.extend(member.findings)
            telem = telem + member.telemetry  # tokens/tool-calls/wall all SUM
            total_dollars += member.dollars
            prose_parts.append(f"## {agent_name} ({provider}/{model})\n{member.raw_review}")
    finally:
        teardown_fixture_tree(control_root, fixture)

    # Members run SEQUENTIALLY in one shared worktree, so wall is the SUM (honest
    # for sequential execution — NOT max, which would pretend parallelism we don't
    # do). Per-member telemetry IS distinct: each drive_agent writes its own
    # events file keyed by agent_id and read_telemetry filters by it. If members
    # are ever parallelized (separate worktrees), switch wall to max then.
    # Dedup only for panels (>1 member): for a single member it can only LOSE
    # findings (two distinct issues at one file:line with empty symbols collapse).
    if len(variant.role_to_model_map) > 1:
        findings = dedup_findings(all_findings)
    else:
        findings = tuple(all_findings)
    return RunResult(
        variant_id=variant.id,
        fixture_id=fixture.id,
        repeat_index=repeat_index,
        raw_review="\n\n".join(prose_parts),
        findings=findings,
        telemetry=telem,
        dollars=total_dollars,
    )


def _run_variant_per_task(
    control_root: Path,
    variant: Variant,
    fixture: Fixture,
    *,
    repeat_index: int = 0,
) -> RunResult:
    """Per-task review: for each task i, materialize the incremental tree (HEAD ==
    tasks[i].diff, working tree == base + tasks[0..i]) and review task i's diff there
    with tasks[i].task_context. Dedup WITHIN each task's panel; union (concat) ACROSS
    tasks. Returns exactly ONE RunResult per cell. Telemetry/dollars sum."""
    all_findings: list[ReviewFinding] = []
    prose_parts: list[str] = []
    telem = Telemetry.zero()
    total_dollars = 0.0
    for i, task in enumerate(fixture.tasks):
        worktree = materialize_task_tree(control_root, fixture, i)
        task_findings: list[ReviewFinding] = []
        try:
            for agent_name, (provider, model) in variant.role_to_model_map.items():
                member = drive_review(
                    worktree,
                    fixture,
                    agent_name,
                    provider,
                    model,
                    repeat_index=repeat_index,
                    override_path=variant.prompt_overrides.get(agent_name),
                    variant_id=variant.id,
                    review_context=task.task_context,
                )
                task_findings.extend(member.findings)
                telem = telem + member.telemetry  # tokens/tool-calls/wall all SUM
                total_dollars += member.dollars
                prose_parts.append(
                    f"## task {i} - {agent_name} ({provider}/{model})\n{member.raw_review}"
                )
        finally:
            teardown_fixture_tree(control_root, fixture, task_index=i)
        # Dedup WITHIN a task's panel (>1 member); union (concat) ACROSS tasks so a
        # cross-task locus flagged at each task survives once per task.
        if len(variant.role_to_model_map) > 1:
            all_findings.extend(dedup_findings(task_findings))
        else:
            all_findings.extend(task_findings)
    return RunResult(
        variant_id=variant.id,
        fixture_id=fixture.id,
        repeat_index=repeat_index,
        raw_review="\n\n".join(prose_parts),
        findings=tuple(all_findings),
        telemetry=telem,
        dollars=total_dollars,
    )
