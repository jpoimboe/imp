"""Tests for imp log --follow: CLI surface and routing (src/imp/cli/main.py)."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.engine.quest import LAYOUT_VERSION
from imp.paths import project_state_dir
from tests._faultlog import diag, fault_lines
from tests.conftest import FIXTURES


def _log_ns(**over: object) -> argparse.Namespace:
    base = dict(token=None, follow=False, summary=False, full=False,
                html=None, md=None, no_pager=False, show=None, hide=None)
    base.update(over)
    return argparse.Namespace(**base)


def _fake_tty(monkeypatch: pytest.MonkeyPatch, *, isatty: bool) -> None:
    import sys
    monkeypatch.setattr(sys.stdout, "isatty", lambda: isatty)


def _make_live_home(control: Path, slug: str) -> Path:
    """Minimal live Quest home with a 2-event narrative so _cmd_log resolves
    the id to a live worktree (materialized default → not a backlog card)."""
    d = project_state_dir(control) / "quests" / slug
    d.mkdir(parents=True)
    (d / "quest.json").write_text(json.dumps({
        "id": slug, "title": "follow", "base_branch": "main",
        "layout_version": LAYOUT_VERSION,
        "created_at": "2026-05-27T10:00:00+00:00"}))
    (d / "state").mkdir()
    (d / "state" / "state.json").write_text(
        json.dumps({"step": "-", "status": "paused"}))
    (d / "events.jsonl").write_text(
        json.dumps({"timestamp": "2026-05-27T10:00:00+00:00",
                    "type": "quest_created", "payload": {"title": "follow"}}) + "\n"
        + json.dumps({"id": "e2", "agent_id": None,
                      "timestamp": "2026-05-27T10:00:01+00:00",
                      "type": "step_started",
                      "payload": {"step": "s", "kind": "agent", "phase": "spec",
                                  "model": "m", "provider": "anthropic"}}) + "\n")
    return d


def _write_events(home: Path, events: list[dict[str, object]]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))


def _write_state(home: Path, *, status: str, slug: str = "s") -> None:
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "state.json").write_text(json.dumps(
        {"id": slug, "step": "-", "step_attempt": 0, "status": status}))


def _write_live_lock(home: Path) -> None:
    """A lock naming this live process → lock_is_live reads True."""
    (home / "lock").write_text(str(os.getpid()))


def test_follow_step_feeds_only_new_events(tmp_path: Path) -> None:
    """First step replays existing events; a later step feeds only the appended
    one; a non-terminal status returns None and never finishes."""
    from imp.cli.main import _EventTail, _follow_step
    slug = "s1"
    home = project_state_dir(tmp_path) / "quests" / slug
    _write_events(home, [{"type": "a"}])
    _write_state(home, status="paused", slug=slug)
    driver, status = MagicMock(), MagicMock()
    tail = _EventTail(home / "events.jsonl")
    assert _follow_step(driver, tail, status, tmp_path, slug, home) is None
    assert [c.args[0] for c in driver.on_event.call_args_list] == [{"type": "a"}]
    driver.finish.assert_not_called()
    with (home / "events.jsonl").open("a") as f:
        f.write(json.dumps({"type": "b"}) + "\n")
    assert _follow_step(driver, tail, status, tmp_path, slug, home) is None
    assert driver.on_event.call_args_list[-1].args[0] == {"type": "b"}


def test_follow_step_paused_footer(tmp_path: Path) -> None:
    """No live lock → the footer composer renders the static paused line and no
    finish() is called (pause is not terminal)."""
    from imp.cli.main import _EventTail, _follow_step
    slug = "follow-me"
    home = project_state_dir(tmp_path) / "quests" / slug
    _write_events(home, [{"type": "a"}])
    _write_state(home, status="paused", slug=slug)  # no lock file → not live
    driver, status = MagicMock(), MagicMock()
    tail = _EventTail(home / "events.jsonl")
    assert _follow_step(driver, tail, status, tmp_path, slug, home) is None
    compose = status.update.call_args.args[0]
    assert compose(0) == ["paused — resume with imp resume follow-me"]
    driver.finish.assert_not_called()


def test_follow_step_merge_blocked_is_non_terminal(tmp_path: Path) -> None:
    """merge_blocked is not terminal: returns None and finish() is not called."""
    from imp.cli.main import _EventTail, _follow_step
    slug = "blocked-me"
    home = project_state_dir(tmp_path) / "quests" / slug
    _write_events(home, [{"type": "a"}])
    _write_state(home, status="merge_blocked", slug=slug)
    driver, status = MagicMock(), MagicMock()
    tail = _EventTail(home / "events.jsonl")
    assert _follow_step(driver, tail, status, tmp_path, slug, home) is None
    driver.finish.assert_not_called()


def test_follow_step_live_footer_uses_compose_status(tmp_path: Path) -> None:
    """A live lock → the footer is driven by driver.compose_status."""
    from imp.cli.main import _EventTail, _follow_step
    slug = "s2"
    home = project_state_dir(tmp_path) / "quests" / slug
    _write_events(home, [{"type": "a"}])
    _write_state(home, status="new", slug=slug)
    _write_live_lock(home)
    driver, status = MagicMock(), MagicMock()
    tail = _EventTail(home / "events.jsonl")
    assert _follow_step(driver, tail, status, tmp_path, slug, home) is None
    status.update.assert_called_with(driver.compose_status)


@pytest.mark.parametrize("terminal", ["merged", "done", "abandoned", "shelved"])
def test_follow_step_terminal_drains_and_finishes(
    tmp_path: Path, terminal: str,
) -> None:
    """Each terminal status feeds remaining events, calls finish() once, and
    returns exit code 0."""
    from imp.cli.main import _EventTail, _follow_step
    slug = "s3"
    home = project_state_dir(tmp_path) / "quests" / slug
    _write_events(home, [{"type": "a"}, {"type": "b"}])
    _write_state(home, status=terminal, slug=slug)
    driver, status = MagicMock(), MagicMock()
    tail = _EventTail(home / "events.jsonl")
    rc = _follow_step(driver, tail, status, tmp_path, slug, home)
    assert rc == 0
    assert driver.on_event.call_count == 2
    driver.finish.assert_called_once()


def test_follow_step_home_vanished_drains_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the home dir is gone, drain from the archive only the events beyond
    the already-emitted count, then finish() and exit 0."""
    from imp.cli.main import _EventTail, _follow_step
    slug = "gone"
    home = project_state_dir(tmp_path) / "quests" / slug  # never created → not exists
    tail = _EventTail(home / "events.jsonl")
    tail.emitted = 1  # pretend one event was already shown before the vanish
    archived = [{"type": "a"}, {"type": "b"}, {"type": "c"}]
    raw = "".join(json.dumps(e) + "\n" for e in archived).encode()
    monkeypatch.setattr("imp.engine.archive.read.read_events", lambda c, i: raw)
    driver, status = MagicMock(), MagicMock()
    rc = _follow_step(driver, tail, status, tmp_path, slug, home)
    assert rc == 0
    assert [c.args[0] for c in driver.on_event.call_args_list] == \
        [{"type": "b"}, {"type": "c"}]
    driver.finish.assert_called_once()


