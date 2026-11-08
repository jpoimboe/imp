from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.main import _pause_lines
from imp.paths import project_state_dir


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
    assert "(untitled)" not in "\n".join(lines)


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


def test_pause_lines_ctrl_c_leave_head_is_paused_dot_ctrl_c() -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _pause_lines, _PauseInfo
    plain = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo("ctrl-c", None), color=False)
    assert plain == [
        "paused · Ctrl-C",
        "dufa26 · T",
        "resume: imp resume dufa26",
    ]
    colored = _pause_lines("dufa26", "dufa26", title="T",
                           info=_PauseInfo("ctrl-c", None), color=True)
    assert colored[0] == pal.ansi("paused", "wordmark") + pal.ansi(" · Ctrl-C", "dim")


def test_pause_lines_non_leave_head_stays_bare_gold_reason() -> None:
    # Guard: a non-leave interrupt head is unchanged — a bare gold reason, not the
    # `paused · <Display>` leave head.
    from imp.cli import palette as pal
    from imp.cli.main import _pause_lines, _PauseInfo
    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo("interrupt", None), color=True)
    assert lines[0] == pal.ansi("interrupt", "wordmark")
    assert "paused · " not in "\n".join(lines)


@pytest.mark.parametrize(("title", "identity"), [
    ("Add version flag", "qzbscf · Add version flag"),
    ("", "qzbscf"),
], ids=["titled", "untitled"])
def test_paused_summary_renders_the_quests_own_id_and_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    title: str, identity: str,
) -> None:
    from imp.cli.main import _emit_paused_summary
    from imp.engine.quest import Quest

    monkeypatch.setenv("NO_COLOR", "1")
    quest = Quest(id="qzbscf", title=title, base_branch="main",
                  control_root=tmp_path, project_root=tmp_path,
                  quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest")
    _emit_paused_summary(quest, info=None)
    # The bare id is the resume token, never a slugified handle.
    assert capsys.readouterr().out == (
        f"paused\n{identity}\nresume: imp resume qzbscf\n\n")


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
    # Escalation set but no trigger stamped (an /archive-style disposition leave).
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
    from imp.cli.main import _ERROR_LABELS, _PAUSE_QUALIFIER
    from imp.engine.error_policy import _POLICY

    # context_window is a clean `paused · context handoff limit` pause, not a red block.
    missing = set(_POLICY) - set(_ERROR_LABELS) - {"context_window"}
    assert missing == set(), f"unmapped categories: {missing}"
    assert "context_window" not in _ERROR_LABELS
    assert "context_window" in _PAUSE_QUALIFIER
    assert "reauth_needed" in _ERROR_LABELS      # the executor's remap target for auth_terminal


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


@pytest.mark.parametrize("base,label", [
    ("api", "API error"),
    ("terminated", "agent error"),
    ("internal_error", "internal error"),
    ("quota", "quota exceeded"),
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


def test_pause_qualifier_holds_exactly_its_eight_entries() -> None:
    from imp.cli.main import _PAUSE_QUALIFIER, _PauseInfo
    assert _PAUSE_QUALIFIER == {
        "pause": "/pause",
        "ctrl-d": "Ctrl-D",
        "ctrl-c": "Ctrl-C",
        "sigterm": "SIGTERM",
        "sighup": "SIGHUP",
        "sigquit": "SIGQUIT",
        "max_loops": "loop limit",
        "context_window": "context handoff limit",
    }
    for trigger, display in _PAUSE_QUALIFIER.items():
        lines = _pause_lines("dufa26", "dufa26", title="T",
                             info=_PauseInfo(trigger, None), color=False)
        assert lines[0] == f"paused · {display}"


def test_pause_qualifier_derives_from_the_narrative_manual_stop_labels() -> None:
    from imp.cli import narrative as nv
    from imp.cli.main import _PAUSE_QUALIFIER
    assert _PAUSE_QUALIFIER == {**nv.MANUAL_STOP_LABELS, "max_loops": "loop limit",
                                "context_window": "context handoff limit"}
    assert not hasattr(nv, "_HUMAN_INTERRUPT_REASONS")


@pytest.mark.parametrize("cause", [None, "prompt is too long: 210000 tokens > 200000"],
                         ids=["no-cause", "provider-cause"])
def test_pause_lines_context_window_is_gold_handoff_limit_with_resume(
    cause: str | None,
) -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _PauseInfo
    lines = _pause_lines("dufa26", "dufa26", title="T",
                         info=_PauseInfo("context_window", cause), color=True)
    assert lines == [
        pal.ansi("paused", "wordmark") + pal.ansi(" · context handoff limit", "dim"),
        f'{pal.ansi("dufa26", "wordmark")} · T',
        "resume: imp resume dufa26",
    ]
