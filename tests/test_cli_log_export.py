from __future__ import annotations

import json
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli.main import main
from imp.engine.quest import LAYOUT_VERSION
from imp.paths import project_state_dir

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:00:30+00:00"
T2 = "2026-01-01T00:01:30+00:00"


def _make_worktree(control: Path, slug: str, status: str = "paused") -> None:
    from imp.engine.state import State

    quest_dir = project_state_dir(control) / "quests" / slug
    (quest_dir / "state").mkdir(parents=True)
    (quest_dir / "quest.json").write_text(
        json.dumps({"id": slug, "title": slug, "base_branch": "main",
                    "created_at": T0, "layout_version": LAYOUT_VERSION}))
    events = [
        {"id": "1", "agent_id": None, "type": "quest_created",
         "timestamp": T0, "payload": {"title": "add auth"}},
        {"id": "2", "agent_id": None, "type": "step_started", "timestamp": T0,
         "payload": {"step": "spec", "kind": "agent", "phase": "design",
                     "model": "m", "provider": "anthropic"}},
        {"id": "3", "agent_id": "step:design/spec", "type": "step_context",
         "timestamp": T0,
         "payload": {"provider": "anthropic", "model": "m", "tools_schema": [],
                     "params": {}, "max_tool_calls": 40,
                     "system_prompt": "do the thing"}},
        {"id": "4", "agent_id": "step:design/spec", "type": "thinking",
         "timestamp": T1, "payload": {"content": "pondering deeply"}},
        {"id": "5", "agent_id": "step:design/spec", "type": "tool_call",
         "timestamp": T1,
         "payload": {"id": "b1", "name": "bash", "input": {"command": "ls"}}},
        {"id": "6", "agent_id": "step:design/spec", "type": "tool_result",
         "timestamp": T1,
         "payload": {"id": "b1", "content": "file-a\nfile-b", "error": False}},
        {"id": "7", "agent_id": None, "type": "token_refresh", "timestamp": T1,
         "payload": {"outcome": "skipped_fresh"}},
        {"id": "8", "agent_id": None, "type": "step_completed", "timestamp": T2,
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


def _retitle(control: Path, slug: str, **fields) -> None:
    path = project_state_dir(control) / "quests" / slug / "quest.json"
    data = json.loads(path.read_text())
    data.update(fields)
    path.write_text(json.dumps(data))


def _page_title(html: str) -> str:
    assert html.count("<title>") == 1
    return html.split("<title>", 1)[1].split("</title>", 1)[0]


def test_md_writes_default_file_and_prints_path(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    rc = main(["log", slug, "--md"])
    assert rc == 0
    out_path = tmp_path / f"imp-log-{slug}.md"
    assert out_path.exists()
    text = out_path.read_text()
    assert "# add auth" in text                 # heading spine from the tree
    assert "<details>" not in text               # lean omits folded content entirely
    assert "pondering deeply" not in text        # thinking is off in a lean export
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
    assert ("| Step | Duration | Fresh | Cache read | Cache write | Out | Cost |"
            in text)  # reworked summary table
    assert "Wait" not in text                              # Wait column dropped
    assert "<details>" not in text
    assert "| total |" in text                  # the totals row is present


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


def test_md_show_reaches_the_export_and_lean_is_smaller(tmp_path, monkeypatch):
    slug = _setup(tmp_path, monkeypatch)
    assert main(["log", slug, "--md", "a.md"]) == 0
    assert main(["log", slug, "--md", "b.md",
                 "--show", "thinking,tools,subagents,prompts,tiers"]) == 0
    lean = (tmp_path / "a.md").read_text()
    shown = (tmp_path / "b.md").read_text()
    assert len(shown) > len(lean)                       # --show now reaches the export
    assert "pondering deeply" in shown and "pondering deeply" not in lean
    assert "command=ls" in shown and "command=ls" not in lean
    assert "Internal context" in shown and "Internal context" not in lean


def test_md_hide_over_a_lean_export_is_a_no_op(tmp_path, monkeypatch):
    # Equal bytes are intent: without --full the base set is empty to subtract from.
    slug = _setup(tmp_path, monkeypatch)
    assert main(["log", slug, "--md", "a.md"]) == 0
    assert main(["log", slug, "--md", "b.md",
                 "--hide", "thinking,tools,prompts,subagents"]) == 0
    assert (tmp_path / "a.md").read_bytes() == (tmp_path / "b.md").read_bytes()


def test_md_default_export_keeps_the_recap_and_drops_the_bodies(
        tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    assert main(["log", slug, "--md", "-"]) == 0
    out = capsys.readouterr().out
    assert "<details>" not in out                  # nothing folded in a lean export
    assert "1 call" in out and "· bash" in out      # burst recap line survives
    assert "pondering deeply" not in out            # no thinking body
    assert "command=ls" not in out                  # no tool-call bodies
    assert "file-a" not in out
    assert "Internal context" not in out            # no per-step context fold
    assert "token_refresh" not in out               # no bookkeeping lines


def test_md_show_thinking_folds_only_thinking(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    assert main(["log", slug, "--md", "-", "--show", "thinking"]) == 0
    out = capsys.readouterr().out
    assert "<summary>thinking</summary>" in out
    assert "pondering deeply" in out
    assert "command=ls" not in out                  # tools still off


def test_md_full_hide_thinking_drops_it_and_keeps_the_rest_inline(
        tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    assert main(["log", slug, "--md", "-", "--full", "--hide", "thinking"]) == 0
    out = capsys.readouterr().out
    assert "pondering deeply" not in out            # --hide bites over --full
    assert "<details>" not in out                   # the rest stays inline
    assert "**1 call · bash" in out
    assert "command=ls" in out
    assert "**Internal context — anthropic/m**" in out
    assert "token_refresh" not in out               # no longer the complete set


def test_html_show_tools_carries_a_real_details_element(
        tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    assert main(["log", slug, "--html", "-", "--show", "tools"]) == 0
    out = capsys.readouterr().out
    assert "<details>" in out                       # a real element, not escaped text
    assert "&lt;details&gt;" not in out
    assert "<summary>" in out
    assert "command=ls" in out                      # tool bodies inside the fold
    assert "pondering deeply" not in out            # thinking still off


def test_html_export_renders_the_commit_list_as_a_real_list(tmp_path, monkeypatch):
    slug = _setup(tmp_path, monkeypatch)
    events_path = project_state_dir(tmp_path) / "quests" / slug / "events.jsonl"
    with events_path.open("a") as fh:
        fh.write(json.dumps(
            {"id": "5", "agent_id": None, "type": "quest_merged", "timestamp": T2,
             "payload": {"base": "main", "commit": "f" * 40,
                         "commits": [{"sha": "4091ab6d" + "0" * 32,
                                      "subject": "cli: print the commit list"}]}}) + "\n")
    assert main(["log", slug, "--html"]) == 0
    html = (tmp_path / f"imp-log-{slug}.html").read_text()
    # a genuine <li>, i.e. the Markdown list was not swallowed into the footer's
    # paragraph on the way through markdown-it
    assert "<li><code>4091ab6d</code> cli: print the commit list</li>" in html
    assert "into main as <code>ffffffff</code>.</strong>" in html


def test_html_title_names_a_titled_quest(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    _retitle(tmp_path, slug, title="Add imp doctor setup check")
    assert main(["log", slug, "--html", "-"]) == 0
    assert _page_title(capsys.readouterr().out) == f"{slug} · Add imp doctor setup check"


def test_html_summary_title_appends_summary(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    _retitle(tmp_path, slug, title="Add imp doctor setup check")
    assert main(["log", slug, "--html", "-", "--summary"]) == 0
    assert (_page_title(capsys.readouterr().out)
            == f"{slug} · Add imp doctor setup check · summary")


def test_html_title_falls_back_to_truncated_intent(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    _retitle(tmp_path, slug, title="", seed_opening="fix  the\n  " + "x" * 100)
    assert main(["log", slug, "--html", "-"]) == 0
    # whitespace flattened, cut to TITLE_CAP=70 chars including the ellipsis
    assert _page_title(capsys.readouterr().out) == f"{slug} · fix the " + "x" * 61 + "…"


def test_html_title_is_bare_id_without_title_or_intent(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    _retitle(tmp_path, slug, title="")
    assert main(["log", slug, "--html", "-"]) == 0
    assert _page_title(capsys.readouterr().out) == slug


def test_html_title_carries_no_ansi_even_when_color_is_on(tmp_path, monkeypatch, capsys):
    slug = _setup(tmp_path, monkeypatch)
    _retitle(tmp_path, slug, title="Add imp doctor setup check")
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(m, "should_use_color", lambda *_a, **_k: True)
    assert main(["log", slug, "--html", "-"]) == 0
    title = _page_title(capsys.readouterr().out)
    assert "\x1b" not in title
    assert title == f"{slug} · Add imp doctor setup check"