def test_follow_step_state_vanished_mid_poll_drains_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """home.exists() passes but state.json is gone (teardown race) → archive drain.

    tail.read_new() runs first (events.jsonl exists) and feeds the live event;
    then State.load raises FileNotFoundError; the drain feeds only the archived
    events beyond the count already emitted by tail.read_new().
    """
    from imp.cli.main import _EventTail, _follow_step
    slug = "race"
    home = project_state_dir(tmp_path) / "quests" / slug
    # home dir + events.jsonl exist; state/state.json does NOT (race condition)
    _write_events(home, [{"type": "live"}])
    # Archive holds the complete sequence; drain skips what tail already emitted
    archived = [{"type": "live"}, {"type": "arch1"}, {"type": "arch2"}]
    raw = "".join(json.dumps(e) + "\n" for e in archived).encode()
    monkeypatch.setattr("imp.engine.archive.read.read_events", lambda c, i: raw)
    driver, status = MagicMock(), MagicMock()
    tail = _EventTail(home / "events.jsonl")
    rc = _follow_step(driver, tail, status, tmp_path, slug, home)
    assert rc == 0
    # tail.read_new() fed "live" (emitted→1); drain fed archived[1:] = arch1, arch2
    assert [c.args[0] for c in driver.on_event.call_args_list] == \
        [{"type": "live"}, {"type": "arch1"}, {"type": "arch2"}]
    driver.finish.assert_called_once()


