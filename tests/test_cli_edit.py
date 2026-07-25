from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from imp.cli import main
from imp.engine import worktree
from imp.engine.quest import Quest
from tests.conftest import _branch_with_commit, _git, _seed_control


def _make_draft(control: Path, quest_id: str, *, origin: str = "draft") -> Quest:
    base = worktree.current_branch(control)
    return Quest.create_draft(
        control, title="Original", quest_id=quest_id, base_branch=base,
        origin=origin)


def _meta(quest: Quest) -> dict:
    return json.loads((quest.quest_dir / "quest.json").read_text())


def _edit_args(token: str, **kw) -> argparse.Namespace:
    fields = dict(token=token, title=None, backlog=False, active=False,
                  depends_on=None, clear_deps=False, append_intent=None, base=None)
    fields.update(kw)
    return argparse.Namespace(**fields)


def test_edit_title_rewrites_title(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit01")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit01", title="Renamed"))
    assert rc == 0
    assert Quest.load(tmp_path, "2026-06-24-edit01").title == "Renamed"


def test_edit_backlog_moves_to_backlog_zone(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit02", origin="draft")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit02", backlog=True))
    assert rc == 0
    assert Quest.load(tmp_path, "2026-06-24-edit02").origin == "backlog"


def test_edit_active_moves_to_draft_zone(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit03", origin="idea")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit03", active=True))
    assert rc == 0
    assert Quest.load(tmp_path, "2026-06-24-edit03").origin == "draft"


def test_edit_zone_rejected_for_materialized(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit04", origin="draft")
    meta = _meta(quest)
    meta["materialized"] = True
    (quest.quest_dir / "quest.json").write_text(json.dumps(meta, indent=2,
                                                              sort_keys=True))
    rc = main._cmd_edit(_edit_args("2026-06-24-edit04", backlog=True))
    assert rc == 2
    assert "imp:" in capsys.readouterr().err
    assert Quest.load(tmp_path, "2026-06-24-edit04").origin == "draft"


def test_edit_depends_on_replaces_list(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit05")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit05", depends_on=["A", "B"]))
    assert rc == 0
    assert _meta(Quest.load(tmp_path, "2026-06-24-edit05"))["depends_on"] == ["A", "B"]


def test_edit_clear_deps_empties_list(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit06")
    quest.set_depends_on(["A"])
    rc = main._cmd_edit(_edit_args("2026-06-24-edit06", clear_deps=True))
    assert rc == 0
    assert _meta(Quest.load(tmp_path, "2026-06-24-edit06"))["depends_on"] == []


def test_edit_no_flags_is_error(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit07")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit07"))
    assert rc == 2
    assert "imp:" in capsys.readouterr().err
    assert Quest.load(tmp_path, "2026-06-24-edit07").title == "Original"


def test_edit_combined_title_zone_and_deps(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit08", origin="draft")
    rc = main._cmd_edit(_edit_args(
        "2026-06-24-edit08", title="Combined", backlog=True, depends_on=["X"]))
    assert rc == 0
    quest = Quest.load(tmp_path, "2026-06-24-edit08")
    assert quest.title == "Combined"
    assert quest.origin == "backlog"
    assert _meta(quest)["depends_on"] == ["X"]


def test_edit_materialized_title_and_zone_rejects_whole_invocation(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit09", origin="draft")
    meta = _meta(quest)
    meta["materialized"] = True
    (quest.quest_dir / "quest.json").write_text(json.dumps(meta, indent=2,
                                                              sort_keys=True))
    rc = main._cmd_edit(
        _edit_args("2026-06-24-edit09", title="Renamed", backlog=True))
    assert rc == 2
    assert "imp:" in capsys.readouterr().err
    reloaded = Quest.load(tmp_path, "2026-06-24-edit09")
    assert reloaded.title == "Original"
    assert reloaded.origin == "draft"


def test_edit_backlog_and_active_are_mutually_exclusive() -> None:
    parser = main._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["edit", "2026-06-24-x", "--backlog", "--active"])


def test_edit_depends_on_and_clear_deps_are_mutually_exclusive() -> None:
    parser = main._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["edit", "2026-06-24-x", "--depends-on", "A", "--clear-deps"])


