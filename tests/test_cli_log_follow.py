"""Tests for imp log --follow: CLI surface and routing (src/imp/cli/main.py)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from imp.engine.change import LAYOUT_VERSION


def _log_ns(**over: object) -> argparse.Namespace:
    base = dict(token=None, follow=False, summary=False, full=False,
                html=None, md=None, no_pager=False)
    base.update(over)
    return argparse.Namespace(**base)


def _fake_tty(monkeypatch: pytest.MonkeyPatch, *, isatty: bool) -> None:
    import sys
    monkeypatch.setattr(sys.stdout, "isatty", lambda: isatty)


def _make_live_home(control: Path, slug: str) -> Path:
    """Minimal live Change home with a 2-event narrative so _cmd_log resolves
    the id to a live worktree (materialized default → not a backlog card)."""
    d = control / ".imp" / "changes" / slug
    d.mkdir(parents=True)
    (d / "change.json").write_text(json.dumps({
        "id": slug, "title": "follow", "base_branch": "main",
        "layout_version": LAYOUT_VERSION,
        "created_at": "2026-05-27T10:00:00+00:00"}))
    (d / "state").mkdir()
    (d / "state" / "state.json").write_text(
        json.dumps({"step": "-", "status": "paused"}))
    (d / "events.jsonl").write_text(
        json.dumps({"timestamp": "2026-05-27T10:00:00+00:00",
                    "type": "change_created", "payload": {"title": "follow"}}) + "\n"
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
    home = tmp_path / ".imp" / "changes" / slug
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
    home = tmp_path / ".imp" / "changes" / slug
    _write_events(home, [{"type": "a"}])
    _write_state(home, status="paused", slug=slug)  # no lock file → not live
    driver, status = MagicMock(), MagicMock()
    tail = _EventTail(home / "events.jsonl")
    assert _follow_step(driver, tail, status, tmp_path, slug, home) is None
    compose = status.update.call_args.args[0]
    assert compose(0, 0.0) == ["paused — resume with imp resume follow-me"]
    driver.finish.assert_not_called()


def test_follow_step_live_footer_uses_compose_status(tmp_path: Path) -> None:
    """A live lock → the footer is driven by driver.compose_status."""
    from imp.cli.main import _EventTail, _follow_step
    slug = "s2"
    home = tmp_path / ".imp" / "changes" / slug
    _write_events(home, [{"type": "a"}])
    _write_state(home, status="new", slug=slug)
    _write_live_lock(home)
    driver, status = MagicMock(), MagicMock()
    tail = _EventTail(home / "events.jsonl")
    assert _follow_step(driver, tail, status, tmp_path, slug, home) is None
    status.update.assert_called_with(driver.compose_status)


@pytest.mark.parametrize("terminal", ["completed", "abandoned", "saved"])
def test_follow_step_terminal_drains_and_finishes(
    tmp_path: Path, terminal: str,
) -> None:
    """Each terminal status feeds remaining events, calls finish() once, and
    returns exit code 0."""
    from imp.cli.main import _EventTail, _follow_step
    slug = "s3"
    home = tmp_path / ".imp" / "changes" / slug
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
    import imp.cli.main as m
    from imp.cli.main import _EventTail, _follow_step
    slug = "gone"
    home = tmp_path / ".imp" / "changes" / slug  # never created → not exists
    tail = _EventTail(home / "events.jsonl")
    tail.emitted = 1  # pretend one event was already shown before the vanish
    archived = [{"type": "a"}, {"type": "b"}, {"type": "c"}]
    raw = "".join(json.dumps(e) + "\n" for e in archived).encode()
    monkeypatch.setattr(m.archive_read, "read_events", lambda c, i: raw)
    driver, status = MagicMock(), MagicMock()
    rc = _follow_step(driver, tail, status, tmp_path, slug, home)
    assert rc == 0
    assert [c.args[0] for c in driver.on_event.call_args_list] == \
        [{"type": "b"}, {"type": "c"}]
    driver.finish.assert_called_once()


def test_follow_step_unknown_after_vanish_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home gone AND not in the archive → finish() and exit 0, nothing fed."""
    import imp.cli.main as m
    from imp.cli.main import _EventTail, _follow_step
    slug = "poof"
    home = tmp_path / ".imp" / "changes" / slug  # not exists
    tail = _EventTail(home / "events.jsonl")
    monkeypatch.setattr(m.archive_read, "read_events", lambda c, i: None)
    driver, status = MagicMock(), MagicMock()
    rc = _follow_step(driver, tail, status, tmp_path, slug, home)
    assert rc == 0
    driver.on_event.assert_not_called()
    driver.finish.assert_called_once()


