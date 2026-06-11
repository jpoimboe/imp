from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.loader import _parse_step
from imp.engine.types import Continue, WorkflowDefaults

_DEFAULTS = WorkflowDefaults(max_retries=3, on_max_retries=Continue(), max_loops=10)
_YAML = Path("workflow.yaml")


def _branch(raw: dict) -> object:
    return _parse_step(raw, "spec", Path("."), _DEFAULTS, {}, {}, _YAML)


def test_quiet_true_sets_the_step_flag() -> None:
    step = _branch({
        "name": "fast-path-spec-gate",
        "rules": [{"else": True, "continue": True}],
        "quiet": True,
    })
    assert step.quiet is True


def test_quiet_absent_defaults_false() -> None:
    step = _branch({
        "name": "spec-reviewer-gate",
        "rules": [{"else": True, "continue": True}],
    })
    assert step.quiet is False


def test_quiet_non_bool_is_rejected() -> None:
    with pytest.raises(LoadError, match="quiet"):
        _branch({
            "name": "bad-gate",
            "rules": [{"else": True, "continue": True}],
            "quiet": "yes",
        })
