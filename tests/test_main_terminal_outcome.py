from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from imp.cli import main
from imp.cli.session_run import _closing_block
from imp.cli.session_terminal import RunWitness
from imp.remote.session.client import StreamEnding


class _Events:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def read(self) -> list[dict]:
        return list(self._events)


class _Change:
    id = "2026-06-12-run-x"
    title = ""
    base_branch = "master"
    seed_opening: str | None = None
    materialized = False                # the real render-seam value (draft never reloaded)
    project_root = "/imp-state/worktrees/2026-06-12-run-x"
    control_root = Path("/imp-state")
    quest_dir = Path("/imp-state/quests/2026-06-12-run-x")
    state_dir = Path("/imp-state/quests/2026-06-12-run-x/state")

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def event_log(self) -> _Events:
        return _Events(self._events)


# No deliberate departure: _leave_trigger reads these two attributes only.
_NO_LEAVE = SimpleNamespace(escalation_event=None, leave_reason=None)


def _witness(*events: dict) -> RunWitness:
    witness = RunWitness()
    for event in events:
        witness.note(event)
    return witness


def test_witnessed_merge_blocked_emits_no_paused_summary() -> None:
    events = [{"type": "merge_blocked",
               "payload": {"reason": "refused", "base": "master",
                           "detail": "error: Aborting"}}]
    with patch.object(main, "_emit_merge_blocked") as mb, \
         patch.object(main, "_emit_paused_summary") as ps:
        rc = _closing_block(
            _Change(events), witness=_witness(*events), ending=StreamEnding.CUT,
            coord=_NO_LEAVE, leave_state={})
    assert rc == 1
    mb.assert_called_once()          # the friendly block still prints
    ps.assert_not_called()           # the redundant second pause line is gone
    assert mb.call_args.args[1:] == ("refused", "error: Aborting", None)


def test_witnessed_pause_emits_the_paused_summary() -> None:
    events = [{"type": "workflow_paused", "payload": {"reason": "interrupt"}}]
    with patch.object(main, "_emit_paused_summary") as ps:
        rc = _closing_block(
            _Change(events), witness=_witness(*events), ending=StreamEnding.CUT,
            coord=_NO_LEAVE, leave_state={})
    assert rc == 130
    ps.assert_called_once()
    assert ps.call_args.kwargs["info"] == main._PauseInfo("interrupt", None)


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
    assert "`/imp-state/worktrees/2026-06-12-run-x` can be removed" in out
    assert out.index("tried 3 passes") < out.index("git said: CONFLICT (content): f.txt")
    assert "resume: imp resume" not in out             # no standalone resume line


def test_emit_merge_blocked_fallback_couldnt_merge(capsys) -> None:
    with patch.object(main, "should_use_color", lambda stream: False):
        main._emit_merge_blocked(_Change([]), "some_unknown_reason")
    out = capsys.readouterr().out
    assert "merge blocked: couldn't merge" in out
    assert "Resolve, then `imp resume 2026-06-12-run-x`." in out
    assert "git said:" not in out                       # fallback carries no git detail


def test_the_closing_block_does_not_reprint_script_detail(capsys) -> None:
    events = [
        {"type": "script_blocked",
         "payload": {"step": "verify", "stderr": "err-detail", "stdout": "out-detail",
                     "exit": 3}},
        {"type": "workflow_paused", "payload": {"reason": "script_blocked"}},
    ]
    with patch.object(main, "_emit_paused_summary") as ps:
        rc = _closing_block(
            _Change(events), witness=_witness(*events), ending=StreamEnding.CUT,
            coord=_NO_LEAVE, leave_state={})
    out = capsys.readouterr().out
    assert "err-detail" not in out       # the exit recap never reprints script detail
    assert "out-detail" not in out
    ps.assert_called_once()              # only the paused summary is emitted
    assert rc == 1


def test_emit_paused_summary_renders_agent_error_block(capsys) -> None:
    quest = _Change([
        {"type": "step_started", "payload": {"phase": "reviewing plan"}},
        {"type": "error",
         "payload": {"detail": {"last_agent_text":
                                "API Error 400 — tool use concurrency\ntrace"}}},
        {"type": "workflow_paused",
         "payload": {"reason": "terminated", "kind": "terminated"}},
    ])
    main._emit_paused_summary(quest, info=main._PauseInfo(
        "terminated", "API Error 400 — tool use concurrency\ntrace"))
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
        main._emit_paused_summary(quest, info=main._PauseInfo(
            "api", "API Error 400 — tool use concurrency"))
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
        main._emit_paused_summary(quest, info=main._PauseInfo("api", "API Error 400"))
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
    main._emit_paused_summary(quest, info=main._PauseInfo("interrupt", None))
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