def test_follow_step_unknown_after_vanish_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home gone AND not in the archive → finish() and exit 0, nothing fed."""
    from imp.cli.main import _EventTail, _follow_step
    slug = "poof"
    home = project_state_dir(tmp_path) / "quests" / slug  # not exists
    tail = _EventTail(home / "events.jsonl")
    monkeypatch.setattr("imp.engine.archive.read.read_events", lambda c, i: None)
    driver, status = MagicMock(), MagicMock()
    rc = _follow_step(driver, tail, status, tmp_path, slug, home)
    assert rc == 0
    driver.on_event.assert_not_called()
    driver.finish.assert_called_once()


def test_follow_conflict_flags_summary() -> None:
    from imp.cli.main import _follow_conflict
    assert _follow_conflict(_log_ns(follow=True, summary=True)) == "--summary"


def test_follow_conflict_allows_the_content_flags() -> None:
    from imp.cli.main import _follow_conflict
    assert _follow_conflict(_log_ns(follow=True, full=True)) is None
    assert _follow_conflict(_log_ns(follow=True, show=["thinking"])) is None
    assert _follow_conflict(_log_ns(follow=True, hide=["tiers"])) is None


def test_follow_conflict_flags_html() -> None:
    from imp.cli.main import _follow_conflict
    assert _follow_conflict(_log_ns(follow=True, html="-")) == "--html"


def test_follow_conflict_flags_md() -> None:
    from imp.cli.main import _follow_conflict
    assert _follow_conflict(_log_ns(follow=True, md="out.md")) == "--md"


def test_follow_conflict_none_when_alone() -> None:
    from imp.cli.main import _follow_conflict
    assert _follow_conflict(_log_ns(follow=True)) is None


def test_main_log_follow_rejects_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`imp log -f --summary` is rejected after parsing with an argparse-style
    error and exit 2 (the flag parses, then the exclusion fires)."""
    import imp.cli.main as m
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = m.main(["log", "-f", "--summary"])
    assert rc == 2
    assert "imp: -f/--follow not allowed with --summary" in capsys.readouterr().err


