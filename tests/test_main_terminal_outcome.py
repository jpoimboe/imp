from __future__ import annotations

from unittest.mock import patch

import pytest

from imp.cli import main
from imp.engine.executor import RunResult


class _Events:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def read(self) -> list[dict]:
        return list(self._events)


class _Change:
    id = "2026-06-12-run-x"
    title = ""
    base_branch = "master"
    materialized = False                # the real render-seam value (draft never reloaded)
    state_dir = "/tmp/imp-test-state"   # never touched: State.load is patched

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def event_log(self) -> _Events:
        return _Events(self._events)


class _State:
    def __init__(self, status: str, step: str | None = None) -> None:
        self.status = status
        self.step = step


def test_merge_blocked_outcome_emits_no_paused_summary() -> None:
    quest = _Change([{"type": "merge_blocked",
                       "payload": {"reason": "refused", "base": "master",
                                   "detail": "error: Aborting"}}])
    with patch("imp.engine.state.State") as State, \
         patch.object(main, "_emit_merge_blocked") as mb, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("merge_blocked")
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=None)
    assert rc == 1
    mb.assert_called_once()          # the friendly ⚠ block still prints
    ps.assert_not_called()           # the redundant second pause line is gone


def test_non_merge_pause_still_emits_paused_summary() -> None:
    quest = _Change([])
    with patch("imp.engine.state.State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step=None)
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=None)
    assert rc == 1
    ps.assert_called_once()          # ordinary pauses keep the summary line


def test_emit_merge_blocked_worktree_dirty_three_part_block(capsys) -> None:
    from imp.cli import palette as pal
    with patch.object(main, "should_use_color", lambda stream: True):
        main._emit_merge_blocked(_Change([]), "worktree_dirty")
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert lines[0] == pal.ansi("merge blocked: uncommitted work in the worktree", "red")
    assert lines[1] == "Commit it, then `imp resume 2026-06-12-run-x`."
    assert lines[2] == pal.ansi("2026-06-12-run-x", "wordmark")   # bare gold id, untitled
    assert "⚠" not in out                                    # no ⚠ glyph
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_emit_merge_blocked_refused_summary_and_git_said(capsys) -> None:
    with patch.object(main, "should_use_color", lambda stream: False):
        main._emit_merge_blocked(_Change([]), "refused", detail="error: Aborting\n")
    out = capsys.readouterr().out
    assert "merge blocked: git refused to merge imp/2026-06-12-run-x into master" in out
    assert "Commonly: uncommitted changes on `master`" in out
    assert "Resolve, then `imp resume 2026-06-12-run-x`." in out
    assert "git said: error: Aborting" in out          # detail.strip() — trailing newline gone
    assert "\x1b[" not in out                           # plain under NO_COLOR / non-TTY


def test_emit_merge_blocked_conflict_body_order_note_before_git_said(capsys) -> None:
    with patch.object(main, "should_use_color", lambda stream: False):
        main._emit_merge_blocked(_Change([]), "conflict",
                                 detail="CONFLICT (content): f.txt", note="tried 3 passes")
    out = capsys.readouterr().out
    assert "merge blocked: imp/2026-06-12-run-x doesn't auto-merge into master" in out
    assert "imp can't resolve conflicts in this version" in out
    assert "`.imp/worktrees/2026-06-12-run-x` can be removed" in out
    assert out.index("tried 3 passes") < out.index("git said: CONFLICT (content): f.txt")
    assert "resume: imp resume" not in out             # no standalone resume line


def test_emit_merge_blocked_fallback_couldnt_merge(capsys) -> None:
    with patch.object(main, "should_use_color", lambda stream: False):
        main._emit_merge_blocked(_Change([]), "some_unknown_reason")
    out = capsys.readouterr().out
    assert "merge blocked: couldn't merge" in out
    assert "Resolve, then `imp resume 2026-06-12-run-x`." in out
    assert "git said:" not in out                       # fallback carries no git detail



