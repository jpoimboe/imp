from __future__ import annotations

import json
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli.main import main
from imp.engine.change import LAYOUT_VERSION


def _make_worktree(control: Path, slug: str, status: str = "paused") -> None:
    from imp.engine.state import State

    quest_dir = control / ".imp" / "quests" / slug
    (quest_dir / "state").mkdir(parents=True)
    (quest_dir / "quest.json").write_text(
        json.dumps({"id": slug, "title": slug, "base_branch": "main",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "layout_version": LAYOUT_VERSION}))
    events = [
        {"type": "step_started", "timestamp": "2026-01-01T00:00:00+00:00",
         "payload": {"step": "spec-writer", "phase": "spec"}},
        {"type": "step_completed", "timestamp": "2026-01-01T00:01:30+00:00",
         "payload": {"step": "spec-writer", "action": {"verb": "continue"}}},
        {"type": "step_started", "timestamp": "2026-01-01T00:01:35+00:00",
         "payload": {"step": "plan-writer", "phase": "plan"}},
        {"type": "step_completed", "timestamp": "2026-01-01T00:04:45+00:00",
         "payload": {"step": "plan-writer", "action": {"verb": "continue"}}},
    ]
    (quest_dir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))
    State(id=slug, status=status, step="plan", step_attempt=0).save(quest_dir / "state")


def _setup(tmp_path, monkeypatch, slug="sum-smoke"):
    _make_worktree(tmp_path, slug)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    return slug


def test_summary_renders_header_and_timeline(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    rc = main(["log", "-s", slug])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"·  {slug}  ·  paused  ·  4m 45s" in out
    assert "spec\n" in out
    assert "  spec-writer  1m 30s" in out
    assert "plan\n" in out
    assert "  plan-writer  3m 10s" in out


def test_summary_long_flag_alias(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    assert main(["log", "--summary", slug]) == 0
    assert "  spec-writer  1m 30s" in capsys.readouterr().out


def test_summary_and_full_are_mutually_exclusive(tmp_path, monkeypatch):
    slug = _setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["log", "-s", "--full", slug])
    assert exc.value.code == 2


def test_summary_unknown_id_errors(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    rc = main(["log", "-s", "no-such-token"])
    assert rc == 2
    assert "no Change matches" in capsys.readouterr().err


def test_summary_paused_open_step_reads_paused_not_running(tmp_path, monkeypatch, capsys):
    from imp.engine.state import State

    cid = "paused-open"
    quest_dir = tmp_path / ".imp" / "quests" / cid
    (quest_dir / "state").mkdir(parents=True)
    (quest_dir / "quest.json").write_text(json.dumps(
        {"id": cid, "title": cid, "base_branch": "main",
         "created_at": "2026-01-01T00:00:00+00:00", "layout_version": LAYOUT_VERSION}))
    events = [
        {"type": "step_started", "timestamp": "2026-01-01T00:00:00+00:00",
         "payload": {"step": "spec-writer", "phase": "spec"}},
        {"type": "step_completed", "timestamp": "2026-01-01T00:01:30+00:00",
         "payload": {"step": "spec-writer", "action": {"verb": "continue"}}},
        {"type": "step_started", "timestamp": "2026-01-01T00:01:35+00:00",
         "payload": {"step": "plan-writer", "phase": "plan"}},
    ]
    (quest_dir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))
    State(id=cid, status="paused", step="plan", step_attempt=0).save(quest_dir / "state")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    rc = main(["log", "-s", cid])
    assert rc == 0
    out = capsys.readouterr().out
    assert "  plan-writer  paused" in out
    assert "plan-writer  running" not in out


def test_summary_merged_change_via_journal_status(tmp_path, monkeypatch, capsys):
    cid = "merged-id"
    events = [
        {"type": "step_started", "timestamp": "2026-01-01T00:00:00+00:00",
         "payload": {"step": "merge-to-base", "phase": "merge"}},
        {"type": "step_completed", "timestamp": "2026-01-01T00:00:10+00:00",
         "payload": {"step": "merge-to-base", "action": {"verb": "continue"}}},
    ]
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(m, "_resolve_id", lambda control, token: cid)
    monkeypatch.setattr(m, "_load_events_for_id", lambda control, c: events)
    monkeypatch.setattr(
        m, "_row_for_id",
        lambda control, c: (cid, "merged", "merge", "2026-01-01T00:00:10+00:00",
                            cid, "2026-01-01T00:00:00+00:00"))
    rc = main(["log", "-s", cid])
    assert rc == 0
    out = capsys.readouterr().out
    assert "merged" in out
    assert "  merge-to-base  10s" in out
