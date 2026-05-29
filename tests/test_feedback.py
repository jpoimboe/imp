from __future__ import annotations

from imp.engine.feedback import (
    format_agent_feedback,
    format_human_feedback,
    format_script_feedback,
)

_WRAPPER_HEAD = "Your previous attempt completed but was not accepted."
_WRAPPER_TAIL = "Address this, then call `complete_step` again."


def test_script_feedback_frames_command_and_output() -> None:
    out = format_script_feedback("./scripts/test.sh", 1, "PASS a\n", "FAIL b\n")
    assert out.startswith(_WRAPPER_HEAD)
    assert out.endswith(_WRAPPER_TAIL)
    assert "`./scripts/test.sh` failed (exit 1)" in out
    assert "PASS a" in out
    assert "FAIL b" in out


def test_script_feedback_truncates_head_and_tail() -> None:
    body = "\n".join(f"line {i}" for i in range(2000))  # well over 4000 chars
    out = format_script_feedback("cmd", 1, body, "")
    assert len(out) < len(body)
    assert "line 0" in out          # head preserved
    assert "line 1999" in out       # tail preserved
    assert "truncated" in out       # marker present


def test_script_feedback_coerces_bytes() -> None:
    out = format_script_feedback("cmd", 2, b"\xff\xferaw", b"")
    assert "raw" in out             # undecodable bytes replaced, not crashed


def test_agent_feedback_names_reviewer_and_renders_fields() -> None:
    out = format_agent_feedback(
        "spec-reviewer", {"status": "Reject", "reason": "missing tests"})
    assert "spec-reviewer" in out
    assert "status: Reject" in out
    assert "reason: missing tests" in out
    assert out.startswith(_WRAPPER_HEAD)


def test_human_feedback_quotes_label() -> None:
    out = format_human_feedback("Send back for rework")
    assert "'Send back for rework'" in out
    assert out.startswith(_WRAPPER_HEAD)
