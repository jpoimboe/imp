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
