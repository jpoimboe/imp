from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from imp.cli.slash import REPROMPT, handle_slash_line
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_ls_builtin_lists_changes_before_skill_fallback(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="Live one", change_id="live1", base_branch="main")
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/ls", change=change, output_fn=out.append)
    printed = capsys.readouterr().out
    assert result is REPROMPT                 # a built-in, not a skill lookup
    assert "live1" in printed                 # the listing rendered
    assert not any("no such skill" in line for line in out)


def test_ls_builtin_all_flag_includes_archived_completed(tmp_path: Path, capsys) -> None:
    # `_completed_rows` reads the archive index (not the live store), so seed a
    # completed change there directly; the default `/ls` hides it and `/ls -a`
    # surfaces it, pinning that `-a` maps to all=True.
    from imp.engine import archive
    events = tmp_path / "e.jsonl"
    events.write_text("")
    change_json = tmp_path / "c.json"
    change_json.write_text(json.dumps({
        "id": "doneid", "title": "Done one", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / "s.json"
    state_json.write_text(json.dumps({"id": "doneid", "status": "completed", "step": "merge"}))
    archive.archive_change(
        tmp_path, "doneid", events_path=events,
        change_json_path=change_json, state_json_path=state_json)
    change = SimpleNamespace(control_root=tmp_path)

    assert handle_slash_line("/ls", change=change, output_fn=[].append) is REPROMPT
    assert "doneid" not in capsys.readouterr().out          # completed hidden by default

    assert handle_slash_line("/ls -a", change=change, output_fn=[].append) is REPROMPT
    assert "doneid" in capsys.readouterr().out              # -a surfaces the archive


def test_search_builtin_prints_matching_archived_change(tmp_path: Path) -> None:
    import json

    from imp.engine import archive
    events = tmp_path / "e.jsonl"
    events.write_text("")
    change_json = tmp_path / "c.json"
    change_json.write_text(json.dumps({
        "id": "darkid", "title": "Dark mode toggle", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / "s.json"
    state_json.write_text(json.dumps({"id": "darkid", "status": "abandoned", "step": "spec"}))
    archive.archive_change(
        tmp_path, "darkid", events_path=events,
        change_json_path=change_json, state_json_path=state_json)

    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/search dark", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("darkid" in line and "Dark mode toggle" in line for line in out)


def test_search_builtin_requires_a_query(tmp_path: Path) -> None:
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/search", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("Usage: /search" in line for line in out)


def test_abandon_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /abandon is a built-in (NOT a skill): it returns the literal so the session
    # gate loop (cli/session_terminal) confirms and sends CONTROL_ABANDON.
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/abandon", change=change, output_fn=out.append)
    assert result == "/abandon"
    assert not any("no such skill" in line for line in out)


def test_abandon_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append)
    assert any("/abandon" in line for line in lines)


def test_resume_builtin_rehydrates_resumable_match(tmp_path: Path) -> None:
    import json

    from imp.engine import archive
    events = tmp_path / "e.jsonl"
    events.write_text("")
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({
        "id": "conv1", "title": "Conv", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
        "materialized": False}))
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({"id": "conv1", "status": "paused", "step": "front-door"}))
    archive.archive_change(
        tmp_path, "conv1", events_path=events,
        change_json_path=cj, state_json_path=sj, resumable=True)

    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume conv1", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert (tmp_path / ".imp" / "changes" / "conv1").exists()       # rehydrated live home
    assert any("imp resume conv1" in line for line in out)          # how to continue


def test_resume_builtin_refuses_non_resumable(tmp_path: Path) -> None:
    import json

    from imp.engine import archive
    events = tmp_path / "e.jsonl"
    events.write_text("")
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({
        "id": "done1", "title": "Done", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({"id": "done1", "status": "completed", "step": "merge"}))
    archive.archive_change(
        tmp_path, "done1", events_path=events,
        change_json_path=cj, state_json_path=sj)                    # resumable False

    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume done1", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert not (tmp_path / ".imp" / "changes" / "done1").exists()   # never rehydrated
    assert any("not resumable" in line for line in out)


def test_resume_builtin_requires_a_query(tmp_path: Path) -> None:
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("Usage: /resume" in line for line in out)


def test_resume_builtin_no_match(tmp_path: Path) -> None:
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume zzz999", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("No change matches" in line for line in out)


def test_resume_builtin_ambiguous_match(tmp_path: Path) -> None:
    import json

    from imp.engine import archive

    for cid in ("conv-a", "conv-b"):
        events = tmp_path / f"{cid}.jsonl"
        events.write_text("")
        cj = tmp_path / f"{cid}.json"
        cj.write_text(json.dumps({
            "id": cid, "title": cid.title(), "base_branch": "main",
            "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
        sj = tmp_path / f"{cid}s.json"
        sj.write_text(json.dumps({"id": cid, "status": "completed", "step": "merge"}))
        archive.archive_change(
            tmp_path, cid, events_path=events,
            change_json_path=cj, state_json_path=sj)

    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume conv", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("ambiguous" in line for line in out)


def test_resume_builtin_live_match_skips_rehydration(tmp_path: Path) -> None:
    from tests.conftest import _seed_control
    _seed_control(tmp_path)
    from imp.engine.change import Change
    Change.create_draft(tmp_path, title="Live", change_id="live1", base_branch="main")

    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume live1", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("live" in line and "imp resume live1" in line for line in out)
    # live home is still there (not removed/re-created by rehydrate)
    assert (tmp_path / ".imp" / "changes" / "live1").exists()


def test_save_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /save is a built-in (NOT a skill): it returns the literal so the session gate
    # loop (cli/session_terminal) records the save intent and detaches.
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/save", change=change, output_fn=out.append)
    assert result == "/save"
    assert not any("no such skill" in line for line in out)


def test_save_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append)
    assert any("/save" in line for line in lines)
