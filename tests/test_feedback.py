from __future__ import annotations

from imp.engine.feedback import (
    COMPLETION_BACKSTOP,
    apply_completion_backstop,
    format_clean_tree_feedback,
    format_script_feedback,
    render_output_fields,
    summarize_transition_reason,
)

_WRAPPER_HEAD = "Your previous attempt completed but was not accepted."


def test_script_feedback_frames_command_and_output() -> None:
    out = format_script_feedback("./scripts/test.sh", 1, "PASS a\n", "FAIL b\n")
    assert out.startswith(_WRAPPER_HEAD)
    assert "Address this, then call" not in out   # tail moved to the backstop
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


def test_render_output_fields_is_plain_no_editorial() -> None:
    out = render_output_fields({"status": "Reject", "reason": "missing tests"})
    assert "status: Reject" in out
    assert "reason: missing tests" in out
    assert "not accepted" not in out          # no editorial
    assert "Your previous attempt" not in out  # no _WRAPPER


def test_apply_completion_backstop_appends_when_feedback_and_schema() -> None:
    out = apply_completion_backstop(
        "Here's review feedback to address: fix X.", has_schema=True)
    assert out is not None
    assert out.startswith("Here's review feedback to address: fix X.")
    assert out.endswith(COMPLETION_BACKSTOP)
    assert out.count(COMPLETION_BACKSTOP) == 1


def test_apply_completion_backstop_skips_when_feedback_empty() -> None:
    # Empty/absent wrapper_text => nothing to remind on => no backstop.
    assert apply_completion_backstop(None, has_schema=True) is None
    assert apply_completion_backstop("", has_schema=True) == ""


def test_apply_completion_backstop_skips_when_no_schema() -> None:
    # A schema-less target has no complete_step to call => feedback unchanged.
    assert apply_completion_backstop("some feedback", has_schema=False) == "some feedback"


def test_completion_backstop_preserves_human_escape_hatch() -> None:
    # The escape hatch: a genuine bare-turn-end yield to a human stays available.
    assert "genuinely need a person" in COMPLETION_BACKSTOP
    assert "`complete_step`" in COMPLETION_BACKSTOP


def test_script_feedback_no_longer_carries_complete_step_reminder() -> None:
    out = format_script_feedback("./scripts/test.sh", 1, "PASS a\n", "FAIL b\n")
    assert "Address this, then call" not in out
    assert "complete_step" not in out


def test_gate_feedback_reminder_has_single_source() -> None:
    # Mirrors the executor: gate feedback (format_script_feedback) run through the
    # same guard helper carries the reminder exactly once and drops the old tail.
    composed = apply_completion_backstop(
        format_script_feedback("cmd", 1, "out", "err"), has_schema=True)
    assert composed is not None
    assert "Address this, then call" not in composed
    assert composed.count(COMPLETION_BACKSTOP) == 1


def test_summarize_transition_reason_strips_framing_and_takes_first_line() -> None:
    fb = format_script_feedback("./scripts/gate.sh", 1, "50 coverage issues\nmore\n", "")
    reason = summarize_transition_reason(fb)
    assert reason is not None
    # framing prefix gone; summary is the meaningful first line, single-line
    assert reason.startswith("Gate `./scripts/gate.sh` failed (exit 1)")
    assert _WRAPPER_HEAD not in reason
    assert "\n" not in reason


def test_summarize_transition_reason_none_when_no_feedback() -> None:
    assert summarize_transition_reason(None) is None
    assert summarize_transition_reason("") is None
    assert summarize_transition_reason("   \n  \n\t") is None


def test_summarize_transition_reason_takes_first_nonempty_line_of_plain_text() -> None:
    # an agent review preamble ("Here's review feedback to address:\nfindings: ...")
    reason = summarize_transition_reason("Here's review feedback to address:\nfindings: x")
    assert reason == "Here's review feedback to address:"


def test_summarize_transition_reason_truncates_long_line() -> None:
    reason = summarize_transition_reason("x" * 200, limit=80)
    assert reason is not None
    assert len(reason) <= 80
    assert reason.endswith("…")
    assert reason.startswith("x")


def test_clean_tree_feedback_frames_paths_and_commit_instruction() -> None:
    out = format_clean_tree_feedback(" M src/foo.py\n?? scratch.txt")
    assert out.startswith(_WRAPPER_HEAD)                # reuses the resume frame
    assert "src/foo.py" in out                          # dirty paths listed
    assert "scratch.txt" in out
    assert "Commit all of your work" in out             # actionable instruction
    assert "merge or rebase" in out                     # resolution-in-progress path


def test_clean_tree_feedback_truncates_a_huge_path_list() -> None:
    body = "\n".join(f"?? file{i}.txt" for i in range(2000))  # well over 4000 chars
    out = format_clean_tree_feedback(body)
    assert len(out) < len(body)
    assert "file0.txt" in out       # head kept
    assert "file1999.txt" in out    # tail kept
    assert "truncated" in out       # marker present