def test_emit_paused_summary_renders_the_info_it_is_handed_not_the_event_log(
    monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    # The log says `max_loops`; the caller hands it the interrupt it witnessed.
    quest = _Change([{"type": "workflow_paused", "payload": {"reason": "max_loops"}}])
    main._emit_paused_summary(quest, info=main._PauseInfo("interrupt", None))
    out = capsys.readouterr().out
    assert out.split("\n")[0] == "interrupt"
    assert "loop limit" not in out


def test_emit_leave_pause_keystroke_prints_the_caret_shadow_above_the_head(
    monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_leave_pause(_Change([]), "ctrl-d")
    out = capsys.readouterr().out
    assert out == (
        "> Ctrl-D\n"
        "paused · Ctrl-D\n"
        "2026-06-12-run-x\n"
        "resume: imp resume 2026-06-12-run-x\n"
        "\n")


@pytest.mark.parametrize(
    ("trigger", "display"),
    [("sigterm", "SIGTERM"), ("sighup", "SIGHUP"), ("sigquit", "SIGQUIT"),
     ("pause", "/pause")])
def test_emit_leave_pause_non_keystroke_prints_no_caret(
    trigger, display, monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_leave_pause(_Change([]), trigger)
    out = capsys.readouterr().out
    assert out == (
        f"paused · {display}\n"
        "2026-06-12-run-x\n"
        "resume: imp resume 2026-06-12-run-x\n"
        "\n")


def test_status_spine_prompt_fallback_dim_and_unprefixed_color() -> None:
    from imp.cli import palette as pal
    line = main._status_spine("z1", "", "archived", "wordmark", color=True,
                              intent="add a dark mode toggle")
    # the opening-prompt fallback is the dim, unprefixed middle segment
    assert pal.ansi("add a dark mode toggle", "dim") in line
    assert "> " not in line
    assert "(unnamed)" not in line


def test_status_spine_prompt_fallback_flattens_and_caps_at_70() -> None:
    line = main._status_spine("z1", "   ", "archived", "wordmark", color=False,
                              intent="add a\n   dark   mode\ttoggle " + "y" * 80)
    # flattened to single spaces, then 69 chars + "…" = 70 display cols (NAME_CAP)
    body = "add a dark mode toggle " + "y" * 80
    assert "z1 · " + body[:69] + "… · archived" == line


def test_status_spine_unnamed_has_no_prompt_prefix() -> None:
    line = main._status_spine("z1", "", "archived", "wordmark", color=False,
                              intent=None)
    assert line == "z1 · (unnamed) · archived"
    assert "> (unnamed)" not in line


def test_status_spine_title_unchanged_when_intent_present() -> None:
    # A real title wins over the opening prompt: plain, no dim, no `> `.
    line = main._status_spine("z1", "Real title", "archived", "wordmark",
                              color=False, intent="ignored prompt")
    assert line == "z1 · Real title · archived"


def test_status_spine_prompt_fallback_no_color_is_plain_and_parseable() -> None:
    line = main._status_spine("z1", "", "archived", "wordmark", color=False,
                              intent="when I hit save it crashed")
    # NO_COLOR / piped: plain text, no ANSI, dot-delimited spine intact
    assert line == "z1 · when I hit save it crashed · archived"
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


_SHA_A = "4091ab6d1f2e3d4c5b6a7988990011223344ffee"
_SHA_B = "832c04d7aabbccddeeff00112233445566778899"


def _quest(*, quest_id: str = "soro82",
           title: str = "Closing commit list layout") -> _Change:
    """A quest stub with a known id and title for _emit_completed assertions."""
    q = _Change([])
    q.id, q.title = quest_id, title
    return q


def test_commit_lines_are_gray_sha8_two_spaces_then_the_plain_subject() -> None:
    from imp.cli import palette as pal
    lines = main._commit_lines(
        [{"sha": _SHA_A, "subject": "cli: print the commit list"},
         {"sha": _SHA_B, "subject": "test: cover both finish paths"}],
        color=True)
    # two-space indent, then the sha truncated to 8 and gray; the subject carries
    # no styling of its own
    assert lines == ["  " + pal.ansi("4091ab6d", "gray") + "  cli: print the commit list",
                     "  " + pal.ansi("832c04d7", "gray") + "  test: cover both finish paths"]


def test_commit_lines_without_color_are_plain_text() -> None:
    lines = main._commit_lines([{"sha": _SHA_A, "subject": "only"}], color=False)
    assert lines == ["  4091ab6d  only"]


def test_commit_lines_caret_escape_a_subject_carrying_control_bytes() -> None:
    """A commit subject is content-origin text: it must not be able to move the
    cursor, clear the screen or beep."""
    lines = main._commit_lines(
        [{"sha": _SHA_A, "subject": "boom\x1b[2Jwipe\x07"}], color=False)
    assert lines == ["  4091ab6d  boom^[[2Jwipe^G"]
    assert "\x1b" not in lines[0]
    assert "\x07" not in lines[0]


def test_commit_lines_of_nothing_is_nothing() -> None:
    assert main._commit_lines([], color=True) == []


def test_emit_completed_merged_renders_the_full_after_state(monkeypatch, capsys) -> None:
    # AC 1: spine, two commit lines indented exactly two spaces, one trailing blank.
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(
        _quest(), merged_base="master",
        commits=[{"sha": _SHA_A, "subject": "first"},
                 {"sha": _SHA_B, "subject": "second"}])
    assert capsys.readouterr().out == (
        "soro82 · Closing commit list layout · merged into master\n"
        "  4091ab6d  first\n"
        "  832c04d7  second\n"
        "\n")


def test_emit_completed_not_merged_renders_the_same_commit_list(monkeypatch, capsys) -> None:
    # AC 2: the four-segment done tail, same indented list beneath it.
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(
        _quest(), merged_base=None,
        commits=[{"sha": _SHA_A, "subject": "first"},
                 {"sha": _SHA_B, "subject": "second"}])
    assert capsys.readouterr().out == (
        "soro82 · Closing commit list layout · completed · commits on imp/soro82\n"
        "  4091ab6d  first\n"
        "  832c04d7  second\n"
        "\n")


def test_emit_completed_with_no_commits_is_the_bare_spine(monkeypatch, capsys) -> None:
    # AC 3a
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_quest(), merged_base="master")
    assert capsys.readouterr().out == (
        "soro82 · Closing commit list layout · merged into master\n"
        "\n")


def test_emit_completed_untitled_and_intentless_shows_the_unnamed_placeholder(
    monkeypatch, capsys,
) -> None:
    """Neither a title nor an opening intent: the middle segment is the dim
    `(unnamed)` placeholder — never a bare id with no ` · `."""
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_quest(title=""), merged_base="master")
    assert capsys.readouterr().out == "soro82 · (unnamed) · merged into master\n\n"


def test_emit_completed_merged_prints_the_gold_spine_never_green(
    monkeypatch, capsys,
) -> None:
    # AC 4a, 4b, 4c
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    main._emit_completed(_quest(), merged_base="master",
                         commits=[{"sha": _SHA_A, "subject": "first"}])
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert lines[0] == (f'{pal.ansi("soro82", "wordmark")} · Closing commit list layout · '
                        f'{pal.ansi("merged into master", "wordmark")}')
    assert lines[1] == "  " + pal.ansi("4091ab6d", "gray") + "  first"  # subject unstyled
    assert lines[2] == ""
    assert pal.ansi("X", "green").split("X")[0] not in out   # no green-coded text at all
    assert "✓" not in out
    assert not out.startswith("\n")                          # no leading blank
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_emit_completed_names_the_witnessed_base_not_the_quest_attribute(
    monkeypatch, capsys,
) -> None:
    # AC 7
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    quest = _quest()
    assert quest.base_branch == "master"      # the attribute the emitter must NOT read
    main._emit_completed(quest, merged_base="release/9")
    out = capsys.readouterr().out
    assert out.split("\n")[0] == "soro82 · Closing commit list layout · merged into release/9"
    assert "master" not in out


def test_emit_completed_non_merge_tail_names_the_imp_branch_not_the_base(
    monkeypatch, capsys,
) -> None:
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    quest = _quest()
    assert quest.materialized is False                          # the real render seam
    main._emit_completed(quest, merged_base=None)
    out = capsys.readouterr().out
    # The tail names the quest branch imp/<id>, NOT the base branch, even though the
    # client quest object is still materialized=False.
    assert out.split("\n")[0] == (
        f'{pal.ansi("soro82", "wordmark")} · Closing commit list layout · '
        f'{pal.ansi("completed · commits on imp/soro82", "wordmark")}')
    assert "commits on master" not in out
    assert pal.ansi("completed · commits on imp/soro82", "green") not in out
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_emit_completed_emits_no_escape_byte_from_a_commit_subject(
    monkeypatch, capsys,
) -> None:
    # AC 6
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_quest(), merged_base="master",
                         commits=[{"sha": _SHA_A, "subject": "boom\x1b[2Jwipe\x07"}])
    out = capsys.readouterr().out
    assert "  4091ab6d  boom^[[2Jwipe^G\n" in out
    assert "\x1b" not in out
    assert "\x07" not in out


