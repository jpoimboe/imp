"""Pins the reframed resume/interrupt copy: a user pause must read as 'paused,
not rejected/failed', and the continuation nudge must discourage re-soliciting.
Falsifiable against the spec's required wording, independent of the source."""
import json

from imp.agents.transcript import (
    RESUME_CONTINUE_NUDGE,
    RESUME_DURABILITY_NOTE,
    _synthetic_tool_result_content,
)
from imp.engine.agent_run import _CONTINUATION_NUDGE


def test_synthetic_non_bash_reframes_pause_not_rejection() -> None:
    body = _synthetic_tool_result_content("sub_agent")
    assert body.startswith("[interrupted]")
    assert "The user paused the run" in body
    assert "NOT a rejection, denial, or failure" in body
    assert "re-run or re-dispatch this call if its work is still needed" in body
    # The tool name is no longer interpolated into the body.
    assert "sub_agent" not in body
    # The repair tests key on this substring surviving.
    assert "interrupted" in body


def test_synthetic_bash_keeps_json_shape_and_reframes() -> None:
    for name in ("bash", "mcp__imp__bash"):
        parsed = json.loads(_synthetic_tool_result_content(name))
        assert parsed["stdout"] == ""
        assert parsed["returncode"] == -2
        assert "interrupted by user pause before result captured" in parsed["stderr"]
        assert "not a failure" in parsed["stderr"]
        assert "interrupted" in parsed["stderr"]


def test_durability_note_prepends_pause_framing_and_keeps_durability_text() -> None:
    assert RESUME_DURABILITY_NOTE.startswith(
        "[resume] The run was paused by the user and is now resuming."
    )
    assert "they were not rejected or failed" in RESUME_DURABILITY_NOTE
    # The original durability sentences are preserved verbatim.
    assert (
        "The tool results above were reconstructed from before this resume "
        "and may display as errors even when the original action succeeded."
    ) in RESUME_DURABILITY_NOTE
    assert "durable on disk — do not blindly redo them" in RESUME_DURABILITY_NOTE
    assert "run git log / git status" in RESUME_DURABILITY_NOTE


def test_continuation_nudge_reframes_pause_and_discourages_solicit() -> None:
    assert _CONTINUATION_NUDGE.startswith(
        "The run was paused by the user and is now resuming"
    )
    assert "this is not a rejection and nothing failed" in _CONTINUATION_NUDGE
    assert "call complete_step when it is done" in _CONTINUATION_NUDGE
    assert (
        "Do not ask the user how to proceed unless you genuinely need input"
    ) in _CONTINUATION_NUDGE


def test_codex_continue_nudge_matches_continuation_nudge() -> None:
    # Codex mirrors the continuation copy for cross-provider parity.
    assert RESUME_CONTINUE_NUDGE == _CONTINUATION_NUDGE