def test_main_log_follow_implies_no_pager_on_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`imp log -f <id>` never spawns a pager even on a TTY — --follow forces
    no_pager and the follow path returns before any paging."""
    import imp.cli.main as m
    slug = "follow-me"
    _make_live_home(tmp_path, slug)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("PAGER", raising=False)
    _fake_tty(monkeypatch, isatty=True)
    monkeypatch.setattr(m, "_follow_live",
                        lambda control, cid, home, *, cats: 0)
    with patch("imp.cli.main.subprocess.Popen") as popen:
        rc = m.main(["log", "-f", slug])
    assert rc == 0
    assert popen.call_count == 0


def test_follow_log_replays_all_events_then_finishes() -> None:
    """_follow_log feeds every event to LiveDriver.on_event in order, then
    calls finish() exactly once, and returns 0."""
    import imp.cli.main as m
    events = [
        {"type": "quest_created", "payload": {"title": "t"}},
        {"type": "step_started", "payload": {"step": "s"}},
    ]
    driver = MagicMock()
    with patch("imp.cli.live_driver.LiveDriver", return_value=driver), \
         patch("imp.cli.live_status.LiveStatus"):
        rc = m._follow_log(events, cats=frozenset())
    assert rc == 0
    assert [c.args[0] for c in driver.on_event.call_args_list] == events
    driver.finish.assert_called_once()


def test_event_tail_incremental(tmp_path: Path) -> None:
    """read_new() returns only lines appended since the previous call, and
    `emitted` counts every event handed out."""
    from imp.cli.main import _EventTail
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps({"type": "a"}) + "\n")
    tail = _EventTail(p)
    assert tail.read_new() == [{"type": "a"}]
    assert tail.read_new() == []
    with p.open("a") as f:
        f.write(json.dumps({"type": "b"}) + "\n")
    assert tail.read_new() == [{"type": "b"}]
    assert tail.emitted == 2


def test_event_tail_partial_line_held(tmp_path: Path) -> None:
    """A final line with no trailing newline is held back until it is
    newline-terminated."""
    from imp.cli.main import _EventTail
    p = tmp_path / "events.jsonl"
    p.write_bytes(json.dumps({"type": "a"}).encode() + b"\n"
                  + json.dumps({"type": "b"}).encode())  # no trailing "\n"
    tail = _EventTail(p)
    assert tail.read_new() == [{"type": "a"}]
    with p.open("a") as f:
        f.write("\n")
    assert tail.read_new() == [{"type": "b"}]


def test_event_tail_bad_line_halts_then_recovers(tmp_path: Path) -> None:
    """A complete but unparseable line halts consumption at its offset without
    raising; once the bytes at that offset read as valid JSON (transient fault
    cleared) the tail resumes from there."""
    from imp.cli.main import _EventTail
    p = tmp_path / "events.jsonl"
    good = json.dumps({"type": "a"}).encode() + b"\n"
    p.write_bytes(good + b"{bad json}\n" + json.dumps({"type": "c"}).encode() + b"\n")
    tail = _EventTail(p)
    assert tail.read_new() == [{"type": "a"}]   # stops at the bad line, no crash
    assert tail.read_new() == []                # still blocked at the same offset
    p.write_bytes(good + json.dumps({"type": "b"}).encode() + b"\n"
                  + json.dumps({"type": "c"}).encode() + b"\n")
    assert tail.read_new() == [{"type": "b"}, {"type": "c"}]


_BAD_LINE = b"{bad json}"


def _events_with_bad_line(p: Path) -> bytes:
    """Write an events.jsonl whose second line is unparseable; return the
    leading good line so a later write can heal the file at the same offset."""
    good_first = json.dumps({"type": "a"}).encode() + b"\n"
    p.write_bytes(good_first + _BAD_LINE + b"\n"
                  + json.dumps({"type": "c"}).encode() + b"\n")
    return good_first


def test_event_tail_fault_records_once_and_drops_the_page_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll that first meets an unparseable line records one unrecovered
    line and forces the next read to come from disk."""
    from imp.cli.main import _EventTail
    p = tmp_path / "events.jsonl"
    _events_with_bad_line(p)
    dropped: list[Path] = []
    monkeypatch.setattr("imp.engine.pagecache.drop_page_cache", dropped.append)
    tail = _EventTail(p)
    assert tail.read_new() == [{"type": "a"}]
    assert dropped == [p]
    lines = fault_lines()
    assert len(lines) == 1
    line = lines[0]
    assert line["site"] == "event_tail"
    assert line["path"] == str(p)
    assert line["recovered"] is False
    assert line["attempt"] is None
    assert line["good_len"] is None
    assert line["head_drop"] is None
    assert line["bad_len"] == len(_BAD_LINE)
    assert line["bad_sha1"] == hashlib.sha1(_BAD_LINE).hexdigest()
    assert (diag() / line["bytes"]).read_bytes() == _BAD_LINE