def test_emit_completed_titleless_shows_prompt_fallback(monkeypatch, capsys) -> None:
    # AC 5
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_quest(title=""), merged_base="master",
                         intent="add a dark mode toggle")
    out = capsys.readouterr().out
    assert out.split("\n")[0] == "soro82 · add a dark mode toggle · merged into master"
    assert "(unnamed)" not in out
    assert "\x1b[" not in out


def test_emit_merge_blocked_titleless_shows_prompt_fallback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    quest = _Change([{"type": "user_message",
                      "payload": {"subtype": "intent",
                                  "content": [{"type": "text", "text": "fix the crash"}]}}])
    main._emit_merge_blocked(quest, "worktree_dirty", intent="fix the crash")
    out = capsys.readouterr().out
    assert "merge blocked: uncommitted work in the worktree" in out
    assert "2026-06-12-run-x · fix the crash" in out   # identity line, prompt fallback
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
    main._emit_paused_summary(
        quest, info=main._PauseInfo("interrupt", None), intent="add a dark mode toggle")
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert lines[0] == "interrupt"                                    # gold headline, own line
    assert lines[1] == "2026-06-12-run-x · add a dark mode toggle"  # identity, prompt fallback
    assert lines[2] == "resume: imp resume 2026-06-12-run-x"         # resume line kept
    assert "(unnamed)" not in out


