"""The heal that moves a state parked on the retired `converge-verify` step to
`wait-for-head` (archive data 16 -> 17)."""
from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

import imp
from imp.engine.archive import record, records_root, step_heal
from imp.engine.archive.step_heal import heal_archived_records, heal_live_home, heal_state
from tests.conftest import _seed_control, write_agent_md

_STUB = ("name: stub\ntools: [complete_step]\n"
         "output:\n  status: {type: enum, values: [Success, Error]}")
FORK = (
    'name: fork\ndescription: ""\nphases:\n  - name: p\n    steps:\n'
    "      - {name: converge-verify, file: agents/stub.md}\n"
    "      - {name: wait-for-head, file: agents/stub.md}\n")
SOLO = ('name: solo\ndescription: ""\nphases:\n  - name: p\n    steps:\n'
        "      - {name: s, file: agents/stub.md}\n")
TEMPLATED = (
    'name: tmpl\ndescription: ""\nvars:\n  n: {type: string, default: "x"}\n'
    'phases:\n  - name: p\n    label: "phase {{ vars.n }}"\n    steps:\n'
    '      - {name: wait-for-head, file: agents/stub.md, label: "waiting {{ vars.n }}"}\n')
DEFAULT = {"workflow": "default"}   # a bare tmp_path resolves this to the bundled default

HEALED = {
    "step": "wait-for-head", "resume_pending": None, "resume_target": None,
    "resume_should_continue": False, "retry_feedback": None, "route_label": None,
    "unsandboxed": False, "step_label": "waiting in merge queue",
    "phase_label": "integration phase",
}


def workflow_at(control: Path, name: str, yaml_text: str) -> None:
    wf = control / "imp" / "workflows" / name
    write_agent_md(wf / "agents" / "stub.md", frontmatter=_STUB)
    (wf / "workflow.yaml").write_text(yaml_text)


def parked(**extra: Any) -> dict[str, Any]:
    return {
        "id": "park01", "step": "converge-verify", "status": "paused",
        "halt_condition": "waiting", "resume_pending": "converge-verify",
        "resume_target": "agent", "resume_should_continue": True,
        "retry_feedback": {"target_step": "converge-verify", "text": "slow gate failed"},
        "route_label": "back to tests", "unsandboxed": True,
        "step_label": "waiting to test", "phase_label": "converge phase",
        "vars": {"fast_path": False}, **extra,
    }


def _dump(state: dict[str, Any]) -> bytes:
    return json.dumps(state, indent=2, sort_keys=True).encode("utf-8")


def test_heal_state_moves_a_parked_state_to_wait_for_head(tmp_path: Path) -> None:
    state = parked()

    assert heal_state(tmp_path, DEFAULT, state) is True

    assert state == {**parked(), **HEALED}
    assert (state["status"], state["halt_condition"]) == ("paused", "waiting")


def test_heal_state_keeps_retry_feedback_aimed_at_another_step(tmp_path: Path) -> None:
    fb = {"target_step": "integration-verify", "text": "t"}
    state = parked(retry_feedback=fb)

    assert heal_state(tmp_path, DEFAULT, state) is True
    assert state["retry_feedback"] == fb


@pytest.mark.parametrize("step", ["deferred-sweep", "wait-for-head", "verify", None])
def test_heal_state_ignores_every_other_step(tmp_path: Path, step: str | None) -> None:
    state = parked(step=step)
    before = copy.deepcopy(state)

    assert heal_state(tmp_path, DEFAULT, state) is False
    assert state == before


@pytest.mark.parametrize(("name", "yaml_text"), [("default", FORK), ("solo", SOLO)])
def test_heal_state_needs_wait_for_head_and_no_retired_step(
        tmp_path: Path, name: str, yaml_text: str) -> None:
    workflow_at(tmp_path, name, yaml_text)
    state = parked()

    assert heal_state(tmp_path, {"workflow": name}, state) is False
    assert state == parked()


def test_heal_state_leaves_an_unresolvable_workflow(tmp_path: Path) -> None:
    state = parked()

    assert heal_state(tmp_path, {"workflow": "nosuch"}, state) is False
    assert state == parked()


def test_a_null_workflow_resolves_through_the_project_default(tmp_path: Path) -> None:
    _seed_control(tmp_path)               # default-workflow `imp`: a stub with no wait-for-head
    state = parked()
    assert heal_state(tmp_path, {"workflow": None}, state) is False
    assert state == parked()

    workflow_at(tmp_path, "imp", TEMPLATED)

    assert heal_state(tmp_path, {}, state) is True
    assert state["step"] == "wait-for-head"
    # a templated label is left for the executor to render at the next run
    assert (state["step_label"], state["phase_label"]) == (None, None)


def test_heal_live_home_rewrites_durably_and_returns_the_bytes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "quest.json").write_text(json.dumps({"id": "park01", **DEFAULT}))
    state_json = home / "state" / "state.json"
    state_json.write_bytes(_dump(parked()))

    healed = heal_live_home(tmp_path, home)

    assert healed == state_json.read_bytes() == _dump({**parked(), **HEALED})
    assert heal_live_home(tmp_path, home) is None      # idempotent
    assert state_json.read_bytes() == healed


def test_heal_live_home_leaves_unreadable_or_unmatched_homes(tmp_path: Path) -> None:
    homes = {}
    for qid, meta, raw in (
            ("brok01", json.dumps(DEFAULT), b"{not json"),
            ("nome01", None, _dump(parked())),
            ("othr01", json.dumps(DEFAULT), _dump(parked(step="deferred-sweep")))):
        home = tmp_path / qid
        (home / "state").mkdir(parents=True)
        if meta is not None:
            (home / "quest.json").write_text(meta)
        (home / "state" / "state.json").write_bytes(raw)
        homes[home] = raw

    for home, raw in homes.items():
        assert heal_live_home(tmp_path, home) is None
        assert (home / "state" / "state.json").read_bytes() == raw


def test_heal_archived_records_rewrites_only_archived_records(tmp_path: Path) -> None:
    rdir = records_root(tmp_path)
    for qid, status, step in (("arch01", "archived", "converge-verify"),
                              ("cmpl01", "completed", "converge-verify"),
                              ("abnd01", "abandoned", "converge-verify"),
                              ("arch02", "archived", "spec")):
        meta = {"id": qid, "title": qid, "base_branch": "main", **DEFAULT}
        record.write_record(rdir, record.build_record(
            meta, parked(id=qid, status=status, step=step), "0" * 64,
            resumable=status == "archived"))
    before = record.read_record(rdir, "arch01")
    assert before is not None
    keep = {q: (rdir / f"{q}.json").read_bytes() for q in ("cmpl01", "abnd01", "arch02")}

    heal_archived_records(tmp_path)

    assert record.read_record(rdir, "arch01") == {
        **before, "final_state": {**before["final_state"], **HEALED}}
    for qid, raw in keep.items():
        assert (rdir / f"{qid}.json").read_bytes() == raw, qid


def test_step_heal_exposes_exactly_its_three_heals() -> None:
    public = {n for n, obj in vars(step_heal).items()
              if inspect.isfunction(obj) and obj.__module__ == step_heal.__name__
              and not n.startswith("_")}
    assert public == {"heal_state", "heal_live_home", "heal_archived_records"}


def test_the_retired_step_name_is_confined_to_the_heal_module() -> None:
    src = Path(imp.__file__).resolve().parents[1]
    naming = sorted(
        p.relative_to(src).as_posix() for p in src.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
        and (b"converge-verify" in p.read_bytes() or b"converge_verify" in p.read_bytes()))
    assert naming == ["imp/engine/archive/step_heal.py"]
