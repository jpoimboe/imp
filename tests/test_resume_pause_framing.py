"""Pins the reframed resume/interrupt copy: a user pause must read as 'paused,
not rejected/failed', and the continuation nudge must discourage re-soliciting.
Falsifiable against the spec's required wording, independent of the source."""
import json

from imp.agents.transcript import (
    RESUME_CONTINUE_NUDGE,
    RESUME_DURABILITY_NOTE,
    RESUME_RETRY_NUDGE,
    _synthetic_tool_result_content,
)
from imp.engine.agent_run import continuation_nudge


def test_synthetic_non_bash_reframes_pause_not_rejection() -> None:
    body = _synthetic_tool_result_content("sub_agent")
    assert body.startswith("[interrupted]")
    assert "The run was paused" in body
    assert "NOT a rejection, denial, or failure" in body
    assert "re-run or re-dispatch this call if its work is still needed" in body
    # Cause-neutrality guards: no user attribution.
    assert "The user paused" not in body
    assert "by the user" not in body
    # The tool name is no longer interpolated into the body.
    assert "sub_agent" not in body
    # The repair tests key on this substring surviving.
    assert "interrupted" in body


def test_synthetic_bash_keeps_json_shape_and_reframes() -> None:
    for name in ("bash", "mcp__imp__bash"):
        parsed = json.loads(_synthetic_tool_result_content(name))
        assert parsed["stdout"] == ""
        assert parsed["returncode"] == -2
        assert "interrupted by a pause before result captured" in parsed["stderr"]
        assert "not a failure" in parsed["stderr"]
        assert "interrupted" in parsed["stderr"]
        # Cause-neutrality guard: no user attribution.
        assert "by user pause" not in parsed["stderr"]


def test_durability_note_prepends_pause_framing_and_keeps_durability_text() -> None:
    assert RESUME_DURABILITY_NOTE.startswith(
        "[resume] The run was paused and is now resuming."
    )
    assert "they were not rejected or failed" in RESUME_DURABILITY_NOTE
    # Cause-neutrality guard: no user attribution.
    assert "by the user" not in RESUME_DURABILITY_NOTE
    # The original durability sentences are preserved verbatim.
    assert (
        "The tool results above were reconstructed from before this resume "
        "and may display as errors even when the original action succeeded."
    ) in RESUME_DURABILITY_NOTE
    assert "durable on disk — do not blindly redo them" in RESUME_DURABILITY_NOTE
    assert "run git log / git status" in RESUME_DURABILITY_NOTE


def test_continuation_nudge_reframes_pause_and_discourages_solicit() -> None:
    assert RESUME_CONTINUE_NUDGE.startswith(
        "The run was paused and is now resuming"
    )
    assert "this is not a rejection and nothing failed" in RESUME_CONTINUE_NUDGE
    # Cause-neutrality guards: no user attribution.
    assert "by the user" not in RESUME_CONTINUE_NUDGE
    assert "The user paused" not in RESUME_CONTINUE_NUDGE
    assert "call complete_step when it is done" in RESUME_CONTINUE_NUDGE
    assert (
        "Do not ask the user how to proceed unless you genuinely need input"
    ) in RESUME_CONTINUE_NUDGE


def test_retry_note_frames_a_provider_failure_not_a_human_pause() -> None:
    assert RESUME_RETRY_NUDGE == (
        "The previous provider call failed and the engine is retrying it — this is "
        "not a rejection and nothing you did failed. Anything you already said above "
        "has reached the user's screen; continue from there rather than repeating it. "
        "Continue the step from where you left off and call complete_step when it is "
        "done. Do not ask the user how to proceed unless you genuinely need input you "
        "do not already have."
    )
    # The pause framing must not leak into the retry copy.
    assert "The run was paused" not in RESUME_RETRY_NUDGE
    assert "nothing failed." not in RESUME_RETRY_NUDGE


def test_continuation_nudge_selects_the_note_by_cause() -> None:
    assert continuation_nudge("human") == RESUME_CONTINUE_NUDGE
    assert continuation_nudge("provider_retry") == RESUME_RETRY_NUDGE
