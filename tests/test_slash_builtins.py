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
