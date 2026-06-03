"""§6 edge-coverage test: 7 unique findings-carrying (source, target) pairs
(9 rule entries total) must each have a non-empty feedback preamble in the
default workflow."""
from __future__ import annotations

import pathlib

from imp.engine.loader import load_workflow
from imp.engine.types import Goto

FINDINGS_TARGETS = {  # (source_step, target) pairs that MUST carry a preamble
    ("spec-reviewer", "spec-writer"),
    ("plan-review", "plan-writer"),
    ("task-lead", "plan-writer"),
    ("plan-cycle-check", "plan-writer"),
    ("code-reviewer", "implement"),
    ("code-reviewer", "plan-writer"),
    ("final-review", "implement"),
}


def _gotos(workflow):
    for step in workflow.flat_steps():
        for rule in step.rules:
            if isinstance(rule.action, Goto):
                yield step.name, rule.action


def test_every_findings_edge_has_a_preamble():
    wf = load_workflow(
        pathlib.Path("src/imp/assets/workflows/default"),
    )
    seen = set()
    for src, g in _gotos(wf):
        if (src, g.target) in FINDINGS_TARGETS:
            assert g.feedback, f"{src}->{g.target} lost its feedback preamble"
            seen.add((src, g.target))
    assert seen == FINDINGS_TARGETS, f"missing edges: {FINDINGS_TARGETS - seen}"
