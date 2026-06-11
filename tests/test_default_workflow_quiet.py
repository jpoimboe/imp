from __future__ import annotations

from pathlib import Path

from imp.engine.loader import load_workflow

_QUIET = {
    "spec-verify-commit", "plan-verify-commit", "commit-check",
    "fast-path-spec-gate", "fast-path-plan-gate",
}


def test_plumbing_steps_are_quiet() -> None:
    wf = load_workflow(Path("src/imp/assets/workflows/default"))
    by_name = {s.name: s for s in wf.flat_steps()}
    for name in _QUIET:
        assert by_name[name].quiet is True, name


def test_substantive_steps_stay_loud() -> None:
    wf = load_workflow(Path("src/imp/assets/workflows/default"))
    by_name = {s.name: s for s in wf.flat_steps()}
    for name in ("spec-writer", "plan-writer", "task-lead", "spec-reviewer"):
        assert by_name[name].quiet is False, name
    # the fast-path CYCLE gate is intentionally NOT quiet (out of the five).
    assert by_name["fast-path-cycle-gate"].quiet is False