def test_event_tail_stuck_offset_records_nothing_further(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tail stuck at the same offset must not spam the diagnostics log."""
    from imp.cli.main import _EventTail
    p = tmp_path / "events.jsonl"
    _events_with_bad_line(p)
    dropped: list[Path] = []
    monkeypatch.setattr("imp.engine.pagecache.drop_page_cache", dropped.append)
    tail = _EventTail(p)
    assert tail.read_new() == [{"type": "a"}]
    for _ in range(10):
        assert tail.read_new() == []
    assert len(fault_lines()) == 1
    assert dropped == [p]


def test_event_tail_recovery_records_the_poll_total_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the stuck offset finally parses, a second line records the
    recovery with every poll that saw the offset counted, and the tail resumes
    with no event lost or duplicated."""
    from imp.cli.main import _EventTail
    p = tmp_path / "events.jsonl"
    good_first = _events_with_bad_line(p)
    monkeypatch.setattr("imp.engine.pagecache.drop_page_cache", lambda path: None)
    tail = _EventTail(p)
    assert tail.read_new() == [{"type": "a"}]        # poll 1: the fault
    for _ in range(10):                              # polls 2-11: still stuck
        assert tail.read_new() == []
    healed = json.dumps({"type": "b"}).encode()
    p.write_bytes(good_first + healed + b"\n"
                  + json.dumps({"type": "c"}).encode() + b"\n")
    assert tail.read_new() == [{"type": "b"}, {"type": "c"}]   # poll 12
    assert tail.read_new() == []
    assert tail.emitted == 3                         # a, b, c -- exactly once each
    lines = fault_lines()
    assert len(lines) == 2
    rec = lines[1]
    assert rec["site"] == "event_tail"
    assert rec["recovered"] is True
    assert rec["attempt"] == 12                      # 11 failing polls + the recovering one
    assert rec["bad_len"] == len(_BAD_LINE)
    assert rec["bad_sha1"] == hashlib.sha1(_BAD_LINE).hexdigest()
    assert rec["good_len"] == len(healed)
    assert rec["good_sha1"] == hashlib.sha1(healed).hexdigest()
    assert rec["head_drop"] is False
    assert (diag() / rec["bytes"]).read_bytes() == _BAD_LINE


def test_event_tail_missing_file_then_created(tmp_path: Path) -> None:
    """A missing file yields [] without raising; once it appears, read_new()
    reads it from the start."""
    from imp.cli.main import _EventTail
    p = tmp_path / "events.jsonl"
    tail = _EventTail(p)
    assert tail.read_new() == []          # file absent → [], no crash
    p.write_text(json.dumps({"type": "a"}) + "\n")
    assert tail.read_new() == [{"type": "a"}]
    assert tail.emitted == 1


def test_follow_step_second_drain_catches_late_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event appended in the window between the first read and the terminal
    status check is fed by the second drain, not the first."""
    from imp.cli.main import _EventTail, _follow_step
    slug = "late"
    home = project_state_dir(tmp_path) / "quests" / slug
    _write_events(home, [{"type": "a"}])
    driver, status = MagicMock(), MagicMock()
    tail = _EventTail(home / "events.jsonl")

    class _TerminalState:
        status = "merged"

    def _load_and_append(state_dir: Path) -> _TerminalState:
        with (home / "events.jsonl").open("a") as f:
            f.write(json.dumps({"type": "b"}) + "\n")
        return _TerminalState()

    monkeypatch.setattr("imp.engine.state.State.load", _load_and_append)
    rc = _follow_step(driver, tail, status, tmp_path, slug, home)
    assert rc == 0
    assert [c.args[0] for c in driver.on_event.call_args_list] == \
        [{"type": "a"}, {"type": "b"}]
    driver.finish.assert_called_once()


def test_cmd_log_follow_routes_live_home_to_follow_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`imp log -f <id>` on a present home routes to _follow_live with the
    change home path."""
    import imp.cli.main as m
    slug = "live-one"
    _make_live_home(tmp_path, slug)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    _fake_tty(monkeypatch, isatty=False)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        m, "_follow_live",
        lambda control, cid, home, *, cats: (seen.update(cid=cid, home=home), 7)[1])
    rc = m.main(["log", "-f", slug])
    assert rc == 7
    assert seen["cid"] == slug
    assert seen["home"] == project_state_dir(tmp_path) / "quests" / slug