def test_handle_terminal_outcome_does_not_reprint_script_detail(capsys) -> None:
    events = [
        {"type": "script_blocked",
         "payload": {"step": "verify", "stderr": "err-detail", "stdout": "out-detail", "exit": 3}},
    ]
    quest = _Change(events)
    with patch("imp.engine.state.State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step="verify")
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=None)
    out = capsys.readouterr().out
    assert "err-detail" not in out       # the exit recap no longer reprints script detail
    assert "out-detail" not in out
    ps.assert_called_once()              # only the paused summary is emitted
    assert rc == 1


def test_merge_blocked_outcome_forwards_exhaustion_note() -> None:
    note = "imp attempted 3 recovery passes and the merge still failed."
    quest = _Change([{"type": "merge_blocked",
                       "payload": {"reason": "conflict", "base": "master",
                                   "detail": "merge conflict", "note": note}}])
    with patch("imp.engine.state.State") as State, \
         patch.object(main, "_emit_merge_blocked") as mb, \
         patch.object(main, "_emit_paused_summary"):
        State.load.return_value = _State("merge_blocked")
        main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=None)
    mb.assert_called_once()
    assert mb.call_args.args[3] == note     # the note reached _emit_merge_blocked as the note arg


def test_emit_paused_summary_renders_agent_error_block(capsys) -> None:
    quest = _Change([
        {"type": "step_started", "payload": {"phase": "reviewing plan"}},
        {"type": "error",
         "payload": {"detail": {"last_agent_text":
                                "API Error 400 — tool use concurrency\ntrace"}}},
        {"type": "workflow_paused",
         "payload": {"reason": "terminated", "kind": "terminated"}},
    ])
    main._emit_paused_summary(quest)
    out = capsys.readouterr().out
    assert not out.startswith("\n")                    # no leading blank
    assert "agent error: API Error 400 — tool use concurrency" in out
    assert "\ntrace\n" in out                          # remaining cause line → body
    assert "terminated:" not in out                    # raw token never shown
    assert " at reviewing plan" not in out             # locator dropped
    assert "resume: imp resume" not in out             # error block has no resume line
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_emit_paused_summary_api_pause_renders_red_block_not_gold(capsys) -> None:
    from imp.cli import palette as pal
    quest = _Change([
        {"type": "step_started", "payload": {"phase": "reviewing plan"}},
        {"type": "error",
         "payload": {"detail": {"last_agent_text": "API Error 400 — tool use concurrency"}}},
        {"type": "workflow_paused", "payload": {"reason": "api", "kind": "api"}},
    ])
    with patch.object(main, "should_use_color", lambda stream: True):
        main._emit_paused_summary(quest)
    out = capsys.readouterr().out
    assert pal.ansi("API error: API Error 400 — tool use concurrency", "red") in out
    assert "resume: imp resume" not in out          # error block carries no resume line
    assert "2026-06-12-run-x" in out                # bare-id identity line (untitled)
    assert out.endswith("\n\n")


def test_emit_paused_summary_error_block_plain_under_no_color(capsys) -> None:
    quest = _Change([
        {"type": "error", "payload": {"detail": {"last_agent_text": "API Error 400"}}},
        {"type": "workflow_paused", "payload": {"reason": "api", "kind": "api"}},
    ])
    with patch.object(main, "should_use_color", lambda stream: False):
        main._emit_paused_summary(quest)
    out = capsys.readouterr().out
    assert "API error: API Error 400" in out
    assert "\x1b[" not in out                        # no SGR under NO_COLOR / non-TTY
    assert "resume: imp resume" not in out


