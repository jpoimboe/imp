from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.loader import _parse_phase_label, load_workflow
from imp.engine.types import SourceLoc

_SRC = SourceLoc(file=Path("workflow.yaml"), line=1)


def test_parse_phase_label_reads_string() -> None:
    assert _parse_phase_label(
        {"name": "code", "label": "implementation phase"}, _SRC
    ) == "implementation phase"


def test_parse_phase_label_absent_is_none() -> None:
    assert _parse_phase_label({"name": "spec"}, _SRC) is None


def test_parse_phase_label_rejects_non_string() -> None:
    with pytest.raises(LoadError, match="label"):
        _parse_phase_label({"name": "code", "label": 7}, _SRC)


def _write_wf(root: Path, body: str) -> Path:
    wf = root / "wf"
    wf.mkdir(parents=True)
    (wf / "workflow.yaml").write_text(textwrap.dedent(body))
    return wf


def test_load_workflow_phase_label_propagates_to_phase_and_steps(tmp_path: Path) -> None:
    wf = _write_wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: main
            label: "implementation phase"
            steps:
              - name: router
                rules:
                  - {else: true, continue: true}
              - name: router2
                rules:
                  - {else: true, continue: true}
        """)
    w = load_workflow(wf)
    assert w.phases[0].label == "implementation phase"
    assert all(s.phase_label == "implementation phase" for s in w.flat_steps())


def test_load_workflow_phase_label_absent_is_none(tmp_path: Path) -> None:
    wf = _write_wf(tmp_path, """\
        name: t
        description: t
        phases:
          - name: main
            steps:
              - name: router
                rules:
                  - {else: true, continue: true}
        """)
    w = load_workflow(wf)
    assert w.phases[0].label is None
    assert all(s.phase_label is None for s in w.flat_steps())
