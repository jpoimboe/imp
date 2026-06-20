from __future__ import annotations

from pathlib import Path

from imp.engine.executor import _step_started_payload
from imp.engine.types import Continue, SourceLoc, Step


def _step(*, quiet: bool) -> Step:
    return Step(
        name="spec-verify-commit", phase="spec", kind="script", rules=(),
        on_max_retries=Continue(), max_loops=1,
        source=SourceLoc(file=Path("workflow.yaml"), line=1),
        command="./x.sh", quiet=quiet)


def test_payload_carries_step_kind_phase_and_quiet() -> None:
    p = _step_started_payload(_step(quiet=True))
    assert p == {"step": "spec-verify-commit", "kind": "script",
                 "phase": "spec", "quiet": True, "label": None,
                 "command": "./x.sh"}


def test_payload_quiet_false_for_a_loud_step() -> None:
    assert _step_started_payload(_step(quiet=False))["quiet"] is False
