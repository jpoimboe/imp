"""Schema + prompt-content + yaml->prompt parity guards for the confirm gate."""
from __future__ import annotations

from pathlib import Path

from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow

AGENTS = Path("src/imp/assets/workflows/default/agents")


def test_spec_writer_schema_gains_confirmed_phase_and_unseen_changes() -> None:
    wf = load_workflow(default_workflow_dir())
    schema = wf.agents["spec-writer"].output_schema
    phase = next(f for f in schema.fields if f.name == "phase")
    assert phase.type == "enum"
    assert set(phase.values) == {"written", "confirmed"}
    unseen = next(f for f in schema.fields if f.name == "unseen_changes")
    assert unseen.type == "bool"
    assert unseen.required is True
