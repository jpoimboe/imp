"""Retry re-entry: a duplicate step_started for the same (name, phase) reuses
the still-open StepNode instead of nesting a fresh one inside the previous
attempt (spec: fix-retry-step-nesting).
"""
from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _retry_sequence() -> list[dict[str, object]]:
    """Two rate_limit retries then success — the exact failure pattern from the
    spec's acceptance criterion 1."""
    return [
        ev("1", "change_created", slug="x", title="x"),
        ev("2", "step_started", step="coder", kind="agent", phase="implement",
           model="m", provider="anthropic", interactive=False),
        ev("3", "error", agent_id="step:implement/coder",
           message="429 …", category="rate_limit"),
        ev("4", "agent_retry",
           category="rate_limit", attempt=1, backoff_seconds=1.0),
        ev("5", "step_started", step="coder", kind="agent", phase="implement",
           model="m", provider="anthropic", interactive=False),
        ev("6", "error", agent_id="step:implement/coder",
           message="429 …", category="rate_limit"),
        ev("7", "agent_retry",
           category="rate_limit", attempt=2, backoff_seconds=2.0),
        ev("8", "step_started", step="coder", kind="agent", phase="implement",
           model="m", provider="anthropic", interactive=False),
        ev("9", "step_completed", step="coder", kind="agent",
           action={"verb": "success"}, var_mutations={}),
    ]


def test_retry_reentry_reuses_single_step_node_unnested() -> None:
    tree = nv.build_tree(_retry_sequence())

    # Exactly one PhaseNode named "implement".
    phases = [c for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert len(phases) == 1
    phase = phases[0]
    assert phase.name == "implement"

    # Exactly one StepNode named "coder" directly under the phase.
    steps = [c for c in phase.children if isinstance(c, nv.StepNode)]
    assert len(steps) == 1, (
        f"expected exactly 1 StepNode under the phase; got {len(steps)}: "
        f"{[s.name for s in steps]}"
    )
    coder = steps[0]
    assert coder.name == "coder"

    # That StepNode must have NO nested StepNode child — retries do not deepen.
    nested_steps = [c for c in coder.children if isinstance(c, nv.StepNode)]
    assert nested_steps == [], (
        f"retry must not nest a fresh StepNode under the previous attempt; "
        f"found nested: {[s.name for s in nested_steps]}"
    )

    # Direct error/warning children must appear in the executor's emit order:
    # error, agent_retry(warn), error, agent_retry(warn).
    errs = [c for c in coder.children if isinstance(c, nv.ErrorNode)]
    assert [(e.category, e.is_warning) for e in errs] == [
        ("rate_limit", False),
        ("rate_limit", True),
        ("rate_limit", False),
        ("rate_limit", True),
    ], (
        "rate_limit error/retry pairs must surface as direct children of the "
        f"single StepNode in order; got: {[(e.category, e.is_warning) for e in errs]}"
    )


def test_retry_reentry_preserves_first_attempt_start_ts() -> None:
    """Elapsed time must reflect the operator's wait — keep the FIRST
    step_started's timestamp, not the most recent attempt's (spec decision)."""
    events: list[dict[str, object]] = [
        ev("1", "change_created", slug="x", title="x"),
        {"id": "2", "agent_id": None, "type": "step_started",
         "timestamp": "2026-06-04T00:00:00+00:00",
         "payload": {"step": "coder", "kind": "agent", "phase": "implement",
                     "model": "m", "provider": "anthropic", "interactive": False}},
        ev("3", "agent_retry", category="network", attempt=1, backoff_seconds=1.0),
        {"id": "4", "agent_id": None, "type": "step_started",
         "timestamp": "2026-06-04T00:00:05+00:00",
         "payload": {"step": "coder", "kind": "agent", "phase": "implement",
                     "model": "m", "provider": "anthropic", "interactive": False}},
    ]
    tree = nv.build_tree(events)
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    coder = next(c for c in phase.children if isinstance(c, nv.StepNode))
    assert coder.start_ts == "2026-06-04T00:00:00+00:00"


def test_retry_reentry_does_not_clear_existing_children() -> None:
    """Unlike resume re-entry (which clears because the executor re-runs from
    scratch), retry re-entry must NOT clear prior error/warning children — every
    attempt's lines stay visible."""
    tree = nv.build_tree(_retry_sequence())
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    coder = next(c for c in phase.children if isinstance(c, nv.StepNode))
    errs = [c for c in coder.children if isinstance(c, nv.ErrorNode)]
    assert len(errs) == 4, (
        f"all four error/warning lines (2 errors + 2 retry warnings) must remain "
        f"as children after re-entry; got {len(errs)}"
    )
