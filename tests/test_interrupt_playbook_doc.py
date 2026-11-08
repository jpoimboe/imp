"""tests/playbooks/interrupt-playbook.md — `imp resume` lands where you left."""

from __future__ import annotations

import re
from pathlib import Path

DOC = Path("tests/playbooks/interrupt-playbook.md")

_WHERE_YOU_LEFT = (
    "**Where `imp resume` lands — where you left.** On a materialized quest the landing is"
    " decided once, at the moment you leave, by where you were — never by the gesture (Ctrl-D,"
    " `/pause`, SIGTERM, SIGHUP, SIGQUIT, a vanished terminal) or by how the provider's turn"
    " ended (cooperative stop or hard kill after the 2s deadline):"
    " | Left from | `imp resume` lands on |"
    " |-----------|-----------------------|"
    " | mid-run (agent thinking, a tool or subagent running) | the agent, continuing — no"
    " intervening prompt |"
    " | mid-run on a script step (verify gate running) | the gate, re-run — no intervening hold"
    " prompt |"
    " | an agent's question | that question, re-asked; the typed answer reaches the agent |"
    " | the inline steer gate (after a Ctrl-C) | `paused · enter to continue · type to"
    " redirect · Ctrl-D to leave` |"
    " | a hold prompt (workflow pause, verify gate, or a resumed hold prompt) | that same hold"
    " prompt |"
    " Leaving again from the landing re-applies the same rule; a stale landing never outlives"
    " the step it was recorded for. Headless and autonomous resumes always run the agent."
    " Every resume — draft or built — first replays, under the `resuming` header, the current"
    " step agent's last message and everything after it (your replies, tool runs, subagent"
    " folds); earlier turns stay in `imp log <id>`. A step with no agent message yet replays"
    " nothing. An inert pending-question resume prints that question itself instead."
)
_RESUMED_HOLD = "`paused · enter to continue · type to redirect · Ctrl-D to leave`"


def _flat(text: str) -> str:
    """Collapse wrapping and blockquote markers so prose checks ignore line breaks."""
    return " ".join(re.sub(r"^>\s?", "", text, flags=re.M).split())


def _between(start: str, end: str) -> str:
    body = DOC.read_text()
    i = body.index(start)
    return _flat(body[i:body.index(end, i + len(start))])


def test_where_you_left_paragraph_closes_the_0b_bullets() -> None:
    section = _between("### 0b.", "> **The Ctrl-C disposition is uniform")
    assert section.endswith(_WHERE_YOU_LEFT)


def test_signal_leaves_mid_run_resume_to_the_agent() -> None:
    flat = _flat(DOC.read_text())
    assert ("After a mid-run SIGTERM, `imp resume` continues the agent,"
            " exactly as after a mid-run Ctrl-D.") in flat
    agent = "`imp resume` continues the agent, same as after a mid-run Ctrl-D"
    assert agent in _between("After the matrix:", "### 6a.")
    assert agent in _between("- **Ctrl-\\ deliberate detach", "\n- **")
    assert agent in _between("- **SIGHUP / closed terminal", "\n- **")
    rows = [line for line in DOC.read_text().splitlines()
            if line.startswith(("| **SIGTERM** |", "| **SIGHUP** |", "| **Ctrl-\\** (mid-run) |"))]
    assert len(rows) == 3
    for row in rows:
        assert "resume-to-agent" in row, row


def test_parked_question_survives_a_signal_leave() -> None:
    row_a = _between("| **A** |", "\n| **B** |")
    assert "the question stays parked and a later `imp resume` re-asks it." in row_a
    assert "the quest stays parked." not in row_a


def test_resume_and_verify_asserts_the_where_you_left_table() -> None:
    step = _between("### **`RESUME-AND-VERIFY(id)`**", "\n---")
    assert ("4. Assert the landing per the *where you left* table in §0b, for whichever"
            " gesture you left with.") in step
    assert ("Below the header it replays the step agent's last message and everything after"
            " it, earlier turns absent (§0b).") in step
    for check in ("**SIGTERM mid-run**", "**Ctrl-D at the resumed hold prompt**",
                  "**An answer typed at a re-asked question**"):
        assert check in step, check
    assert "Assert the **resume_target**" not in step


def test_hold_prompt_landing_is_conditional_on_leaving_from_it() -> None:
    flat = _flat(DOC.read_text())
    assert "which is also the surface `imp resume` lands on when you leave from it;" in flat
    assert "a mid-run leave on a script step instead re-runs the gate on resume" in flat
    assert "shown on `imp resume` into a paused quest" not in flat
    assert "(e.g. the inline steer gate after a Ctrl-C) → detaches" not in flat


def test_resteer_hold_prompt_text_is_named_paused() -> None:
    flat = _flat(DOC.read_text())
    sites = [m.end() for m in re.finditer(r"daemon resteer hold prompt", flat)]
    assert len(sites) == 5
    for end in sites:
        assert _RESUMED_HOLD in flat[end:end + 300], flat[end:end + 300]
    assert "identical to the inline steer gate" not in flat
    assert "`interrupted · enter to continue · type to redirect · Ctrl-D to leave`" in flat


def test_phase0_leave_outcomes_are_scoped_by_regime() -> None:
    flat = _flat(DOC.read_text())
    for stale in ("either regime", "Both regimes write a",
                  "because `imp resume` carries no leave-mode disposition",
                  "(materialized) or `conversation_saved` (phase-0)",
                  "a fresh `imp new` run mid-execution"):
        assert stale not in flat, stale
    row_b = _between("| **B** |", "\n| **C** |")
    assert "`conversation_saved`" in row_b
    assert "no** `workflow_paused`" in row_b
    step = _between("### **`INTERRUPT-AND-VERIFY", "### **`RESUME-AND-VERIFY")
    for label in ("**Mid-run Ctrl-D, phase 0:**", "**Ctrl-D at the inline steer gate, phase 0:**"):
        assert label in step, label
    clause = (
        "phase 0, live session: the save or archive spine; a direct leave (no Ctrl-C first) "
        "writes only `conversation_saved` (save) or `conversation_archived` (archive), and a "
        "leave after a Ctrl-C also keeps the Ctrl-C's `workflow_paused`; discarded if you "
        "haven't sent a message yet. Phase-0 pre-session prompts (opening, `/clear`, inert "
        "resume): §6 row A"
    )
    rows = [line for line in DOC.read_text().splitlines()
            if line.startswith(("| **SIGTERM** |", "| **SIGHUP** |", "| **Ctrl-\\** (mid-run) |",
                                "| **Ctrl-D** (mid-run) |"))]
    assert len(rows) == 4
    for row in rows:
        assert clause in row, row


def test_resume_landings_name_the_replayed_tail() -> None:
    flat = _flat(DOC.read_text())
    assert "transcript replays" not in flat
    assert ("(resume_target = None: the agent's last message and what followed replay,"
            " no intervening prompt)") in flat
    assert ("(the agent's last message and what followed replay, then the next agent turn"
            " runs) — no hold prompt is shown.") in flat
