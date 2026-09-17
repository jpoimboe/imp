"""Every findings-carrying (source, target) edge in the default workflow must
have a non-empty feedback preamble: the engine appends the reviewer's rendered
output fields to any Goto that carries one, forward edges included."""
from __future__ import annotations

import pathlib

from imp.engine.loader import load_workflow
from imp.engine.types import Goto

FINDINGS_TARGETS = {
    ("spec-reviewer", "spec-finalizer"),
    ("spec-reviewer", "plan-writer"),
    ("plan-reviewer", "plan-writer"),
    ("finalize", "plan-writer"),
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
