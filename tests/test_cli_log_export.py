from __future__ import annotations

import json
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli.main import main
from imp.engine.quest import LAYOUT_VERSION

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:00:30+00:00"
T2 = "2026-01-01T00:01:30+00:00"


def _make_worktree(control: Path, slug: str, status: str = "paused") -> None:
    from imp.engine.state import State

    quest_dir = control / ".imp" / "quests" / slug
    (quest_dir / "state").mkdir(parents=True)
    (quest_dir / "quest.json").write_text(
        json.dumps({"id": slug, "title": slug, "base_branch": "main",
                    "created_at": T0, "layout_version": LAYOUT_VERSION}))
    events = [
        {"id": "1", "agent_id": None, "type": "change_created",
         "timestamp": T0, "payload": {"title": "add auth"}},
        {"id": "2", "agent_id": None, "type": "step_started", "timestamp": T0,
         "payload": {"step": "spec", "kind": "agent", "phase": "design",
                     "model": "m", "provider": "anthropic"}},
        {"id": "3", "agent_id": "step:design/spec", "type": "thinking",
         "timestamp": T1, "payload": {"content": "pondering deeply"}},
        {"id": "4", "agent_id": None, "type": "step_completed", "timestamp": T2,
         "payload": {"step": "spec", "kind": "agent",
                     "action": {"verb": "continue"}, "var_mutations": {}}},
    ]
    (quest_dir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))
    State(id=slug, status=status, step="design",
          step_attempt=0).save(quest_dir / "state")


def _setup(tmp_path, monkeypatch, slug="exp-smoke"):
    _make_worktree(tmp_path, slug)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    return slug


def test_md_writes_default_file_and_prints_path(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    rc = main(["log", slug, "--md"])
    assert rc == 0
    out_path = tmp_path / f"imp-log-{slug}.md"
    assert out_path.exists()
    text = out_path.read_text()
    assert "# add auth" in text                 # heading spine from the tree
    assert "<details>" in text                   # thinking fold collapsed by default
    assert f"imp-log-{slug}.md" in capsys.readouterr().out


def test_html_writes_default_file(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    rc = main(["log", slug, "--html"])
    assert rc == 0
    out_path = tmp_path / f"imp-log-{slug}.html"
    assert out_path.exists()
    html = out_path.read_text()
    assert "<!DOCTYPE html>" in html             # full HTML page, not a fragment
    assert "<link" not in html                   # self-contained, no external ref
    assert "add auth" in html                     # narrative wired through end to end
    assert f"imp-log-{slug}.html" in capsys.readouterr().out


def test_md_full_expands_folds(tmp_path, monkeypatch):
    slug = _setup(tmp_path, monkeypatch)
    assert main(["log", slug, "--md", "--full"]) == 0
    text = (tmp_path / f"imp-log-{slug}.md").read_text()
    assert "<details>" not in text               # --full expands inline
    assert "**thinking**" in text
    assert "pondering deeply" in text


def test_html_and_md_are_mutually_exclusive(tmp_path, monkeypatch):
    slug = _setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["log", slug, "--html", "--md"])
    assert exc.value.code == 2


def test_md_dash_streams_to_stdout(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    rc = main(["log", slug, "--md", "-"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# add auth" in out                   # document streamed, not a path
    assert not (tmp_path / f"imp-log-{slug}.md").exists()


def test_html_dash_streams_to_stdout(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    rc = main(["log", slug, "--html", "-"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "<!DOCTYPE html>" in out
    assert not (tmp_path / f"imp-log-{slug}.html").exists()


def test_md_explicit_path_writes_there(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    rc = main(["log", slug, "--md", "out.md"])
    assert rc == 0
    assert (tmp_path / "out.md").read_text().startswith("# add auth")
    assert not (tmp_path / f"imp-log-{slug}.md").exists()
    assert "out.md" in capsys.readouterr().out


def test_html_explicit_path_writes_there(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    rc = main(["log", slug, "--html", "out.html"])
    assert rc == 0
    assert "<!DOCTYPE html>" in (tmp_path / "out.html").read_text()
    assert not (tmp_path / f"imp-log-{slug}.html").exists()
    assert "out.html" in capsys.readouterr().out


def test_md_summary_uses_summary_document(tmp_path, monkeypatch):
    slug = _setup(tmp_path, monkeypatch)
    assert main(["log", slug, "--md", "--summary"]) == 0
    text = (tmp_path / f"imp-log-{slug}.md").read_text()
    assert "**Status:**" in text                 # SummaryHeader line
    assert "| Step | Duration | Tokens | Cost |" in text  # reworked summary table
    assert "Wait" not in text                              # Wait column dropped
    assert "<details>" not in text


def test_html_summary_renders_header(tmp_path, monkeypatch):
    slug = _setup(tmp_path, monkeypatch)
    assert main(["log", slug, "--html", "--summary"]) == 0
    html = (tmp_path / f"imp-log-{slug}.html").read_text()
    assert "<!DOCTYPE html>" in html
    assert "<strong>Status:</strong>" in html     # bold header carried into HTML


def test_export_never_pages(tmp_path, monkeypatch):
    slug = _setup(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise AssertionError("export must not page")

    monkeypatch.setattr(m, "_page_or_print", _boom)
    assert main(["log", slug, "--md", "-"]) == 0
    assert main(["log", slug, "--html"]) == 0
    assert main(["log", slug, "--md", "--summary"]) == 0
    assert main(["log", slug, "--html", "--summary"]) == 0
