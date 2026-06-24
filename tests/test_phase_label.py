from __future__ import annotations

import textwrap
import types as _types
from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.errors import LoadError
from imp.engine.executor import _step_started_payload
from imp.engine.loader import _parse_phase_label, load_workflow
from imp.engine.types import Continue, SourceLoc, Step
from tests.conftest import _seed_control

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


def _step(*, phase_label: str | None) -> Step:
    return Step(
        name="task-lead", phase="code", kind="agent", rules=(),
        on_max_retries=Continue(), max_loops=1,
        source=SourceLoc(file=Path("workflow.yaml"), line=1),
        phase_label=phase_label)


def test_payload_carries_rendered_phase_label_when_set() -> None:
    p = _step_started_payload(_step(phase_label="implementation phase"),
                              _types.SimpleNamespace(route_label=None))
    assert p["phase_label"] == "implementation phase"


def test_payload_renders_templated_phase_label(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="t", change_id="aaa111", base_branch="main")
    p = _step_started_payload(
        _step(phase_label="plan {{ vars.plan_index }}/{{ vars.plan_total }}"),
        _types.SimpleNamespace(route_label=None, vars={"plan_index": 2, "plan_total": 5}),
        change=ch, log=ch.event_log(),
    )
    assert p["phase_label"] == "plan 2/5"


def test_payload_omits_phase_label_when_unset() -> None:
    p = _step_started_payload(_step(phase_label=None),
                              _types.SimpleNamespace(route_label=None))
    assert "phase_label" not in p
