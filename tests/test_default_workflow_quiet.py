from __future__ import annotations

from pathlib import Path

from imp.engine.loader import load_workflow

_QUIET = {
    # spec/plan/implement plumbing
    "spec-verify-commit", "plan-verify-commit",
    # per-batch fold+stamp + diff-base pinning (mechanical git, no narrative)
    "cleanup-implement", "review-base-repin",
    # review/merge plumbing: mechanical git scripts + pure routing gates
    "stamp-trailers",
    "integrate-rebase",
}


def test_plumbing_steps_are_quiet() -> None:
    wf = load_workflow(Path("src/imp/assets/workflows/imp"))
    by_name = {s.name: s for s in wf.flat_steps()}
    for name in _QUIET:
        assert by_name[name].quiet is True, name


def test_substantive_steps_stay_loud() -> None:
    wf = load_workflow(Path("src/imp/assets/workflows/imp"))
    by_name = {s.name: s for s in wf.flat_steps()}
    # agents and the consequential merge step carry real narrative — never quiet.
    for name in ("spec-writer", "plan-writer", "task-lead", "spec-reviewer",
                 "verify", "code-reviewer", "deferred-sweep",
                 "autosquash-resolve-implement", "integrator", "integration-reviewer",
                 "integration-resolver", "finalize", "merge-to-base",
                 "wait-for-head"):
        assert by_name[name].quiet is False, name
