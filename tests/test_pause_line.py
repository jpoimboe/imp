from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.main import _pause_lines
from imp.paths import project_state_dir
from tests._event_fixtures import ev


def test_pause_lines_named_reasonless_pause_uses_paused_headline() -> None:
    lines = _pause_lines("dufa26", "dufa26", title="Add version flag", color=False)
    assert lines == [
        "paused",
        "dufa26 · Add version flag",
        "resume: imp resume dufa26",
    ]


def test_pause_lines_titleless_pause_shows_bare_id() -> None:
    lines = _pause_lines("dufa26", "dufa26", color=False)
    assert lines == ["paused", "dufa26", "resume: imp resume dufa26"]
    assert "(unnamed)" not in "\n".join(lines)


def test_pause_lines_reason_becomes_the_headline_no_at_locator_no_parens() -> None:
    from imp.cli.main import _PauseInfo
    lines = _pause_lines("dufa26", "dufa26", title="Add version flag",
                         info=_PauseInfo("interrupt", None), color=False)
    assert lines == [
        "interrupt",
        "dufa26 · Add version flag",
        "resume: imp resume dufa26",
    ]
    joined = "\n".join(lines)
    assert " at " not in joined
    assert "(" not in joined


def test_pause_lines_clean_reason_headline_is_gold() -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _PauseInfo
    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo("interrupt", None), color=True)
    assert lines[0] == pal.ansi("interrupt", "wordmark")            # gold headline, own line
    assert lines[1] == f'{pal.ansi("dufa26", "wordmark")} · T'      # gold id · plain title
    assert lines[2] == "resume: imp resume dufa26"                  # resume line stays plain
    assert "⏸" not in lines[0]
    assert "paused" not in lines[0]  # a reason replaces the bare word


def test_pause_lines_error_reason_tail_is_red_id_stays_gold() -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _PauseInfo
    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo("terminated", "API Error 400"), color=True)
    assert lines[0] == pal.ansi("agent error: API Error 400", "red")
    assert lines[-1] == f'{pal.ansi("dufa26", "wordmark")} · T'


def test_pause_lines_crash_pointer_reason_tail_is_red() -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _PauseInfo
    info = _PauseInfo("crash", "see .imp/quests/dufa26/daemon.stderr")
    lines = _pause_lines("dufa26", "dufa26", info=info, color=True)
    assert lines[0] == pal.ansi("crash: see .imp/quests/dufa26/daemon.stderr", "red")


def test_pause_lines_internal_error_reason_tail_is_red() -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _PauseInfo
    info = _PauseInfo("internal_error", "project has no fact 'guidance'")
    lines = _pause_lines("dufa26", "dufa26", info=info, color=True)
    assert lines[0] == pal.ansi("internal error: project has no fact 'guidance'", "red")


def test_pause_lines_keystroke_leave_head_is_paused_with_dim_display() -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _pause_lines, _PauseInfo
    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo("ctrl-d", None), color=True)
    assert lines == [
        pal.ansi("paused", "wordmark") + pal.ansi(" · Ctrl-D", "dim"),
        f'{pal.ansi("dufa26", "wordmark")} · T',
        "resume: imp resume dufa26",
    ]


@pytest.mark.parametrize("reason,display", [
    ("sigterm", "SIGTERM"),
    ("sighup", "SIGHUP"),
    ("sigquit", "SIGQUIT"),
])
def test_pause_lines_signal_leave_head_plain_is_paused_dot_display(
    reason: str, display: str,
) -> None:
    from imp.cli.main import _pause_lines, _PauseInfo
    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo(reason, None), color=False)
    assert lines == [
        f"paused · {display}",
        "dufa26 · T",
        "resume: imp resume dufa26",
    ]


def test_pause_lines_non_leave_head_stays_bare_gold_reason() -> None:
    # Guard: a non-leave interrupt head is unchanged — a bare gold reason, not the
    # `paused · <Display>` leave head.
    from imp.cli import palette as pal
    from imp.cli.main import _pause_lines, _PauseInfo
    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo("interrupt", None), color=True)
    assert lines[0] == pal.ansi("interrupt", "wordmark")
    assert "paused · " not in "\n".join(lines)