def test_a_non_leave_pause_prints_no_caret(monkeypatch, capsys) -> None:
    # AC 4c: the caret shadow belongs to the keystroke-leave row alone.
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    events = [{"type": "workflow_paused", "payload": {"reason": "interrupt"}}]
    with patch.object(main, "_emit_paused_summary") as ps:
        rc = _closing_block(
            _Change(events), witness=_witness(*events), ending=StreamEnding.CUT,
            coord=_NO_LEAVE, leave_state={})
    assert rc == 130
    ps.assert_called_once()
    assert capsys.readouterr().out == ""


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
        assert _closing_block(
            _Change(events), witness=_witness(*events), ending=StreamEnding.ENDED,
            coord=_NO_LEAVE, leave_state={}) == 0
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
def test_exactly_one_blank_between_narrative_and_the_spine(
    monkeypatch, warnings, tty,
) -> None:
    # AC 8a and 8b, on both a tty and a pipe.
    monkeypatch.setenv("NO_COLOR", "1")
    raw = _end_of_run_output(warnings, tty=tty)
    rows = _screen_rows(raw, tty=tty)
    spine = "2026-06-12-run-x · (unnamed) · merged into master"
    h = next(i for i, r in enumerate(rows) if r.strip() == spine)
    assert rows[h - 1].strip() == ""      # exactly one blank directly above the spine
    assert rows[h - 2].strip() != ""      # …and not two
    last = "• retrying after API 529" if warnings else "merging · complete"
    assert last in rows[h - 2]            # …and it follows the LAST narrative line
    assert raw.endswith("\n\n")           # …and the block ends on exactly one blank
    assert not raw.endswith("\n\n\n")


