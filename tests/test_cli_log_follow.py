"""Tests for imp log --follow: CLI surface and routing (src/imp/cli/main.py)."""

from __future__ import annotations

import argparse
import json
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
    no_pager. (Plain `imp log <id>` on a TTY does spawn one.)"""
    import imp.cli.main as m
    slug = "follow-me"
    _make_live_home(tmp_path, slug)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("PAGER", raising=False)
    _fake_tty(monkeypatch, isatty=True)
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


def test_main_log_follow_routes_through_livedriver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`imp log -f <id>` on a live home routes to _follow_log: both narrative
    events replay through LiveDriver, finish() runs, no pager, exit 0."""
    import imp.cli.main as m
    slug = "follow-me"
    _make_live_home(tmp_path, slug)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("PAGER", raising=False)
    _fake_tty(monkeypatch, isatty=False)
    driver = MagicMock()
    with patch.object(m, "LiveDriver", return_value=driver), \
         patch.object(m, "LiveStatus"), \
         patch("imp.cli.main.subprocess.Popen") as popen:
        rc = m.main(["log", "-f", slug])
    assert rc == 0
    assert popen.call_count == 0
    assert driver.on_event.call_count == 2
    driver.finish.assert_called_once()
