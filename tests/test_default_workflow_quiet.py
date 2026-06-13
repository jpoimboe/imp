from __future__ import annotations

from pathlib import Path

from imp.engine.loader import load_workflow

_QUIET = {
    # spec/plan/implement plumbing
    "spec-verify-commit", "plan-verify-commit", "commit-check",
    "spec-exit-gate", "fast-path-plan-gate", "fast-path-cycle-gate",
    # review/merge plumbing: mechanical git scripts + pure routing gates
    "autosquash", "stamp-trailers", "integration-gate", "stamp-sweep-trailers",
    "final-review-gate", "base-fresh-check", "integrate-merge",
}


def test_plumbing_steps_are_quiet() -> None:
    wf = load_workflow(Path("src/imp/assets/workflows/default"))
    by_name = {s.name: s for s in wf.flat_steps()}
    for name in _QUIET:
        assert by_name[name].quiet is True, name


def test_substantive_steps_stay_loud() -> None:
    wf = load_workflow(Path("src/imp/assets/workflows/default"))
    by_name = {s.name: s for s in wf.flat_steps()}
    # agents and the consequential merge step carry real narrative — never quiet.
    for name in ("spec-writer", "plan-writer", "task-lead", "spec-reviewer",
                 "run-tests", "review", "deferred-sweep", "autosquash-resolve",
                 "integrator", "integration-review", "final-review", "merge-to-base"):
        assert by_name[name].quiet is False, name
