from __future__ import annotations

import json
from pathlib import Path

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine import archive
from imp.engine.quest import LAYOUT_VERSION
from imp.engine.state import JsonlEventLog

# ---- helpers ---------------------------------------------------------------

def _control_events_path(control: Path, cid: str) -> Path:
    """<control>/.imp/quests/<cid>/events.jsonl (the ctx's current-quest anchor)."""
    d = control / ".imp" / "quests" / cid
    d.mkdir(parents=True, exist_ok=True)
    return d / "events.jsonl"


def _ctx_at(events_path: Path) -> ToolContext:
    return ToolContext(project_root=events_path.parent, events_path=events_path)


def _call(events_path: Path, **args) -> str:
    return get_registry()["imp_show"].invoke(_ctx_at(events_path), args)


def _make_home(
    control: Path,
    cid: str,
    *,
    status: str = "completed",
    origin: str = "draft",
    materialized: bool = True,
    title: str | None = None,
    created_at: str = "2026-07-01T00:00:00+00:00",
    depends_on: list[str] | None = None,
    seed_opening: str | None = None,
    intent: str | None = None,
    summary: str | None = None,
    spec: str | None = None,
) -> Path:
    """A live quest home: quest.json (+ layout_version/project_root so Quest.load
    succeeds), state/state.json, events, and optional summary/spec artifacts.
    Returns its events.jsonl path."""
    d = control / ".imp" / "quests" / cid
    (d / "state").mkdir(parents=True, exist_ok=True)
    meta: dict = {
        "id": cid,
        "title": title or cid.title(),
        "base_branch": "main",
        "project_root": str(control),
        "layout_version": LAYOUT_VERSION,
        "created_at": created_at,
        "origin": origin,
        "materialized": materialized,
    }
    if depends_on:
        meta["depends_on"] = depends_on
    if seed_opening is not None:
        meta["seed_opening"] = seed_opening
    (d / "quest.json").write_text(json.dumps(meta))
    (d / "state" / "state.json").write_text(
        json.dumps({"id": cid, "status": status, "step": None}))
    log = JsonlEventLog(d / "events.jsonl")
    if intent is not None:
        log.append(agent_id=None, type="opening_intent", payload={"text": intent})
    if summary is not None:
        art = d / "artifacts" / "summary"
        art.mkdir(parents=True, exist_ok=True)
        (art / f"{cid}.md").write_text(summary)
    if spec is not None:
        art = d / "artifacts" / "spec"
        art.mkdir(parents=True, exist_ok=True)
        (art / f"{cid}.md").write_text(spec)
    return d / "events.jsonl"