def test_emit_paused_summary_interrupt_renders_gold_block(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    quest = _Change([
        {"type": "workflow_paused",
         "payload": {"reason": "interrupt", "interrupt_reason": "interrupt"}},
    ])
    main._emit_paused_summary(quest)
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert lines[0] == "interrupt"  # gold headline, own line (plain here)
    assert lines[1] == "2026-06-12-run-x"                      # identity, bare id (untitled)
    assert lines[2] == "resume: imp resume 2026-06-12-run-x"   # resume line kept
    assert "(unnamed)" not in out
    assert "· interrupt" not in out                            # no crammed spine tail
    assert not out.startswith("\n")                            # no leading blank
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_status_spine_prompt_fallback_dim_and_prefixed_color() -> None:
    from imp.cli import palette as pal
    line = main._status_spine("z1", "", "shelved", "wordmark", color=True,
                              intent="add a dark mode toggle")
    # the opening-prompt fallback is the dim, `> `-prefixed middle segment
    assert pal.ansi("> add a dark mode toggle", "dim") in line
    assert "(unnamed)" not in line


def test_status_spine_prompt_fallback_flattens_and_caps_at_70() -> None:
    line = main._status_spine("z1", "   ", "shelved", "wordmark", color=False,
                              intent="add a\n   dark   mode\ttoggle " + "y" * 80)
    # flattened to single spaces, then "> " (2) + 67 chars + "…" = 70 display cols
    body = "add a dark mode toggle " + "y" * 80
    assert "> " + body[:67] + "…" in line
    assert "> " + body[:68] not in line


def test_status_spine_unnamed_has_no_prompt_prefix() -> None:
    line = main._status_spine("z1", "", "shelved", "wordmark", color=False,
                              intent=None)
    assert line == "z1 · (unnamed) · shelved"
    assert "> (unnamed)" not in line


def test_status_spine_title_unchanged_when_intent_present() -> None:
    # A real title wins over the opening prompt: plain, no dim, no `> `.
    line = main._status_spine("z1", "Real title", "shelved", "wordmark",
                              color=False, intent="ignored prompt")
    assert line == "z1 · Real title · shelved"


def test_status_spine_prompt_fallback_no_color_is_plain_and_parseable() -> None:
    line = main._status_spine("z1", "", "shelved", "wordmark", color=False,
                              intent="when I hit save it crashed")
    # NO_COLOR / piped: plain ASCII `> `, no ANSI, dot-delimited spine intact
    assert line == "z1 · > when I hit save it crashed · shelved"
    assert "\x1b[" not in line
    assert line.count(" · ") == 2


def test_status_spine_named_renders_plain_id_title_tail_structure() -> None:
    line = main._status_spine("vahi04", "One-line title", "completed", "green",
                              color=False)
    assert line == "vahi04 · One-line title · completed"


def test_status_spine_titleless_renders_unnamed_middle_segment() -> None:
    line = main._status_spine("vahi04", "", "completed", "green", color=False)
    assert line == "vahi04 · (unnamed) · completed"
    # exactly two separators now: id · (unnamed) · tail
    assert line.count(" · ") == 2


def test_status_spine_colors_id_gold_and_tail_by_role() -> None:
    from imp.cli import palette as pal
    line = main._status_spine("vahi04", "T", "completed", "green", color=True)
    assert line == (f'{pal.ansi("vahi04", "wordmark")} · T · '
                    f'{pal.ansi("completed", "green")}')


def test_clean_block_titled_gold_headline_and_identity() -> None:
    from imp.cli import palette as pal
    lines = main._clean_block("completed", "vahi04", "Real title",
                              color=True, intent="ignored prompt")
    assert lines == [
        pal.ansi("completed", "wordmark"),                       # gold headline, own line
        f'{pal.ansi("vahi04", "wordmark")} · Real title',        # gold id · plain title
    ]


def test_clean_block_untitled_is_bare_gold_id() -> None:
    from imp.cli import palette as pal
    lines = main._clean_block("completed", "vahi04", "", color=True, intent=None)
    assert lines == [pal.ansi("completed", "wordmark"),
                     pal.ansi("vahi04", "wordmark")]             # bare gold id, no ` · `


def test_emit_completed_merged_prints_gold_block_no_green(
    monkeypatch, capsys,
) -> None:
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    main._emit_completed(_Change([]), merged=True)
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert lines[0] == pal.ansi("merged into master", "wordmark")   # gold headline, own line
    assert lines[1] == pal.ansi("2026-06-12-run-x", "wordmark")  # gold identity, bare id
    assert pal.ansi("merged into master", "green") not in out       # no green anywhere
    assert " · " not in out                                         # no crammed id·title·tail spine
    assert "✓" not in out
    assert not out.startswith("\n")                                 # no leading blank
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_emit_completed_non_merge_headline_is_imp_branch_gold(monkeypatch, capsys) -> None:
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    quest = _Change([])
    assert quest.materialized is False                          # the real render seam
    main._emit_completed(quest, merged=False)
    out = capsys.readouterr().out
    lines = out.split("\n")
    # Headline names the quest branch imp/<id>, NOT the base branch, even though
    # the client quest object is still materialized=False.
    assert lines[0] == pal.ansi("done · commits on imp/2026-06-12-run-x", "wordmark")
    assert "commits on master" not in out                       # never the base branch
    assert pal.ansi("done · commits on imp/2026-06-12-run-x", "green") not in out  # not green
    assert lines[1] == pal.ansi("2026-06-12-run-x", "wordmark")  # gold identity
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_handle_terminal_outcome_threads_deliberate_leave_trigger() -> None:
    class _Evt:
        def is_set(self) -> bool:
            return True

    class _Coord:
        leave_reason = "ctrl-d"
        escalation_event = _Evt()

    quest = _Change([])
    with patch("imp.engine.state.State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step=None)
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=_Coord())
    assert rc == 1
    ps.assert_called_once()
    assert ps.call_args.args[1] == "ctrl-d"   # the trigger reached the summary


def test_handle_terminal_outcome_reads_intent_from_archive(monkeypatch) -> None:
    quest = _Change([])
    quest.control_root = main.Path("/tmp/imp-no-such-worktree-xyz")
    archive_blob = (
        b'{"type": "user_message", "payload": {"subtype": "intent", '
        b'"content": [{"type": "text", "text": "resolve me from archive"}]}}\n'
        b'{"type": "runner_warning", "payload": {"message": "a"}}\n'
        b'{"type": "runner_warning", "payload": {"message": "b"}}\n'
        b'{"type": "step_started", "payload": {"step": "s"}}\n'
    )
    monkeypatch.setattr(
        "imp.engine.archive.read.read_events",
        lambda control_root, quest_id: archive_blob)
    captured: dict = {}
    monkeypatch.setattr(
        main, "_emit_completed",
        lambda ch, *, merged, intent: captured.update(intent=intent))
    rc = main._handle_terminal_outcome(quest, RunResult.COMPLETED, coord=None)
    assert rc == 0
    assert captured["intent"] == "resolve me from archive"


class _CtrlDCoord:
    interrupted = True
    leave_reason = "ctrl-d"

    class _Evt:
        def is_set(self) -> bool:
            return True

    escalation_event = _Evt()


def test_ctrl_d_pause_retains_caret_above_banner(monkeypatch, capsys) -> None:
    # An in-session Ctrl-D leave prints the retained `> ` caret immediately
    # above the (here mocked) pause banner.
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    quest = _Change([])
    with patch("imp.engine.state.State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step=None)
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, _CtrlDCoord())
    assert rc == 1
    ps.assert_called_once()
    assert capsys.readouterr().out == "> Ctrl-D\n"   # keystroke shadow only (banner mocked out)


class _SigtermCoord:
    leave_reason = "sigterm"

    class _Evt:
        def is_set(self) -> bool:
            return True

    escalation_event = _Evt()


def test_sigterm_leave_prints_no_caret_and_paused_display_head(monkeypatch, capsys) -> None:
    # A signal leave renders NO caret line and a `paused · SIGTERM` head above the
    # unchanged identity and resume lines.
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    quest = _Change([])
    with patch("imp.engine.state.State") as State:
        State.load.return_value = _State("paused", step=None)
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, _SigtermCoord())
    assert rc == 1
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert lines[0] == "paused · SIGTERM"                       # state-first head, no caret above
    assert not out.startswith("> ")                            # signals get no caret shadow
    assert lines[1] == "2026-06-12-run-x"                      # identity, bare id (untitled)
    assert lines[2] == "resume: imp resume 2026-06-12-run-x"   # resume line unchanged