def test_cmd_log_follow_routes_archived_only_to_follow_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`imp log -f <id>` for an id with no live home (archived-only) keeps the
    Part 1 replay-and-exit path _follow_log."""
    import imp.cli.main as m
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_resolve_id", lambda control, tok: "arch")
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, i: None)
    monkeypatch.setattr(
        m, "_load_events_for_id",
        lambda c, i: [{"type": "quest_created", "payload": {}}])
    monkeypatch.setattr(m, "_quest_home", lambda c, i: tmp_path / "absent")
    monkeypatch.setattr(m, "_follow_log", lambda events, *, cats, project: 9)
    rc = m.main(["log", "-f", "arch"])
    assert rc == 9


def test_cmd_log_follow_archived_only_threads_cats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archived-only route hands _follow_log the resolved category set, so
    `imp log -f --full` on a quest with no live home is a full-view replay."""
    import imp.cli.main as m
    from imp.cli.render_text import LOG_CATEGORIES
    seen: dict[str, object] = {}
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_resolve_id", lambda control, tok: "arch")
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, i: None)
    monkeypatch.setattr(
        m, "_load_events_for_id",
        lambda c, i: [{"type": "quest_created", "payload": {}}])
    monkeypatch.setattr(m, "_quest_home", lambda c, i: tmp_path / "absent")
    monkeypatch.setattr(
        m, "_follow_log",
        lambda events, *, cats, project: (seen.update(cats=cats), 9)[1])
    rc = m.main(["log", "-f", "--full", "--hide", "tiers", "arch"])
    assert rc == 9
    assert seen["cats"] == frozenset(LOG_CATEGORIES) - {"tiers"}


def test_follow_live_ctrl_c_clears_and_returns_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C during the poll loop clears the footer (status.reset) and exits
    130, without calling finish()."""
    import imp.cli.main as m
    status = MagicMock()
    driver = MagicMock()
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda stream: status)
    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", lambda **kw: driver)
    monkeypatch.setattr("anyio.run", MagicMock(side_effect=KeyboardInterrupt))
    rc = m._follow_live(tmp_path, "id", tmp_path / "home", cats=frozenset())
    assert rc == 130
    status.reset.assert_called_once()
    driver.finish.assert_not_called()


class _Pipe(io.StringIO):
    """A non-TTY stream: LiveStatus self-disables on it, so only the driver's
    permanent narrative lines are written."""

    def isatty(self) -> bool:
        return False


def _full_run_events() -> list[dict[str, object]]:
    """The complete journaled run behind the live-view golden snapshots."""
    gz = FIXTURES / "cc_open_by_asking_full.jsonl.gz"
    with gzip.open(gz, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _live_capture(events: list[dict[str, object]]) -> str:
    """The live-session capture: feed every event straight through a LiveDriver
    (as a headless session does), then finish() to flush the final block."""
    out = _Pipe()
    driver = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        driver.on_event(e)
    driver.finish()
    return out.getvalue()


def _live_narrative_no_finish(events: list[dict[str, object]]) -> str:
    """The permanent narrative a LiveDriver emits mid-stream (no finish()) — the
    exact bytes a non-terminal follow poll should have produced."""
    out = _Pipe()
    driver = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        driver.on_event(e)
    return out.getvalue()


def test_follow_matches_live_session_capture(tmp_path: Path) -> None:
    """Following a run to completion — replaying the narrative already present,
    then tailing the rest across polls until a terminal status — produces
    byte-for-byte the same output as feeding every event straight through
    LiveDriver. This is the spec's golden comparison against a live capture."""
    from imp.cli.main import _EventTail, _follow_step
    events = _full_run_events()
    capture = _live_capture(events)

    slug = "goldenrun"
    home = project_state_dir(tmp_path) / "quests" / slug
    split = 500
    _write_events(home, events[:split])           # narrative present at start
    _write_state(home, status="new", slug=slug)   # not terminal yet

    out = _Pipe()
    status = LiveStatus(out)
    driver = LiveDriver(stream=out, color=False, width=80, status=status)
    tail = _EventTail(home / "events.jsonl")

    # Poll 1: replay everything present; still running → keep polling.
    assert _follow_step(driver, tail, status, tmp_path, slug, home) is None

    # The run appends the remaining events and reaches a terminal status.
    with (home / "events.jsonl").open("a") as f:
        for e in events[split:]:
            f.write(json.dumps(e) + "\n")
    _write_state(home, status="merged", slug=slug)

    # Poll 2: drain the tail, finish(), exit 0 — output equals the live capture,
    # and finish() is independently pinned (byte-identical with/without it here).
    with patch.object(driver, "finish", wraps=driver.finish) as finish_spy:
        assert _follow_step(driver, tail, status, tmp_path, slug, home) == 0
    assert out.getvalue() == capture
    finish_spy.assert_called_once()