def test_the_closing_block_prints_the_commits_recorded_on_the_merge(
    monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    events = [
        {"type": "quest_merged",
         "payload": {"base": "master", "commit": "f" * 40,
                     "commits": [{"sha": _SHA_A, "subject": "first"},
                                 {"sha": _SHA_B, "subject": "second"}]}},
    ]
    rc = _closing_block(
        _Change(events), witness=_witness(*events), ending=StreamEnding.ENDED,
        coord=_NO_LEAVE, leave_state={})
    assert rc == 0
    assert capsys.readouterr().out == (
        "2026-06-12-run-x · (unnamed) · merged into master\n"
        "  4091ab6d  first\n"
        "  832c04d7  second\n"
        "\n")


def test_the_closing_block_prints_the_commits_recorded_on_a_plain_done(
    monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    events = [{"type": "workflow_completed",
               "payload": {"commits": [{"sha": _SHA_B, "subject": "only"}]}}]
    rc = _closing_block(
        _Change(events), witness=_witness(*events), ending=StreamEnding.ENDED,
        coord=_NO_LEAVE, leave_state={})
    assert rc == 0
    assert capsys.readouterr().out == (
        "2026-06-12-run-x · (unnamed) · completed · commits on imp/2026-06-12-run-x\n"
        "  832c04d7  only\n"
        "\n")


def test_recorded_commits_is_keyed_on_merged_not_on_whichever_event_came_last() -> None:
    """Both terminal events can sit in one log; `merged` — derived from the
    recorded status — decides which one the body comes from."""
    events = [
        {"type": "workflow_completed",
         "payload": {"commits": [{"sha": _SHA_A, "subject": "completed side"}]}},
        {"type": "quest_merged",
         "payload": {"base": "master", "commit": "f" * 40,
                     "commits": [{"sha": _SHA_B, "subject": "merged side"}]}},
    ]
    assert main._recorded_commits(events, merged=False) == [
        {"sha": _SHA_A, "subject": "completed side"}]
    assert main._recorded_commits(events, merged=True) == [
        {"sha": _SHA_B, "subject": "merged side"}]


def test_a_terminal_event_with_no_commits_key_renders_like_an_empty_list(
    monkeypatch, capsys,
) -> None:
    """An un-reaped quest home can hold a pre-change terminal event. Absent and []
    are one code path: both produce the block imp printed before this change."""
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    absent = [{"type": "workflow_completed", "payload": {}}]
    assert _closing_block(
        _Change(absent), witness=_witness(*absent), ending=StreamEnding.ENDED,
        coord=_NO_LEAVE, leave_state={}) == 0
    no_key = capsys.readouterr().out

    empty = [{"type": "workflow_completed", "payload": {"commits": []}}]
    assert _closing_block(
        _Change(empty), witness=_witness(*empty), ending=StreamEnding.ENDED,
        coord=_NO_LEAVE, leave_state={}) == 0
    assert capsys.readouterr().out == no_key
    assert no_key == (
        "2026-06-12-run-x · (unnamed) · completed · commits on imp/2026-06-12-run-x\n"
        "\n")


_SEED_EMITTERS = [
    pytest.param(lambda q: main._emit_completed(q, merged_base="master"), id="completed"),
    pytest.param(lambda q: main._emit_paused_summary(q, info=None), id="paused"),
    pytest.param(lambda q: main._emit_leave_pause(q, "sigterm"), id="leave-pause"),
    pytest.param(lambda q: main._emit_merge_blocked(q, "worktree_dirty"), id="merge-blocked"),
    pytest.param(lambda q: main._emit_lost_connection(q), id="lost-connection"),
    pytest.param(lambda q: main._emit_daemon_died(q), id="daemon-died"),
    pytest.param(lambda q: main._emit_leave_block(q, main.CONTROL_SAVE, intent=None),
                 id="leave-block"),
    pytest.param(lambda q: main._print_leave_disposition(q, {"send_failed": "1"}),
                 id="left-running"),
]


@pytest.mark.parametrize("emit", _SEED_EMITTERS)
def test_titleless_quest_with_no_intent_is_named_by_its_seed_opening(
    emit, monkeypatch, capsys,
) -> None:
    # An unstarted draft / backlog quest has no intent event; seed_opening names it.
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    q = _Change([])
    q.seed_opening = "summarize\n  foo.py"
    emit(q)
    out = capsys.readouterr().out
    assert "2026-06-12-run-x · summarize foo.py" in out
    assert "(unnamed)" not in out


def test_a_handed_intent_wins_over_seed_opening(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    q = _quest(title="")
    q.seed_opening = "the seeded brief"
    main._emit_completed(q, merged_base="master", intent="what the client witnessed")
    out = capsys.readouterr().out
    assert out.split("\n")[0] == "soro82 · what the client witnessed · merged into master"
    assert "the seeded brief" not in out