def test_emit_completed_titleless_shows_prompt_fallback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_Change([]), merged=False, intent="add a dark mode toggle")
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert lines[0] == "done · commits on imp/2026-06-12-run-x"      # headline on its own line
    assert lines[1] == "2026-06-12-run-x · > add a dark mode toggle"  # identity, prompt fallback
    assert "(unnamed)" not in out
    assert "\x1b[" not in out


def test_handle_terminal_outcome_forwards_opening_intent(monkeypatch) -> None:
    events = [
        {"type": "user_message",
         "payload": {"subtype": "intent",
                     "content": [{"type": "text", "text": "add a dark mode toggle"}]}},
        {"type": "step_started", "payload": {"step": "s", "kind": "agent"}},
    ]
    quest = _Change(events)
    quest.control_root = main.Path("/tmp/imp-no-such-worktree-xyz")
    captured: dict = {}
    monkeypatch.setattr(
        main, "_emit_completed",
        lambda ch, *, merged, intent: captured.update(intent=intent))
    rc = main._handle_terminal_outcome(quest, RunResult.COMPLETED, coord=None)
    assert rc == 0
    assert captured["intent"] == "add a dark mode toggle"


def test_emit_merge_blocked_titleless_shows_prompt_fallback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    quest = _Change([{"type": "user_message",
                      "payload": {"subtype": "intent",
                                  "content": [{"type": "text", "text": "fix the crash"}]}}])
    main._emit_merge_blocked(quest, "worktree_dirty")
    out = capsys.readouterr().out
    assert "merge blocked: uncommitted work in the worktree" in out
    assert "2026-06-12-run-x · > fix the crash" in out   # identity line, prompt fallback
    assert "(unnamed)" not in out