def test_follow_non_tty_footer_stays_inert(tmp_path: Path) -> None:
    """Off a TTY, LiveStatus is inert even on the LIVE footer branch (lock live →
    driver.compose_status): a non-terminal poll writes only the plain
    append-only narrative and no footer bytes, so a piped `imp log -f` degrades
    to the driver-only stream."""
    from imp.cli.main import _EventTail, _follow_step
    events = _full_run_events()[:120]
    slug = "piped"
    home = project_state_dir(tmp_path) / "quests" / slug
    _write_events(home, events)
    _write_state(home, status="new", slug=slug)
    _write_live_lock(home)                         # lock live → live-spinner branch

    out = _Pipe()                                  # isatty() → False
    status = LiveStatus(out)
    driver = LiveDriver(stream=out, color=False, width=80, status=status)
    tail = _EventTail(home / "events.jsonl")

    assert _follow_step(driver, tail, status, tmp_path, slug, home) is None
    body = out.getvalue()
    # Equal to the driver-only narrative → the footer contributed nothing.
    assert body == _live_narrative_no_finish(events)
    assert "\x1b" not in body                      # no cursor-hide / erase ANSI


def test_cmd_log_follow_long_form_starts_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `--follow` long form starts a follow on a live home exactly as `-f`
    does — both spellings route to _follow_live with the change home path."""
    import imp.cli.main as m
    slug = "long-form"
    _make_live_home(tmp_path, slug)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    _fake_tty(monkeypatch, isatty=False)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        m, "_follow_live",
        lambda control, cid, home, *, cats: (seen.update(cid=cid, home=home), 5)[1])
    rc = m.main(["log", "--follow", slug])
    assert rc == 5
    assert seen["cid"] == slug
    assert seen["home"] == project_state_dir(tmp_path) / "quests" / slug


def test_cmd_log_follow_backlog_id_prints_card_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A backlog/unstarted id under `-f` prints the backlog card and exits 0 —
    nothing will ever stream, so the backlog guard fires before the follow route
    and neither follow entrypoint is invoked."""
    import imp.cli.main as m

    def _boom(*a: object, **k: object) -> int:
        raise AssertionError("follow must not run for a backlog id")

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_resolve_id", lambda control, tok: "back")
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, i: {"stub": True})
    monkeypatch.setattr(m, "_backlog_card", lambda meta, control, now, color: ["BACKLOG CARD"])
    monkeypatch.setattr(m, "_follow_live", _boom)
    monkeypatch.setattr(m, "_follow_log", _boom)
    rc = m.main(["log", "-f", "back"])
    assert rc == 0
    assert "BACKLOG CARD" in capsys.readouterr().out


def test_cmd_log_follow_in_sandbox_degrades_to_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """In-sandbox (IMP_SESSION_SOCKET set), `imp log -f <id>` on a LIVE home never
    tails the masked events.jsonl: it prints a note to stderr and routes the
    store-served events through the one-shot _follow_log, exiting cleanly."""
    import types

    import imp.cli.main as m
    from imp.cli.store import HomeDigest
    slug = "sandboxed"
    _make_live_home(tmp_path, slug)  # live home exists → would normally _follow_live
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("IMP_SESSION_SOCKET", "/tmp/imp-1000/deadbeef.sock")
    snapshot = [{"type": "quest_created", "payload": {"title": "follow"}}]
    # IMP_SESSION_SOCKET set → the real make_store picks ProxyStore; stub it so the
    # events come from the store without a live daemon socket.
    monkeypatch.setattr(
        "imp.cli.store.make_store",
        lambda control: types.SimpleNamespace(
            get_events=lambda qid: snapshot,
            scan_homes=lambda: [HomeDigest(slug, b"{}", None, {}, "", False, "", None, False)],
            completed_rows=lambda: []))

    def _boom(*a: object, **k: object) -> int:
        raise AssertionError("live follow must not run inside the sandbox")

    monkeypatch.setattr(m, "_follow_live", _boom)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        m, "_follow_log",
        lambda events, *, cats, project: (
            seen.update(events=events, project=project), 0)[1])
    rc = m.main(["log", "-f", slug])
    assert rc == 0
    assert seen["events"] == snapshot  # the store snapshot went to the one-shot path
    assert seen["project"] == tmp_path.name
    err = capsys.readouterr().err
    assert "imp: live follow is unavailable inside the sandbox" in err
    assert "imp log:" not in err


