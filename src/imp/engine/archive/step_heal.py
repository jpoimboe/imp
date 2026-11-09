# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Move states parked on `converge-verify`, the slow gate the default workflow retired,
to `wait-for-head`. Retired with the 16 -> 17 archive hop at the next _DATA_VERSION bump."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imp.assets import resolve_workflow_dir
from imp.engine import durable
from imp.engine.archive import record, records_root
from imp.engine.loader import load_workflow
from imp.engine.types import Step
from imp.providers.effective import resolve_default_workflow

_RETIRED = "converge-verify"
_SUCCESSOR = "wait-for-head"


def _successor(control_root: Path, workflow: str | None) -> Step | None:
    """The workflow the quest's next run re-snapshots; None while it still ships the step."""
    name = workflow or resolve_default_workflow(control_root=control_root)
    wf = load_workflow(resolve_workflow_dir(control_root, name))
    steps = {s.name: s for s in wf.flat_steps()}
    if _RETIRED in steps:
        return None
    return steps.get(_SUCCESSOR)


def _untemplated(label: str | None) -> str | None:
    return None if label is None or "{{" in label else label


def heal_state(
    control_root: Path, quest_meta: dict[str, Any], state: dict[str, Any],
) -> bool:
    """Move a state parked on the retired step to its successor in place; True if changed."""
    if state.get("step") != _RETIRED:
        return False
    try:
        successor = _successor(control_root, quest_meta.get("workflow"))
    except Exception:
        return False
    if successor is None:
        return False
    state["step"] = _SUCCESSOR
    state["resume_pending"] = None
    state["resume_target"] = None
    state["resume_should_continue"] = False
    if (state.get("retry_feedback") or {}).get("target_step") == _RETIRED:
        state["retry_feedback"] = None
    state["route_label"] = None
    state["unsandboxed"] = False
    state["step_label"] = _untemplated(successor.label)
    state["phase_label"] = _untemplated(successor.phase_label)
    return True


def heal_live_home(control_root: Path, home: Path) -> bytes | None:
    """Durably heal one live home's state.json; the new bytes, or None when unchanged."""
    state_json = home / "state" / "state.json"
    try:
        quest_meta = json.loads((home / "quest.json").read_bytes())
        state = json.loads(state_json.read_bytes())
    except (OSError, ValueError):
        return None
    if not heal_state(control_root, quest_meta, state):
        return None
    healed = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
    durable.durable_replace_bytes(state_json, healed)
    return healed


def heal_archived_records(control_root: Path) -> None:
    """Heal every resumable `archived` record; finished records keep the step as history."""
    records_dir = records_root(control_root)
    for cid in record.list_record_ids(records_dir):
        rec = record.read_record(records_dir, cid)
        if rec is None or rec["final_state"].get("status") != "archived":
            continue
        if heal_state(control_root, rec["quest_meta"], rec["final_state"]):
            record.write_record(records_dir, rec)