def test_emit_paused_summary_titleless_shows_prompt_fallback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    quest = _Change([
        {"type": "user_message",
         "payload": {"subtype": "intent",
                     "content": [{"type": "text", "text": "add a dark mode toggle"}]}},
        {"type": "workflow_paused",
         "payload": {"reason": "interrupt", "interrupt_reason": "interrupt"}},
    ])
    main._emit_paused_summary(quest)
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert lines[0] == "interrupt"                                    # gold headline, own line
    assert lines[1] == "2026-06-12-run-x · > add a dark mode toggle"  # identity, prompt fallback
    assert lines[2] == "resume: imp resume 2026-06-12-run-x"         # resume line kept
    assert "(unnamed)" not in out


def test_non_ctrl_d_pause_prints_no_caret(monkeypatch, capsys) -> None:
    # A pause that was not a Ctrl-D prompt leave retains no caret.
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    quest = _Change([])
    with patch("imp.engine.state.State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step=None)
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=None)
    assert rc == 1
    ps.assert_called_once()
    assert capsys.readouterr().out == ""


def test_handle_terminal_outcome_merged_event_yields_merged_true(
    monkeypatch, tmp_path,
) -> None:
    # The worktree dir is still present at render time (the queued / cross-process
    # case). Pre-fix, the wt_path probe would read merged=False; the event wins now.
    wt = tmp_path / ".imp" / "worktrees" / "2026-06-12-run-x"
    wt.mkdir(parents=True)
    events = [
        {"type": "quest_merged", "payload": {"base": "master", "commit": "abc123"}},
        {"type": "step_started", "payload": {"step": "s", "kind": "agent"}},
    ]
    quest = _Change(events)
    quest.control_root = tmp_path
    captured: dict = {}
    monkeypatch.setattr(
        main, "_emit_completed",
        lambda ch, *, merged, intent: captured.update(merged=merged))
    rc = main._handle_terminal_outcome(quest, RunResult.COMPLETED, coord=None)
    assert rc == 0
    assert captured["merged"] is True