def test_edit_title_prints_confirmation(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit10")  # fixture title is "Original"
    rc = main._cmd_edit(_edit_args("2026-06-24-edit10", title="Renamed"))
    assert rc == 0
    assert Quest.load(tmp_path, "2026-06-24-edit10").title == "Renamed"
    assert capsys.readouterr().out.strip() == \
        'edited 2026-06-24-edit10 · title → "Renamed"'


def test_edit_append_intent_first_note_multiparagraph(
        tmp_path: Path, monkeypatch, capsys) -> None:
    # The common case: a backlog opening is `title\n\ndescription` (already
    # contains a blank line). The first note must still be separated by a blank
    # line (AC #1), not glued under the description.
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit20")
    quest.set_seed_opening("Title\n\nDescription body")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit20", append_intent="hello"))
    assert rc == 0
    assert Quest.load(tmp_path, "2026-06-24-edit20").seed_opening == \
        "Title\n\nDescription body\n\nhello"
    assert capsys.readouterr().out.strip() == \
        "edited 2026-06-24-edit20 · appended to intent"


def test_edit_append_intent_first_note_single_line(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit21")
    quest.set_seed_opening("Original opening")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit21", append_intent="hello"))
    assert rc == 0
    assert Quest.load(tmp_path, "2026-06-24-edit21").seed_opening == \
        "Original opening\n\nhello"


def test_edit_append_intent_stacks(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit22")
    quest.set_seed_opening("Original opening")
    main._cmd_edit(_edit_args("2026-06-24-edit22", append_intent="hello"))
    rc = main._cmd_edit(_edit_args("2026-06-24-edit22", append_intent="world"))
    assert rc == 0
    # Each appended note is blank-line separated (see plan `## Decisions`:
    # honors AC #1 for multi-paragraph openings; deviates from AC #2's
    # consecutive-line stacking, ratified at the merge gate).
    assert Quest.load(tmp_path, "2026-06-24-edit22").seed_opening == \
        "Original opening\n\nhello\n\nworld"


def test_edit_append_intent_empty_opening(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit23")  # seed_opening is None
    rc = main._cmd_edit(_edit_args("2026-06-24-edit23", append_intent="n"))
    assert rc == 0
    assert Quest.load(tmp_path, "2026-06-24-edit23").seed_opening == "n"


def test_edit_append_intent_rejects_materialized(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit24")
    quest.set_seed_opening("Original opening")
    meta = _meta(quest)
    meta["materialized"] = True
    (quest.quest_dir / "quest.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True))
    rc = main._cmd_edit(_edit_args("2026-06-24-edit24", append_intent="hello"))
    assert rc == 2
    assert "already started" in capsys.readouterr().err
    assert Quest.load(tmp_path, "2026-06-24-edit24").seed_opening == \
        "Original opening"


def test_edit_append_intent_rejects_conversed(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit25")
    quest.set_seed_opening("Original opening")
    quest.event_log().append(
        agent_id=None, type="user_message",
        payload={"content": [{"type": "text", "text": "hi"}], "subtype": "reply"})
    rc = main._cmd_edit(_edit_args("2026-06-24-edit25", append_intent="hello"))
    assert rc == 2
    assert "already started" in capsys.readouterr().err
    assert Quest.load(tmp_path, "2026-06-24-edit25").seed_opening == \
        "Original opening"


def test_edit_no_flags_names_append_intent(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit26")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit26"))
    assert rc == 2
    assert "--append-intent" in capsys.readouterr().err


def test_edit_append_intent_surfaces_in_show(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit27", origin="idea")
    quest.set_seed_opening("Original opening")
    main._cmd_edit(_edit_args("2026-06-24-edit27", append_intent="hello"))
    meta = _meta(Quest.load(tmp_path, "2026-06-24-edit27"))
    lines = main._backlog_card(meta, tmp_path, now=datetime.now(UTC), color=False)
    assert "hello" in "\n".join(lines)


def test_edit_append_intent_parses_as_sole_flag() -> None:
    parser = main._build_parser()
    ns = parser.parse_args(
        ["edit", "2026-06-24-x", "--append-intent", "a note"])
    assert ns.append_intent == "a note"


def test_apply_edit_sets_title_and_returns_ok(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit90")
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit90", title="Renamed", backlog=False,
        active=False, depends_on=None, clear_deps=False, append_intent=None, base=None)
    assert rc == 0
    assert msg == 'edited 2026-06-24-edit90 · title → "Renamed"'
    assert Quest.load(tmp_path, "2026-06-24-edit90").title == "Renamed"


def test_apply_edit_no_fields_returns_usage_error(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit91")
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit91", title=None, backlog=False,
        active=False, depends_on=None, clear_deps=False, append_intent=None, base=None)
    assert rc == 2
    assert msg is not None and "at least one of" in msg


def test_apply_edit_title_noop_reports_no_changes(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit92")  # fixture title is "Original"
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit92", title="Original", backlog=False,
        active=False, depends_on=None, clear_deps=False, append_intent=None, base=None)
    assert (rc, msg) == (0, "edited 2026-06-24-edit92 · no changes")


def test_apply_edit_moved_to_backlog_fragment(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit93", origin="draft")
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit93", title=None, backlog=True,
        active=False, depends_on=None, clear_deps=False, append_intent=None, base=None)
    assert (rc, msg) == (0, "edited 2026-06-24-edit93 · moved to backlog")


def test_apply_edit_backlog_noop_reports_no_changes(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit94", origin="backlog")
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit94", title=None, backlog=True,
        active=False, depends_on=None, clear_deps=False, append_intent=None, base=None)
    assert (rc, msg) == (0, "edited 2026-06-24-edit94 · no changes")


def test_apply_edit_moved_to_active_fragment(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit95", origin="idea")
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit95", title=None, backlog=False,
        active=True, depends_on=None, clear_deps=False, append_intent=None, base=None)
    assert (rc, msg) == (0, "edited 2026-06-24-edit95 · moved to active")


def test_apply_edit_active_noop_reports_no_changes(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edita2", origin="draft")  # already draft
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edita2", title=None, backlog=False,
        active=True, depends_on=None, clear_deps=False, append_intent=None, base=None)
    assert (rc, msg) == (0, "edited 2026-06-24-edita2 · no changes")


def test_apply_edit_depends_on_fragment(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit96")
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit96", title=None, backlog=False,
        active=False, depends_on=["A", "B"], clear_deps=False, append_intent=None, base=None)
    assert (rc, msg) == (0, "edited 2026-06-24-edit96 · depends on A, B")


def test_apply_edit_depends_on_noop_reports_no_changes(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit97")
    quest.set_depends_on(["A"])
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit97", title=None, backlog=False,
        active=False, depends_on=["A"], clear_deps=False, append_intent=None, base=None)
    assert (rc, msg) == (0, "edited 2026-06-24-edit97 · no changes")


def test_apply_edit_cleared_dependencies_fragment(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit98")
    quest.set_depends_on(["A"])
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit98", title=None, backlog=False,
        active=False, depends_on=None, clear_deps=True, append_intent=None, base=None)
    assert (rc, msg) == (0, "edited 2026-06-24-edit98 · cleared dependencies")


def test_apply_edit_clear_deps_noop_reports_no_changes(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit99")  # deps already empty
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit99", title=None, backlog=False,
        active=False, depends_on=None, clear_deps=True, append_intent=None, base=None)
    assert (rc, msg) == (0, "edited 2026-06-24-edit99 · no changes")


def test_apply_edit_appended_to_intent_fragment(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edita0")
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edita0", title=None, backlog=False,
        active=False, depends_on=None, clear_deps=False, append_intent="a note", base=None)
    assert (rc, msg) == (0, "edited 2026-06-24-edita0 · appended to intent")


def test_apply_edit_combined_fragments_join_with_semicolons(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edita1", origin="draft")
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edita1", title="Combined", backlog=True,
        active=False, depends_on=["X"], clear_deps=False, append_intent=None, base=None)
    assert rc == 0
    assert msg == ('edited 2026-06-24-edita1 · title → "Combined"; '
                   "moved to backlog; depends on X")


def test_apply_edit_depends_on_rejected_for_materialized(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit40")
    quest.set_depends_on(["A"])
    meta = _meta(quest)
    meta["materialized"] = True
    (quest.quest_dir / "quest.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True))
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit40", title=None, backlog=False,
        active=False, depends_on=["B"], clear_deps=False, append_intent=None, base=None)
    assert (rc, msg) == (2, "imp: cannot set dependencies on a materialized quest")
    # The rejected edit must not have mutated the dependency list.
    assert _meta(Quest.load(tmp_path, "2026-06-24-edit40"))["depends_on"] == ["A"]


def test_apply_edit_clear_deps_rejected_for_materialized(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit41")
    quest.set_depends_on(["A"])
    meta = _meta(quest)
    meta["materialized"] = True
    (quest.quest_dir / "quest.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True))
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit41", title=None, backlog=False,
        active=False, depends_on=None, clear_deps=True, append_intent=None, base=None)
    assert (rc, msg) == (2, "imp: cannot set dependencies on a materialized quest")
    assert _meta(Quest.load(tmp_path, "2026-06-24-edit41"))["depends_on"] == ["A"]


def test_edit_deps_rejected_for_materialized_via_cmd(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit42")
    quest.set_depends_on(["A"])
    meta = _meta(quest)
    meta["materialized"] = True
    (quest.quest_dir / "quest.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True))
    rc = main._cmd_edit(_edit_args("2026-06-24-edit42", depends_on=["B"]))
    assert rc == 2
    assert "cannot set dependencies on a materialized quest" in capsys.readouterr().err
    assert _meta(Quest.load(tmp_path, "2026-06-24-edit42"))["depends_on"] == ["A"]


def test_edit_depends_on_unmaterialized_still_succeeds(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit43")  # create_draft → materialized False
    rc = main._cmd_edit(_edit_args("2026-06-24-edit43", depends_on=["C"]))
    assert rc == 0
    assert _meta(Quest.load(tmp_path, "2026-06-24-edit43"))["depends_on"] == ["C"]


def _events(quest: Quest) -> list[dict]:
    path = quest.quest_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_edit_title_unmaterialized_writes_metadata_only(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit30")  # create_draft → materialized False
    rc = main._cmd_edit(_edit_args("2026-06-24-edit30", title="Renamed"))
    assert rc == 0
    quest = Quest.load(tmp_path, "2026-06-24-edit30")
    assert quest.title == "Renamed"
    assert _meta(quest)["title"] == "Renamed"
    # The one CLI mutation that would write the masked log must NOT fire.
    assert all(e["type"] != "quest_renamed" for e in _events(quest))


def test_edit_title_materialized_keeps_logged_rename(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit31")
    meta = _meta(quest)
    meta["materialized"] = True
    (quest.quest_dir / "quest.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True))
    rc = main._cmd_edit(_edit_args("2026-06-24-edit31", title="Renamed"))
    assert rc == 0
    reloaded = Quest.load(tmp_path, "2026-06-24-edit31")
    assert reloaded.title == "Renamed"
    renamed = [e for e in _events(reloaded) if e["type"] == "quest_renamed"]
    assert len(renamed) == 1
    assert renamed[0]["payload"]["new_title"] == "Renamed"


def test_edit_base_rewrites_base_and_orig_base_head(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit50")
    tip = _branch_with_commit(tmp_path, "release-2.0")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit50", base="release-2.0"))
    assert rc == 0
    meta = _meta(Quest.load(tmp_path, "2026-06-24-edit50"))
    assert meta["base_branch"] == "release-2.0"
    assert meta["orig_base_head"] == tip


def test_edit_base_rejected_for_materialized(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit51")
    _branch_with_commit(tmp_path, "release-2.0")
    before = _meta(quest)
    before["materialized"] = True
    (quest.quest_dir / "quest.json").write_text(
        json.dumps(before, indent=2, sort_keys=True))
    rc = main._cmd_edit(_edit_args("2026-06-24-edit51", base="release-2.0"))
    assert rc == 2
    assert "cannot change the base of a materialized quest" in capsys.readouterr().err
    after = _meta(Quest.load(tmp_path, "2026-06-24-edit51"))
    assert after["base_branch"] == before["base_branch"]
    assert after["orig_base_head"] == before["orig_base_head"]


def test_edit_base_nonexistent_branch_rejected(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit52")
    before = _meta(quest)
    rc = main._cmd_edit(_edit_args("2026-06-24-edit52", base="release-2.0"))
    assert rc == 2
    assert capsys.readouterr().err.strip() == \
        "imp: --base 'release-2.0' does not name an existing branch"
    assert _meta(Quest.load(tmp_path, "2026-06-24-edit52")) == before


def test_edit_no_flags_guard_names_base(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit53")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit53"))
    assert rc == 2
    assert capsys.readouterr().err.strip() == (
        "imp: imp edit needs at least one of --title, --backlog/--active, "
        "--depends-on/--clear-deps, --base, or --append-intent")


def test_apply_edit_base_fragment(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit54")
    _branch_with_commit(tmp_path, "release-2.0")
    _git(tmp_path, "checkout", "-q", "release-2.0")  # checkout agrees with the new base
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit54", title=None, backlog=False, active=False,
        depends_on=None, clear_deps=False, append_intent=None, base="release-2.0")
    assert (rc, msg) == (0, "edited 2026-06-24-edit54 · base → release-2.0")


def test_edit_base_combines_with_other_flags() -> None:
    parser = main._build_parser()
    ns = parser.parse_args(
        ["edit", "2026-06-24-x", "--title", "T", "--depends-on", "A",
         "--base", "release-2.0"])
    assert (ns.title, ns.depends_on, ns.base) == ("T", ["A"], "release-2.0")


def test_apply_edit_base_noop_preserves_orig_base_head(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    quest = _make_draft(tmp_path, "2026-06-24-edit55")  # based on main
    original = _meta(quest)["orig_base_head"]
    (tmp_path / "moved.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "advance main")
    assert _git(tmp_path, "rev-parse", "main").stdout.strip() != original
    rc, msg = main.apply_edit(
        tmp_path, "2026-06-24-edit55", title=None, backlog=False, active=False,
        depends_on=None, clear_deps=False, append_intent=None, base="main")
    assert (rc, msg) == (0, "edited 2026-06-24-edit55 · no changes")
    assert _meta(Quest.load(tmp_path, "2026-06-24-edit55"))["orig_base_head"] == original
