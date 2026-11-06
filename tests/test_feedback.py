from __future__ import annotations

from imp.engine.feedback import (
    COMPLETION_BACKSTOP,
    COMPLETION_NUDGE,
    apply_completion_backstop,
    format_clean_tree_feedback,
    format_script_feedback,
    render_output_fields,
)

_WRAPPER_HEAD = "Your previous attempt completed but was not accepted."


def test_script_feedback_frames_command_and_output() -> None:
    out = format_script_feedback("./scripts/test.sh", 1, "PASS a\n", "FAIL b\n")
    assert out.startswith(_WRAPPER_HEAD)
    assert "Address this, then call" not in out   # tail moved to the backstop
    assert "`./scripts/test.sh` failed (exit 1)" in out
    assert "failed (exit 1).\n" not in out   # no trailing period on the headline
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


def test_script_feedback_composes_configured_feedback() -> None:
    out = format_script_feedback("cmd", 2, "out\n", "err\n", feedback="Authored wording:")
    assert out == "Authored wording:\nGate `cmd` failed (exit 2)\nout\nerr\n"


def test_script_feedback_without_configured_feedback_keeps_wrapper() -> None:
    out = format_script_feedback("cmd", 2, "out\n", "err\n")
    assert out == f"{_WRAPPER_HEAD} Gate `cmd` failed (exit 2)\nout\nerr\n"


def test_script_feedback_with_configured_feedback_truncates_head_and_tail() -> None:
    body = "\n".join(f"line {i}" for i in range(2000))  # well over 4000 chars
    out = format_script_feedback("cmd", 1, body, "", feedback="Authored wording:")
    assert out.startswith("Authored wording:\nGate `cmd` failed (exit 1)\nline 0\n")
    assert out.endswith("line 1999")
    assert "lines truncated …]" in out
    assert "line 1000\n" not in out
    assert len(out) < len(body)


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


def test_completion_nudge_wording_is_waiting_first_and_names_the_tool() -> None:
    text = COMPLETION_NUDGE
    assert "WAITING" in text
    assert "`complete_step`" in text
    # rindex skips the "ended without `complete_step`" preamble, targeting the option clause.
    assert text.index("WAITING") < text.rindex("`complete_step`")
    assert "restate" in text.lower() or "repeat" in text.lower()
