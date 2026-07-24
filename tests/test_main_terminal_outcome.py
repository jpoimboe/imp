from __future__ import annotations

from unittest.mock import patch

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
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_merge_blocked") as mb, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("merge_blocked")
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=None)
    assert rc == 1
    mb.assert_called_once()          # the friendly ⚠ block still prints
    ps.assert_not_called()           # the redundant second pause line is gone


def test_non_merge_pause_still_emits_paused_summary() -> None:
    quest = _Change([])
    with patch.object(main, "State") as State, \
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
    with patch.object(main, "State") as State, \
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
    with patch.object(main, "State") as State, \
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


def test_emit_completed_non_merge_headline_is_done_gold(monkeypatch, capsys) -> None:
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    main._emit_completed(_Change([]), merged=False)
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert lines[0] == pal.ansi("done · commits on master", "wordmark")   # gold headline
    assert pal.ansi("done · commits on master", "green") not in out       # not green
    assert lines[1] == pal.ansi("2026-06-12-run-x", "wordmark")           # gold identity
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_emit_completed_warnings_render_plain_deduped_list(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(
        _Change([]), merged=True,
        warnings=["clamped to 4", "clamped to 4", "id reused"])
    out = capsys.readouterr().out
    assert "3 warnings this run" in out
    assert "- clamped to 4 (×2)" in out
    assert "- id reused" in out
    assert "(3 warnings)" not in out            # the old count suffix is gone
    assert out.index("2026-06-12-run-x") < out.index("3 warnings this run")  # identity first
    assert out.index("- clamped to 4 (×2)") < out.index("- id reused")  # first-seen order


def test_emit_completed_single_warning_header_is_singular(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_Change([]), merged=False, warnings=["only one"])
    out = capsys.readouterr().out
    assert "1 warning this run" in out
    assert "warnings this run" not in out
    assert "- only one" in out
    assert "(×" not in out                       # no count suffix at a single occurrence


def test_emit_completed_no_warnings_prints_no_block(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_Change([]), merged=False, warnings=[])
    out = capsys.readouterr().out
    assert "warning" not in out
    assert "this run" not in out


def test_handle_terminal_outcome_threads_runner_warning_messages(monkeypatch) -> None:
    events = [
        {"type": "runner_warning", "payload": {"message": "a"}},
        {"type": "runner_warning", "payload": {"message": "a"}},
        {"type": "step_started", "payload": {"step": "s", "kind": "agent"}},
    ]
    quest = _Change(events)
    quest.control_root = main.Path("/tmp/imp-no-such-worktree-xyz")
    captured: dict = {}
    monkeypatch.setattr(
        main, "_emit_completed",
        lambda ch, *, merged, warnings, intent: captured.update(warnings=warnings))
    rc = main._handle_terminal_outcome(quest, RunResult.COMPLETED, coord=None)
    assert rc == 0
    assert captured["warnings"] == ["a", "a"]    # ordered messages, not a count


def test_handle_terminal_outcome_threads_deliberate_leave_trigger() -> None:
    class _Evt:
        def is_set(self) -> bool:
            return True

    class _Coord:
        leave_reason = "ctrl-d"
        escalation_event = _Evt()

    quest = _Change([])
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step=None)
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=_Coord())
    assert rc == 1
    ps.assert_called_once()
    assert ps.call_args.args[1] == "ctrl-d"   # the trigger reached the summary


def test_handle_terminal_outcome_reads_warning_messages_from_archive(monkeypatch) -> None:
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
        main.archive_read, "read_events",
        lambda control_root, quest_id: archive_blob)
    captured: dict = {}
    monkeypatch.setattr(
        main, "_emit_completed",
        lambda ch, *, merged, warnings, intent: captured.update(warnings=warnings, intent=intent))
    rc = main._handle_terminal_outcome(quest, RunResult.COMPLETED, coord=None)
    assert rc == 0
    assert captured["warnings"] == ["a", "b"]
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
    with patch.object(main, "State") as State, \
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
    with patch.object(main, "State") as State:
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
    assert lines[0] == "done · commits on master"                    # headline on its own line
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
        lambda ch, *, merged, warnings, intent: captured.update(intent=intent))
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
    with patch.object(main, "State") as State, \
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
        lambda ch, *, merged, warnings, intent: captured.update(merged=merged))
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
        lambda ch, *, merged, warnings, intent: captured.update(merged=merged))
    rc = main._handle_terminal_outcome(quest, RunResult.COMPLETED, coord=None)
    assert rc == 0
    assert captured["merged"] is False