def test_follow_conflict_flags_summary() -> None:
    from imp.cli.main import _follow_conflict
    assert _follow_conflict(_log_ns(follow=True, summary=True)) == "--summary"


def test_follow_conflict_flags_full() -> None:
    from imp.cli.main import _follow_conflict
    assert _follow_conflict(_log_ns(follow=True, full=True)) == "--full"


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
    assert "not allowed with argument --summary" in capsys.readouterr().err


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
    monkeypatch.setattr(m, "_follow_live", lambda control, cid, home: 0)
    with patch("imp.cli.main.subprocess.Popen") as popen:
        rc = m.main(["log", "-f", slug])
    assert rc == 0
    assert popen.call_count == 0


def test_follow_log_replays_all_events_then_finishes() -> None:
    """_follow_log feeds every event to LiveDriver.on_event in order, then
    calls finish() exactly once, and returns 0."""
    import imp.cli.main as m
    events = [
        {"type": "change_created", "payload": {"title": "t"}},
        {"type": "step_started", "payload": {"step": "s"}},
    ]
    driver = MagicMock()
    with patch.object(m, "LiveDriver", return_value=driver), \
         patch.object(m, "LiveStatus"):
        rc = m._follow_log(events)
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
    import imp.cli.main as m
    from imp.cli.main import _EventTail, _follow_step
    slug = "late"
    home = tmp_path / ".imp" / "changes" / slug
    _write_events(home, [{"type": "a"}])
    driver, status = MagicMock(), MagicMock()
    tail = _EventTail(home / "events.jsonl")

    class _TerminalState:
        status = "completed"

    def _load_and_append(state_dir: Path) -> _TerminalState:
        with (home / "events.jsonl").open("a") as f:
            f.write(json.dumps({"type": "b"}) + "\n")
        return _TerminalState()

    monkeypatch.setattr(m.State, "load", _load_and_append)
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
        lambda control, cid, home: (seen.update(cid=cid, home=home), 7)[1])
    rc = m.main(["log", "-f", slug])
    assert rc == 7
    assert seen["cid"] == slug
    assert seen["home"] == tmp_path / ".imp" / "changes" / slug


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
        lambda c, i: [{"type": "change_created", "payload": {}}])
    monkeypatch.setattr(m, "_change_home", lambda c, i: tmp_path / "absent")
    monkeypatch.setattr(m, "_follow_log", lambda events: 9)
    rc = m.main(["log", "-f", "arch"])
    assert rc == 9


def test_follow_live_ctrl_c_clears_and_returns_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C during the poll loop clears the footer (status.reset) and exits
    130, without calling finish()."""
    import imp.cli.main as m
    status = MagicMock()
    driver = MagicMock()
    monkeypatch.setattr(m, "LiveStatus", lambda stream: status)
    monkeypatch.setattr(m, "LiveDriver", lambda **kw: driver)
    monkeypatch.setattr(m.anyio, "run", MagicMock(side_effect=KeyboardInterrupt))
    rc = m._follow_live(tmp_path, "id", tmp_path / "home")
    assert rc == 130
    status.reset.assert_called_once()
    driver.finish.assert_not_called()