def test_follow_log_threads_cats_into_the_driver() -> None:
    """The one-shot replay path hands its resolved category set to LiveDriver."""
    import imp.cli.main as m
    seen: dict[str, object] = {}

    def _fake_driver(**kwargs: object) -> MagicMock:
        seen.update(kwargs)
        return MagicMock()

    with (
        patch("imp.cli.live_driver.LiveDriver", _fake_driver),
        patch("imp.cli.live_status.LiveStatus"),
    ):
        rc = m._follow_log([], cats=frozenset({"thinking", "tools"}))
    assert rc == 0
    assert seen["cats"] == frozenset({"thinking", "tools"})


def test_follow_live_threads_cats_into_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tailing path hands its resolved category set to LiveDriver."""
    import imp.cli.main as m
    seen: dict[str, object] = {}

    def _fake_driver(**kwargs: object) -> MagicMock:
        seen.update(kwargs)
        return MagicMock()

    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", _fake_driver)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", MagicMock())
    monkeypatch.setattr("anyio.run", lambda *a, **kw: 0)
    rc = m._follow_live(tmp_path, "id", tmp_path / "home",
                        cats=frozenset({"prompts"}))
    assert rc == 0
    assert seen["cats"] == frozenset({"prompts"})


def test_follow_log_threads_the_project_into_the_driver() -> None:
    """The one-shot replay path hands its caller-supplied project to LiveDriver."""
    import imp.cli.main as m
    seen: dict[str, object] = {}

    def _fake_driver(**kwargs: object) -> MagicMock:
        seen.update(kwargs)
        return MagicMock()

    with (
        patch("imp.cli.live_driver.LiveDriver", _fake_driver),
        patch("imp.cli.live_status.LiveStatus"),
    ):
        rc = m._follow_log([], cats=frozenset(), project="imp")
    assert rc == 0
    assert seen["project"] == "imp"


def test_follow_live_threads_the_control_root_name_into_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tailing path names the project from the control root it was handed."""
    import imp.cli.main as m
    seen: dict[str, object] = {}

    def _fake_driver(**kwargs: object) -> MagicMock:
        seen.update(kwargs)
        return MagicMock()

    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", _fake_driver)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", MagicMock())
    monkeypatch.setattr("anyio.run", lambda *a, **kw: 0)
    rc = m._follow_live(tmp_path, "id", tmp_path / "home", cats=frozenset())
    assert rc == 0
    assert seen["project"] == tmp_path.name


def test_cmd_log_follow_archived_only_fills_the_project_from_the_control_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_follow_log takes no control root, so its caller fills project= for it."""
    import imp.cli.main as m
    seen: dict[str, object] = {}
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_resolve_id", lambda control, tok: "arch")
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, i: None)
    monkeypatch.setattr(
        m, "_load_events_for_id",
        lambda c, i: [{"type": "quest_created", "payload": {}}])
    monkeypatch.setattr(m, "_quest_home", lambda c, i: tmp_path / "absent")
    monkeypatch.setattr(
        m, "_follow_log",
        lambda events, *, cats, project: (seen.update(project=project), 9)[1])
    rc = m.main(["log", "-f", "arch"])
    assert rc == 9
    assert seen["project"] == tmp_path.name