def test_handle_terminal_outcome_no_merged_event_yields_merged_false(
    monkeypatch,
) -> None:
    events = [
        {"type": "workflow_completed", "payload": {}},
        {"type": "step_started", "payload": {"step": "s", "kind": "agent"}},
    ]
    quest = _Change(events)
    captured: dict = {}
    monkeypatch.setattr(
        main, "_emit_completed",
        lambda ch, *, merged, intent: captured.update(merged=merged))
    rc = main._handle_terminal_outcome(quest, RunResult.COMPLETED, coord=None)
    assert rc == 0
    assert captured["merged"] is False


_END_OF_RUN_WARNINGS = ["cache read failed", "cache read failed",
                        "retrying after API 529"]


def _merged_run_events(warnings: list[str]) -> list[dict]:
    from tests._event_fixtures import ev
    events = [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="merging", kind="agent", phase="integration",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="a",
           raw={"content": [{"type": "text", "text": "done"}]}, usage={}),
    ]
    events += [ev(f"w{i}", "runner_warning", agent_id="a", message=m)
               for i, m in enumerate(warnings)]
    events += [
        ev("4", "step_completed", step="merging", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("5", "quest_merged", base="master"),
    ]
    return events


def _end_of_run_output(warnings: list[str], *, tty: bool) -> str:
    """The whole end-of-run sequence into ONE capture stream, as a real run does it:
    the live walk over a merged run, the shared teardown, then the terminal outcome."""
    import contextlib
    import io

    from imp.cli.live_driver import LiveDriver
    from imp.cli.live_status import LiveStatus
    from tests._event_fixtures import _TTY

    events = _merged_run_events(warnings)
    out = _TTY() if tty else io.StringIO()
    status = LiveStatus(out)
    status._width_fn = lambda: 80
    drv = LiveDriver(stream=out, color=False, width=80, status=status)
    for e in events:
        drv.on_event(e)
    drv.finish()
    with contextlib.redirect_stdout(out):
        assert main._handle_terminal_outcome(
            _Change(events), RunResult.COMPLETED, coord=None) == 0
    return out.getvalue()


def test_merged_run_with_warnings_renders_the_warnings_block_once(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    raw = _end_of_run_output(_END_OF_RUN_WARNINGS, tty=False)
    assert raw.count("3 warnings this run") == 1     # was twice: narrative + plain block
    assert "  • cache read failed  (×2)" in raw      # the retained block, shape unchanged
    assert "- cache read failed (×2)" not in raw     # the deleted plain block's shape
    assert raw.endswith("\n\n")                      # exactly one trailing blank
    assert not raw.endswith("\n\n\n")


def _screen_rows(raw: str, *, tty: bool) -> list[str]:
    if not tty:
        return raw.split("\n")
    import pyte

    from impty.screen import TerminalScreen
    from impty.view import lines as screen_lines
    screen = TerminalScreen(80, 24, history=200, ratio=0.5)
    # a real tty's ONLCR turns every \n into \r\n; a StringIO capture does not
    pyte.ByteStream(screen).feed(raw.replace("\n", "\r\n").encode())
    return screen_lines(screen)


@pytest.mark.parametrize("tty", [True, False], ids=["pty", "pipe"])
@pytest.mark.parametrize("warnings", [_END_OF_RUN_WARNINGS, []],
                         ids=["warnings", "no-warnings"])
def test_exactly_one_blank_between_narrative_and_headline(
    monkeypatch, warnings, tty,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    rows = _screen_rows(_end_of_run_output(warnings, tty=tty), tty=tty)
    h = next(i for i, r in enumerate(rows) if r.strip() == "merged into master")
    assert rows[h - 1].strip() == ""      # exactly one blank directly above the headline
    assert rows[h - 2].strip() != ""      # …and not two
    last = "• retrying after API 529" if warnings else "merging · complete"
    assert last in rows[h - 2]            # …and it follows the LAST narrative line