def _git_init(root: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def _archive_home(control: Path, cid: str, *, spec: str | None = None) -> None:
    """Archive a quest with NO live home so resolution falls through to the
    archive record store. quest.json carries created_at/materialized so the
    detail card renders."""
    _git_init(control)
    events = control / f"{cid}.jsonl"
    events.write_bytes(
        b'{"id": "e0", "parent_id": null, "agent_id": null,'
        b' "timestamp": "2026-07-01T00:00:00+00:00", "type": "opening_intent",'
        b' "payload": {"text": "archived intent"}}\n')
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-07-01T00:00:00+00:00", "materialized": True}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps(
        {"id": cid, "status": "completed", "step": None}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


# ---- registration ----------------------------------------------------------

def test_imp_show_registered_and_read_only() -> None:
    tool = get_registry()["imp_show"]
    assert tool.read_only is True
    schema = tool.input_schema
    assert schema.get("type") == "object"
    assert "resource" in schema["properties"]
    assert "quest_id" in schema["properties"]


def test_imp_show_resource_enum_exported() -> None:
    schema = get_registry()["imp_show"].input_schema
    assert schema["properties"]["resource"]["enum"] == ["overview", "spec"]


def test_imp_show_in_builtin_modules() -> None:
    import imp.agents.tools as tools
    assert "imp_show" in tools._BUILTIN_MODULES


def test_imp_show_in_base_caps() -> None:
    from imp.engine.loader import BASE_CAPS
    assert "imp_show" in BASE_CAPS


def test_description_documents_overview_spec_and_imp_events() -> None:
    desc = get_registry()["imp_show"].description
    assert "Read another quest's durable record" in desc
    assert "raw spec" in desc
    assert "imp_events" in desc
    assert "current quest" in desc.lower()


# ---- overview: four zones + resolution -------------------------------------

def test_overview_started_quest_matches_detail_card(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    _make_home(
        tmp_path, "othr111", status="completed", materialized=True,
        title="Other Thing", depends_on=["dep0001"],
        intent="build the other thing", summary="It did the other thing.")
    out = _call(cur, quest_id="othr111", resource="overview")
    # Value-focused (not column-alignment-coupled): the card renders each field.
    assert "othr111" in out
    assert "Other Thing" in out
    assert "status:" in out and "completed" in out
    assert "depends_on:" in out and "dep0001" in out
    assert "build the other thing" in out
    assert "It did the other thing." in out


def test_overview_defaults_to_overview_when_resource_omitted(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    _make_home(tmp_path, "othr111", title="Other Thing", summary="Body.")
    assert _call(cur, quest_id="othr111") == _call(
        cur, quest_id="othr111", resource="overview")


def test_overview_archived_zone_resolves(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    _archive_home(tmp_path, "arch222")
    out = _call(cur, quest_id="arch222", resource="overview")
    assert "arch222" in out
    assert "status:" in out and "completed" in out
    assert "archived intent" in out


def test_overview_draft_zone_renders_status_draft(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    _make_home(tmp_path, "drft333", status="new", origin="draft",
               materialized=False, title="Draft Thing")
    out = _call(cur, quest_id="drft333", resource="overview")
    assert "drft333" in out
    # A new/paused un-materialized quest renders status: draft (resolve_display_status).
    assert "status:" in out and "draft" in out


def test_overview_backlog_zone_renders_backlog_card(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    _make_home(tmp_path, "bklg444", status="new", origin="idea",
               materialized=False, title="Backlog Idea",
               seed_opening="a backlog seed body")
    out = _call(cur, quest_id="bklg444", resource="overview")
    assert "bklg444" in out
    # These two are verbatim plain-string literals in _backlog_card (safe to match).
    assert "status:     backlog" in out
    assert "provenance: idea" in out
    assert "a backlog seed body" in out


def test_overview_unknown_id_returns_json_error_not_raise(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    err = json.loads(_call(cur, quest_id="ghost99", resource="overview"))
    assert "error" in err
    assert "ghost99" in err["error"]


def test_overview_no_quest_id_targets_current_quest(tmp_path: Path) -> None:
    cur = _make_home(tmp_path, "curr000", title="Current Quest",
                     summary="Current summary.")
    omitted = _call(cur)
    named = _call(cur, quest_id="curr000")
    assert omitted == named
    assert "curr000" in omitted
    assert "Current summary." in omitted


def test_overview_blank_quest_id_targets_current_quest(tmp_path: Path) -> None:
    cur = _make_home(tmp_path, "curr000", title="Current Quest")
    assert _call(cur, quest_id="") == _call(cur, quest_id="curr000")


# ---- spec resource ---------------------------------------------------------

def test_spec_live_returns_raw_text(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    _make_home(tmp_path, "othr111", spec="# The Spec\n\nbody line\n")
    out = _call(cur, quest_id="othr111", resource="spec")
    assert out == "# The Spec\n\nbody line\n"


def test_spec_current_quest_when_quest_id_omitted(tmp_path: Path) -> None:
    cur = _make_home(tmp_path, "curr000", spec="# Current spec\n")
    assert _call(cur, resource="spec") == "# Current spec\n"


def test_spec_missing_returns_json_error(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    _make_home(tmp_path, "othr111")  # no spec artifact written
    err = json.loads(_call(cur, quest_id="othr111", resource="spec"))
    assert "error" in err
    assert "othr111" in err["error"]