def test_pause_facts_unnamed_change_shows_bare_id(tmp_path: Path) -> None:
    from imp.cli.main import _pause_facts
    from imp.engine.quest import Quest

    quest = Quest(id="qzbscf", title="", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest")
    name, title, token, _reason, _opening = _pause_facts(quest)
    assert name == "qzbscf"
    assert token == "qzbscf"
    assert title == ""


def test_pause_facts_named_change_shows_bare_id(tmp_path: Path) -> None:
    from imp.cli.main import _pause_facts
    from imp.engine.quest import Quest

    quest = Quest(id="qzbscf", title="Add version flag",
                    base_branch="main", control_root=tmp_path,
                    project_root=tmp_path,
                    quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest")
    name, title, token, _reason, _opening = _pause_facts(quest)
    assert name == "qzbscf"          # the bare id, not a slugified handle
    assert token == "qzbscf"
    assert title == "Add version flag"


def test_pause_reason_interrupt_has_no_cause() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    # interrupt_reason wins over reason (mirrors _h_workflow_paused); distinct
    # values make that precedence falsifiable.
    reason = _pause_reason([
        ev("1", "workflow_paused", reason="terminated", interrupt_reason="interrupt"),
    ])
    assert reason == _PauseInfo("interrupt", None)


def test_pause_reason_interrupt_after_prior_terminated_pause_has_no_stale_cause() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    # Cumulative log across resume: a terminated pause, then a fresh invocation
    # that the user ctrl-c'd. The interrupt pause's own segment has no error, so
    # the stale terminated cause must NOT leak onto the interrupt headline.
    reason = _pause_reason([
        ev("1", "error", category="terminated",
           detail={"last_agent_text": "API Error 400 — tool use concurrency"}),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
        ev("3", "workflow_paused", reason="interrupt", interrupt_reason="interrupt"),
    ])
    assert reason == _PauseInfo("interrupt", None)


def test_pause_reason_none_when_no_pause_event() -> None:
    from imp.cli.main import _pause_reason

    assert _pause_reason([ev("1", "quest_created", quest_id=1, title="x")]) is None


def test_pause_reason_internal_error_falls_back_to_full_message() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    # An internal-error pause has no last_agent_text; the cause must come from
    # the error payload's message (the full multi-line text).
    reason = _pause_reason([
        ev("1", "error", category="internal", kind="PromptRenderError",
           message="project has no fact 'guidance'\n  at line 3 of template"),
        ev("2", "workflow_paused", reason="internal_error", kind="PromptRenderError"),
    ])
    assert reason == _PauseInfo(
        "internal_error", "project has no fact 'guidance'\n  at line 3 of template")


def test_pause_reason_last_agent_text_wins_over_message() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    # When both are present, detail.last_agent_text takes precedence.
    reason = _pause_reason([
        ev("1", "error", category="terminated",
           message="internal fallback message",
           detail={"last_agent_text": "API Error 400 — tool use concurrency"}),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
    ])
    assert reason == _PauseInfo("terminated", "API Error 400 — tool use concurrency")


def test_pause_reason_bare_reason_when_error_has_no_cause_fields() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    # An error with neither last_agent_text nor message yields the bare reason.
    reason = _pause_reason([
        ev("1", "error", category="internal", kind="ValueError"),
        ev("2", "workflow_paused", reason="internal_error", kind="ValueError"),
    ])
    assert reason == _PauseInfo("internal_error", None)


def test_pause_reason_resumed_after_paused_returns_stale_sentinel() -> None:
    from imp.cli.main import _pause_reason, _PauseBanner

    # A pause, then a resume with no new pause in the latest segment: the stored
    # reason is a fossil and must not be replayed as the headline.
    reason = _pause_reason([
        ev("1", "workflow_paused", reason="terminated", kind="terminated"),
        ev("2", "workflow_resumed"),
    ])
    assert reason is _PauseBanner.STALE


def test_pause_reason_paused_after_resumed_returns_real_reason() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    # Pause -> resume -> pause again: the latest segment DID end with a pause,
    # so its real reason is the headline (not the stale sentinel).
    reason = _pause_reason([
        ev("1", "workflow_paused", reason="terminated", kind="terminated"),
        ev("2", "workflow_resumed"),
        ev("3", "workflow_paused", reason="interrupt", interrupt_reason="interrupt"),
    ])
    assert reason == _PauseInfo("interrupt", None)


def test_pause_reason_resumed_before_any_pause_is_not_stale() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    # A resume that precedes every pause must not mark a later pause stale.
    reason = _pause_reason([
        ev("1", "workflow_resumed"),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
    ])
    assert reason == _PauseInfo("terminated", None)


def test_pause_reason_resumed_with_no_prior_pause_is_stale() -> None:
    from imp.cli.main import _pause_reason, _PauseBanner

    # A parked draft has conversation_saved but never workflow_paused. Its
    # resume dying before any event (e.g. a workflow LoadError at daemon
    # startup) must still read as an unexpected death, not a mute banner.
    reason = _pause_reason([
        ev("1", "awaiting_user_input", prompt=""),
        ev("2", "conversation_saved"),
        ev("3", "workflow_resumed"),
    ])
    assert reason is _PauseBanner.STALE


def test_pause_reason_conversation_saved_after_resume_is_not_stale() -> None:
    from imp.cli.main import _pause_reason

    # A resumed draft that re-parks cleanly ends its segment with
    # conversation_saved — that is a clean end, not an unexpected death.
    reason = _pause_reason([
        ev("1", "conversation_saved"),
        ev("2", "workflow_resumed"),
        ev("3", "conversation_saved"),
    ])
    assert reason is None


def test_pause_facts_stale_with_crash_stderr_renders_unexpected_death_banner(
    tmp_path: Path,
) -> None:
    import json

    from imp.cli.main import _pause_facts, _PauseInfo
    from imp.engine.quest import Quest

    quest_dir = project_state_dir(tmp_path) / "quests" / "mona44"
    quest_dir.mkdir(parents=True)
    events = [
        ev("1", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
        ev("3", "workflow_resumed"),
    ]
    (quest_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    # A genuine death: the daemon left a traceback in its stderr.
    (quest_dir / "daemon.stderr").write_text(
        "Traceback (most recent call last):\n  ...\nRuntimeError: boom\n")
    quest = Quest(id="mona44", title="", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    quest_dir=quest_dir)
    name, _title, token, reason, _opening = _pause_facts(quest)
    assert name == "mona44"
    assert token == "mona44"
    assert reason == _PauseInfo("crash", f"see {quest_dir / 'daemon.stderr'}")


def test_pause_facts_stale_clean_detach_renders_calm_interrupt(
    tmp_path: Path,
) -> None:
    import json

    from imp.cli.main import _pause_facts, _PauseInfo
    from imp.engine.quest import Quest

    quest_dir = project_state_dir(tmp_path) / "quests" / "mona44"
    quest_dir.mkdir(parents=True)
    # Same resumed-without-pause STALE event shape as a crash...
    events = [
        ev("1", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
        ev("3", "workflow_resumed"),
    ]
    (quest_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    # ...but daemon.stderr holds only benign INFO chatter — a clean detach.
    (quest_dir / "daemon.stderr").write_text("Using bundled Claude Code CLI\n")
    quest = Quest(id="mona44", title="", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    quest_dir=quest_dir)
    _name, _title, _token, reason, _opening = _pause_facts(quest)
    assert reason == _PauseInfo("interrupt", None)


def test_pause_facts_stale_empty_stderr_renders_calm_interrupt(tmp_path: Path) -> None:
    import json

    from imp.cli.main import _pause_facts, _PauseInfo
    from imp.engine.quest import Quest

    # daemon.stderr present but EMPTY (0 bytes): no crash traceback, so a clean
    # detach — the calm interrupt reason, no pointer (the empty-read path,
    # distinct from the missing-file OSError path).
    quest_dir = project_state_dir(tmp_path) / "quests" / "mona44"
    quest_dir.mkdir(parents=True)
    events = [
        ev("1", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
        ev("3", "workflow_resumed"),
    ]
    (quest_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    (quest_dir / "daemon.stderr").write_bytes(b"")
    quest = Quest(id="mona44", title="", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    quest_dir=quest_dir)
    _name, _title, _token, reason, _opening = _pause_facts(quest)
    assert reason == _PauseInfo("interrupt", None)


def test_pause_facts_deliberate_leave_renders_trigger_over_stale(tmp_path: Path) -> None:
    import json

    from imp.cli.main import _pause_facts, _PauseInfo
    from imp.engine.quest import Quest

    # Event log that, WITHOUT a deliberate-leave signal, reads as STALE (a pause
    # followed by a resume) — the daemon-derived path would render the
    # unexpected-death banner. The stamped trigger must win instead.
    quest_dir = project_state_dir(tmp_path) / "quests" / "krol99"
    quest_dir.mkdir(parents=True)
    events = [
        ev("1", "step_started", step="task-lead", kind="agent", phase="implement"),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
        ev("3", "workflow_resumed"),
    ]
    (quest_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    quest = Quest(id="krol99", title="Leave now", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    quest_dir=quest_dir)

    name, title, token, reason, _opening = _pause_facts(quest, leave_trigger="sigterm")
    assert name == "krol99"
    assert title == "Leave now"          # title carried on the deliberate-leave path
    assert token == "krol99"
    assert reason == _PauseInfo("sigterm", None)  # the client's trigger, NOT a crash


def test_pause_facts_returns_the_opening_prompt(tmp_path: Path) -> None:
    import json

    from imp.cli.main import _pause_facts, _PauseInfo
    from imp.engine.quest import Quest

    quest_dir = project_state_dir(tmp_path) / "quests" / "opn11"
    quest_dir.mkdir(parents=True)
    events = [
        ev("1", "user_message", subtype="intent",
           content=[{"type": "text", "text": "add a dark mode toggle"}]),
        ev("2", "workflow_paused", reason="interrupt", interrupt_reason="interrupt"),
    ]
    (quest_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    quest = Quest(id="opn11", title="", base_branch="main",
                  control_root=tmp_path, project_root=tmp_path,
                  quest_dir=quest_dir)
    _name, _title, _token, reason, opening = _pause_facts(quest)
    assert opening == "add a dark mode toggle"
    assert reason == _PauseInfo("interrupt", None)


def test_leave_trigger_returns_reason_only_when_escalated_and_stamped() -> None:
    from imp.cli.main import _leave_trigger

    class _Evt:
        def __init__(self, is_set: bool) -> None:
            self._set = is_set

        def is_set(self) -> bool:
            return self._set

    class _Coord:
        def __init__(self, reason: str | None, evt: object | None) -> None:
            self.leave_reason = reason
            self.escalation_event = evt

    # A deliberate leave: escalation set AND a stamped trigger.
    assert _leave_trigger(_Coord("ctrl-d", _Evt(True))) == "ctrl-d"
    # Escalation set but no trigger stamped (a /save-style disposition leave).
    assert _leave_trigger(_Coord(None, _Evt(True))) is None
    # A trigger present but escalation not fired (defensive: not a completed leave).
    assert _leave_trigger(_Coord("sigterm", _Evt(False))) is None
    # No escalation event at all, and the None coord used by non-interactive paths.
    assert _leave_trigger(_Coord("sigterm", None)) is None
    assert _leave_trigger(None) is None


def test_daemon_stderr_has_crash_detects_traceback(tmp_path: Path) -> None:
    from imp.cli.main import _daemon_stderr_has_crash

    p = tmp_path / "daemon.stderr"
    p.write_text(
        "Traceback (most recent call last):\n"
        '  File "runner.py", line 10, in <module>\n'
        "RuntimeError: boom\n")
    assert _daemon_stderr_has_crash(p) is True


def test_daemon_stderr_has_crash_false_for_benign_info_only(tmp_path: Path) -> None:
    # A clean run leaves only benign provider INFO chatter — non-empty, no crash.
    from imp.cli.main import _daemon_stderr_has_crash

    p = tmp_path / "daemon.stderr"
    p.write_text("Using bundled Claude Code CLI\n")
    assert _daemon_stderr_has_crash(p) is False


def test_daemon_stderr_has_crash_false_when_file_absent(tmp_path: Path) -> None:
    from imp.cli.main import _daemon_stderr_has_crash

    assert _daemon_stderr_has_crash(tmp_path / "daemon.stderr") is False


def test_error_labels_cover_every_policy_category() -> None:
    from imp.cli.main import _ERROR_LABELS
    from imp.engine.error_policy import _POLICY

    missing = set(_POLICY) - set(_ERROR_LABELS)
    assert missing == set(), f"unmapped categories: {missing}"


def test_error_labels_map_tokens_to_user_facing_prefixes() -> None:
    from imp.cli.main import _ERROR_LABELS

    # A category renders its human prefix, never the raw internal token.
    assert _ERROR_LABELS["internal_error"] == "internal error"
    assert _ERROR_LABELS["api"] == "API error"
    assert _ERROR_LABELS["terminated"] == "agent error"
    assert _ERROR_LABELS["crash"] == "crash"


def test_error_block_red_head_plain_body_gold_id_title() -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _error_block

    lines = _error_block("agent error: boom", ["line two", "line three"],
                         "dufa26", "My title", color=True)
    assert lines == [
        pal.ansi("agent error: boom", "red"),
        "line two",
        "line three",
        f'{pal.ansi("dufa26", "wordmark")} · My title',
    ]


def test_error_block_untitled_drops_title_and_body_when_empty() -> None:
    from imp.cli.main import _error_block

    lines = _error_block("crash: see x", [], "dufa26", "", color=False)
    assert lines == ["crash: see x", "dufa26"]
    assert lines[-1].count(" · ") == 0


def test_pause_reason_awaiting_user_has_no_reason() -> None:
    from imp.cli.main import _pause_reason

    # A genuine your-turn park surfaces no reason: the trailing status line is
    # "<id> · paused" (gold) with no "at <phase>" locator and no parenthesized suffix.
    reason = _pause_reason([
        ev("1", "step_started", step="coder", kind="agent", phase="implement"),
        ev("2", "workflow_paused", reason="awaiting_user"),
    ])
    assert reason is None


def test_pause_reason_carries_full_last_agent_text_not_first_line_only() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    reason = _pause_reason([
        ev("1", "step_started", step="plan-reviewer", kind="agent",
           phase="reviewing plan"),
        ev("2", "error", category="terminated",
           detail={"last_agent_text":
                   "API Error 400 — tool use concurrency\nstack trace line"}),
        ev("3", "workflow_paused", reason="terminated", kind="terminated"),
    ])
    assert reason == _PauseInfo(
        "terminated", "API Error 400 — tool use concurrency\nstack trace line")


@pytest.mark.parametrize("base,label", [
    ("api", "API error"),
    ("terminated", "agent error"),
    ("internal_error", "internal error"),
    ("quota", "quota exceeded"),
    ("context_window", "context limit"),
    ("retry_ceiling", "retries exhausted"),
    ("reauth_needed", "auth failed"),
    ("unknown", "unknown error"),
    ("auth", "auth failed"),
    ("config", "config error"),
    ("content_filter", "content filtered"),
    ("loop", "loop detected"),
    ("network", "network error"),
    ("rate_limit", "rate limited"),
    ("stalled", "stalled"),
    ("agent_output_missing", "no output"),
])
def test_pause_lines_mapped_error_category_renders_red_label_block(
    base: str, label: str,
) -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _pause_lines, _PauseInfo

    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo(base, "boom detail"), color=True)
    assert lines[0] == pal.ansi(f"{label}: boom detail", "red")
    assert lines[-1] == f'{pal.ansi("dufa26", "wordmark")} · T'
    assert not any(ln.startswith("resume:") for ln in lines)


def test_pause_lines_api_category_is_red_not_gold() -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _pause_lines, _PauseInfo

    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo("api", "API Error 400"), color=True)
    assert lines[0] == pal.ansi("API error: API Error 400", "red")
    assert pal.ansi("API error: API Error 400", "wordmark") not in lines[0]


def test_pause_lines_multiline_cause_splits_summary_and_body() -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _pause_lines, _PauseInfo

    info = _PauseInfo(
        "terminated",
        "the coder could not apply the patch\nhunk 1 failed\nhunk 2 failed")
    lines = _pause_lines("dufa26", "dufa26", title="T", info=info, color=True)
    assert lines == [
        pal.ansi("agent error: the coder could not apply the patch", "red"),
        "hunk 1 failed",
        "hunk 2 failed",
        f'{pal.ansi("dufa26", "wordmark")} · T',
    ]


def test_pause_lines_single_line_cause_has_no_body() -> None:
    from imp.cli.main import _pause_lines, _PauseInfo

    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo("api", "API Error 400"), color=False)
    assert lines == ["API error: API Error 400", "dufa26 · T"]


def test_pause_lines_error_with_no_cause_renders_bare_label() -> None:
    from imp.cli.main import _pause_lines, _PauseInfo

    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo("internal_error", None), color=False)
    assert lines == ["internal error", "dufa26 · T"]


def test_pause_lines_error_block_untitled_leaves_bare_gold_id() -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _pause_lines, _PauseInfo

    lines = _pause_lines("dufa26", "dufa26",
                         info=_PauseInfo("api", "boom"), color=True)
    assert lines[-1] == pal.ansi("dufa26", "wordmark")
    assert lines[-1].count(" · ") == 0


def test_pause_reason_routed_pause_drops_recovered_error() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    # A transient error that self-healed (step_started clears it) must NOT leak
    # onto a later engine-routed pause such as max_loops.
    reason = _pause_reason([
        ev("1", "error", category="auth",
           detail={"last_agent_text": "You've hit your session limit · resets 4pm"}),
        ev("2", "step_started", step="verify", kind="agent", phase="verify"),
        ev("3", "step_completed", step="verify"),
        ev("4", "workflow_paused", reason="max_loops", step="verify"),
    ])
    assert reason == _PauseInfo("max_loops", None)


def test_pause_reason_adjacent_error_still_attaches() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    # Regression guard: an error with NO intervening step_started must still
    # attach as the pause's cause.
    reason = _pause_reason([
        ev("1", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("2", "error", category="auth",
           detail={"last_agent_text": "401 Unauthorized — token revoked"}),
        ev("3", "workflow_paused", reason="reauth_needed", kind="auth"),
    ])
    assert reason == _PauseInfo("reauth_needed", "401 Unauthorized — token revoked")


def test_pause_reason_multi_attempt_burst_keeps_only_final_error() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    # In a backoff burst each non-final error is cleared by the next attempt's
    # step_started; only the final failed attempt's error contributes a cause.
    reason = _pause_reason([
        ev("1", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("2", "error", category="network",
           detail={"last_agent_text": "first blip"}),
        ev("3", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("4", "error", category="network",
           detail={"last_agent_text": "final failure"}),
        ev("5", "workflow_paused", reason="retry_ceiling"),
    ])
    assert reason == _PauseInfo("retry_ceiling", "final failure")


def test_pause_reason_burst_final_error_cleared_by_step_started_renders_bare() -> None:
    from imp.cli.main import _pause_reason, _PauseInfo

    # Falsifiable coverage of the step_started-clears clause inside a burst: the
    # final attempt's error is followed by the retry's step_started before the
    # routed pause, so no error survives as the cause. Without the clause the
    # stale "final failure" would leak as "retry_ceiling: final failure".
    reason = _pause_reason([
        ev("1", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("2", "error", category="network",
           detail={"last_agent_text": "first blip"}),
        ev("3", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("4", "error", category="network",
           detail={"last_agent_text": "final failure"}),
        ev("5", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("6", "workflow_paused", reason="retry_ceiling"),
    ])
    assert reason == _PauseInfo("retry_ceiling", None)


def test_pause_lines_max_loops_reads_as_loop_limit() -> None:
    from imp.cli.main import _PauseInfo
    lines = _pause_lines("dufa26", "dufa26", title="Add version flag",
                         info=_PauseInfo("max_loops", None), color=False)
    assert lines == [
        "paused · loop limit",
        "dufa26 · Add version flag",
        "resume: imp resume dufa26",
    ]
    assert "max_loops" not in "\n".join(lines)


def test_pause_lines_max_loops_head_is_gold_paused_dim_qualifier() -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _PauseInfo
    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo("max_loops", None), color=True)
    assert lines[0] == (pal.ansi("paused", "wordmark")
                        + pal.ansi(" · loop limit", "dim"))
    assert lines[1] == f'{pal.ansi("dufa26", "wordmark")} · T'
    assert lines[2] == "resume: imp resume dufa26"


def test_leave_display_keeps_exactly_its_four_triggers() -> None:
    from imp.cli.main import _LEAVE_DISPLAY, _PauseInfo
    assert _LEAVE_DISPLAY == {
        "ctrl-d": "Ctrl-D",
        "sigterm": "SIGTERM",
        "sighup": "SIGHUP",
        "sigquit": "SIGQUIT",
    }
    for trigger, display in _LEAVE_DISPLAY.items():
        lines = _pause_lines("dufa26", "dufa26", title="T",
                             info=_PauseInfo(trigger, None), color=False)
        assert lines[0] == f"paused · {display}"
