from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.loader import _parse_phase_chrome, _parse_step, load_workflow
from imp.engine.types import Continue, SourceLoc, WorkflowDefaults

_DEFAULTS = WorkflowDefaults(max_retries=3, on_max_retries=Continue(), max_loops=10)
_YAML = Path("workflow.yaml")
_SRC = SourceLoc(file=_YAML, line=1)


def _branch(raw: dict, *, chrome: str | None = None) -> object:
    return _parse_step(raw, "front-door", Path("."), _DEFAULTS, {}, {}, _YAML,
                       chrome=chrome)


def test_chrome_value_stamps_onto_the_step() -> None:
    step = _branch(
        {"name": "scoping", "rules": [{"else": True, "continue": True}]},
        chrome="none")
    assert step.chrome == "none"


def test_chrome_absent_defaults_none() -> None:
    step = _branch({"name": "scoping", "rules": [{"else": True, "continue": True}]})
    assert step.chrome is None


def test_parse_phase_chrome_accepts_none_keyword() -> None:
    assert _parse_phase_chrome({"name": "front-door", "chrome": "none"}, _SRC) == "none"


def test_parse_phase_chrome_absent_is_none() -> None:
    assert _parse_phase_chrome({"name": "spec"}, _SRC) is None


def test_parse_phase_chrome_rejects_other_values() -> None:
    with pytest.raises(LoadError, match="chrome"):
        _parse_phase_chrome({"name": "front-door", "chrome": "headers"}, _SRC)


# ── load_workflow wiring tests ─────────────────────────────────────────────


def _write_wf(root: Path, workflow_yaml: str) -> Path:
    wf = root / "wf"
    wf.mkdir(parents=True)
    (wf / "workflow.yaml").write_text(textwrap.dedent(workflow_yaml))
    return wf


def test_load_workflow_chrome_none_propagates_to_steps(tmp_path: Path) -> None:
    wf = _write_wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: main
            chrome: none
            steps:
              - name: router
                rules:
                  - {else: true, continue: true}
        """)
    steps = list(load_workflow(wf).flat_steps())
    assert steps and all(s.chrome == "none" for s in steps)


def test_load_workflow_chrome_invalid_raises_load_error(tmp_path: Path) -> None:
    wf = _write_wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: main
            chrome: headers
            steps:
              - name: router
                rules:
                  - {else: true, continue: true}
        """)
    with pytest.raises(LoadError):
        load_workflow(wf)
