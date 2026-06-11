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


def _spec_writer() -> str:
    return (AGENTS / "spec-writer.md").read_text()


def test_spec_writer_drops_rework_solicitation_bans() -> None:
    text = _spec_writer()
    # The standalone rework-close ban is gone (replaced by the policy pointer).
    assert "Never re-solicit the user on rework" not in text
    # …but the kept bans remain.
    assert "do not re-solicit the user" in text          # pre-commit self-review
    assert "do\n**not** ask the user anything" in text or \
           "do **not** ask the user anything" in text     # minors pass


def test_spec_writer_carries_ask_vs_decide_policy_and_markers() -> None:
    text = _spec_writer()
    assert "*(decided during review)*" in text
    assert "*(user-confirmed during review)*" in text
    # the escalation channel is named
    assert "wait_for_user" in text
    # the decide-and-record default phrasing is present
    assert "decide and record" in text.lower()
