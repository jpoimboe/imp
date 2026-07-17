from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from imp.cli import main
from imp.engine import archive
from imp.engine.quest import LAYOUT_VERSION, Quest
from imp.engine.state import State
from tests.conftest import _seed_control

_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


def test_detail_card_backlog_source(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(
        tmp_path,
        title="Add OAuth",
        quest_id="2026-06-16-bkg111",
        base_branch="main",
        origin="followup",
        depends_on=["2026-06-16-dep001"],
        seed_opening="Add OAuth\n\nwire up OAuth login",
    )
    card = "\n".join(main._detail_card(tmp_path, "2026-06-16-bkg111", now=_NOW, color=False))
    assert "2026-06-16-bkg111" in card
    assert "Add OAuth" in card
    assert "backlog" in card
    assert "followup" in card  # provenance shown
    assert "blocked on 2026-06-16-dep001" in card  # readiness from depends_on
    assert "depends_on:" in card
    assert "wire up OAuth login" in card  # seed_opening body
    created_line = next(ln for ln in card.splitlines() if ln.startswith("created:"))
    assert "+00:00" not in created_line               # tz offset trimmed on backlog cards too
    assert "." not in created_line.split("(")[0]      # no microseconds in the timestamp part


def _live_change(control: Path, quest_id: str) -> None:
    cdir = control / ".imp" / "quests" / quest_id
    cdir.mkdir(parents=True)
    (cdir / "state").mkdir()
    (cdir / "quest.json").write_text(
        json.dumps(
            {
                "id": quest_id,
                "title": "Build widget",
                "base_branch": "main",
                "created_at": "2026-06-20T11:00:00+00:00",
                "depends_on": ["2026-06-16-dep001"],
                "layout_version": LAYOUT_VERSION,
            }
        )
    )
    (cdir / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "awaiting_user_input",
                "agent_id": None,
                "timestamp": "2026-06-20T10:59:59+00:00",
                "payload": {"prompt": "what should I build?"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "user_message",
                "agent_id": None,
                "timestamp": "2026-06-20T11:00:00+00:00",
                "payload": {
                    "role": "user",
                    "content": [{"type": "text", "text": "build a widget please"}],
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "step_started",
                "agent_id": None,
                "timestamp": "2026-06-20T11:00:01+00:00",
                "payload": {
                    "step": "spec-writer",
                    "phase": "spec",
                    "phase_label": "spec phase",
                },
            }
        )
        + "\n"
    )
    State(id=quest_id, status="paused", step="spec-writer", step_label="writing spec",
          phase_label="spec phase", step_attempt=0).save(cdir / "state")


def _live_change_task_prompt(control: Path, quest_id: str) -> None:
    """A live change whose sole user_message is the composed task prompt (no
    opening_intent, no awaiting_user_input, no interject) -- the resumed-from-
    backlog shape whose card must show no prompt dump."""
    cdir = control / ".imp" / "quests" / quest_id
    cdir.mkdir(parents=True)
    (cdir / "state").mkdir()
    (cdir / "quest.json").write_text(
        json.dumps(
            {
                "id": quest_id,
                "title": "Resumed seed",
                "base_branch": "main",
                "created_at": "2026-06-20T11:00:00+00:00",
                "layout_version": LAYOUT_VERSION,
            }
        )
    )
    (cdir / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "engine_message",
                "agent_id": "step:implement/coder",
                "timestamp": "2026-06-20T11:00:00+00:00",
                "payload": {
                    "role": "user",
                    "subtype": "agent_prompt",
                    "content": [{"type": "text",
                                 "text": "## Task\nyou are a coder; do the thing"}],
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "step_started",
                "agent_id": None,
                "timestamp": "2026-06-20T11:00:01+00:00",
                "payload": {"step": "coder", "phase": "implement"},
            }
        )
        + "\n"
    )
    State(id=quest_id, status="paused", step="coder", step_attempt=0).save(cdir / "state")


def test_detail_card_omits_synthetic_task_prompt_opening(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _live_change_task_prompt(tmp_path, "2026-06-20-task01")
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-task01", now=_NOW, color=False))
    assert "2026-06-20-task01" in card          # the change card still renders
    assert "Resumed seed" in card               # title shown
    assert "you are a coder" not in card        # no prompt-dump opening
    assert "## Task" not in card


def test_detail_card_live_source(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _live_change(tmp_path, "2026-06-20-live01")
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-live01", now=_NOW, color=False))
    assert "2026-06-20-live01" in card
    assert "Build widget" in card
    assert "paused" in card
    assert "spec phase · writing spec" in card  # <phase label> · <step label>
    assert "spec-writer" not in card  # never the raw step slug
    assert "(spec)" not in card  # never the old "<step> (<phase>)" raw-slug format
    assert "main" in card  # base_branch
    assert "2026-06-16-dep001" in card  # depends_on
    assert "build a widget please" in card  # originating prompt = first user_message


def test_detail_card_sources_phase_from_persisted_state(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    cdir = tmp_path / ".imp" / "quests" / "2026-06-20-src01"
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": "2026-06-20-src01", "title": "Widget", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00", "layout_version": LAYOUT_VERSION}))
    # The event log's phase_label ("EVENT PHASE") must NOT be the card's source.
    (cdir / "events.jsonl").write_text(json.dumps({
        "type": "step_started", "agent_id": None,
        "timestamp": "2026-06-20T11:00:01+00:00",
        "payload": {"step": "spec-writer", "phase": "spec",
                    "phase_label": "EVENT PHASE"}}) + "\n")
    State(id="2026-06-20-src01", status="paused", step="spec-writer",
          step_label="writing spec", phase_label="state phase",
          step_attempt=0).save(cdir / "state")
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-src01", now=_NOW, color=False))
    assert "state phase · writing spec" in card    # sourced from persisted field
    assert "EVENT PHASE" not in card               # not the event-log scan


def test_latest_phase_label_helper_removed() -> None:
    assert not hasattr(main, "_latest_phase_label")


def test_detail_card_created_trims_microseconds_and_offset(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    cdir = tmp_path / ".imp" / "quests" / "2026-06-20-tsx001"
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": "2026-06-20-tsx001", "title": "Widget", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00.123456+00:00",
        "layout_version": LAYOUT_VERSION}))
    (cdir / "events.jsonl").write_text("")
    State(id="2026-06-20-tsx001", status="paused", step="spec-writer",
          step_attempt=0).save(cdir / "state")
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-tsx001", now=_NOW, color=False))
    created_line = next(ln for ln in card.splitlines() if ln.startswith("created:"))
    assert "2026-06-20 11:00:00" in created_line   # trimmed to seconds, space-separated
    assert ".123456" not in created_line            # microseconds dropped
    assert "+00:00" not in created_line              # tz offset dropped
    assert "(" in created_line and ")" in created_line   # friendly age still present


def test_detail_card_shows_merge_commit_for_live_quest(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    cdir = tmp_path / ".imp" / "quests" / "2026-06-20-mrg01"
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": "2026-06-20-mrg01", "title": "Widget", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00", "layout_version": LAYOUT_VERSION}))
    (cdir / "events.jsonl").write_text("")
    State(id="2026-06-20-mrg01", status="completed", step="done",
          merge_commit="deadbeefcafe", step_attempt=0).save(cdir / "state")
    lines = main._detail_card(tmp_path, "2026-06-20-mrg01", now=_NOW, color=False)
    card = "\n".join(lines)
    assert "merge_commit:  deadbeefcafe" in card          # two spaces after the colon
    base_idx = next(i for i, ln in enumerate(lines)
                    if ln.startswith("base_branch:"))
    assert lines[base_idx + 1] == "merge_commit:  deadbeefcafe"   # placed right after base_branch
    # value column aligns with the base_branch value column (15-char label field)
    assert lines[base_idx + 1].index("deadbeefcafe") == lines[base_idx].index("main")


def test_detail_card_omits_merge_commit_when_absent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    cdir = tmp_path / ".imp" / "quests" / "2026-06-20-mrg02"
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": "2026-06-20-mrg02", "title": "Widget", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00", "layout_version": LAYOUT_VERSION}))
    (cdir / "events.jsonl").write_text("")
    State(id="2026-06-20-mrg02", status="paused", step="spec-writer",
          step_attempt=0).save(cdir / "state")   # merge_commit defaults to None
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-mrg02", now=_NOW, color=False))
    assert "merge_commit" not in card             # no line when no recorded SHA


def test_quest_card_dims_labels_and_intent_header(tmp_path: Path) -> None:
    from imp.cli import palette as pal

    _seed_control(tmp_path)
    _live_change(tmp_path, "2026-06-20-dim01")
    lines = main._quest_card(tmp_path, "2026-06-20-dim01", now=_NOW, color=True)
    joined = "\n".join(lines)
    assert pal.ansi("id:", "dim") in joined              # metadata label dimmed
    assert pal.ansi("status:", "dim") in joined
    assert pal.ansi("base_branch:", "dim") in joined
    assert pal.ansi("title:", "dim") in joined
    assert pal.ansi("step:", "dim") in joined
    assert pal.ansi("created:", "dim") in joined
    assert pal.ansi("last activity:", "dim") in joined
    assert pal.ansi("intent:", "dim") in lines            # dim intent section header
    idx = lines.index(pal.ansi("intent:", "dim"))
    assert lines[idx - 1] == ""                            # one blank line precedes it
    assert lines[idx + 1] == "build a widget please"       # intent prose follows header


def test_quest_card_dims_summary_header(tmp_path: Path) -> None:
    from imp.cli import palette as pal

    _seed_control(tmp_path)
    _live_change(tmp_path, "2026-06-20-dim02")
    summary = (tmp_path / ".imp" / "quests" / "2026-06-20-dim02"
               / "artifacts" / "summary")
    summary.mkdir(parents=True)
    (summary / "2026-06-20-dim02.md").write_text("Shipped the widget.\n")
    lines = main._quest_card(tmp_path, "2026-06-20-dim02", now=_NOW, color=True)
    assert pal.ansi("summary:", "dim") in lines           # dim summary section header
    idx = lines.index(pal.ansi("summary:", "dim"))
    assert lines[idx - 1] == ""                            # one blank line precedes it
    assert lines[idx + 1] == "Shipped the widget."         # summary prose follows header


def test_quest_card_plain_without_color_keeps_headers(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _live_change(tmp_path, "2026-06-20-dim03")
    lines = main._quest_card(tmp_path, "2026-06-20-dim03", now=_NOW, color=False)
    joined = "\n".join(lines)
    assert "\x1b[" not in joined                            # no SGR escapes anywhere
    assert "intent:" in lines                               # header text still present
    assert "id:            2026-06-20-dim03" in joined      # label plain, value aligned at col 15
    la = next(line for line in lines if line.startswith("last activity:"))
    assert la[:15] == "last activity: "     # 14-char label + exactly one pad space
    st = next(line for line in lines if line.startswith("step:"))
    assert st[:15] == "step:          "     # 5-char label padded to column 15
    idx = lines.index("intent:")
    assert lines[idx - 1] == ""             # blank line precedes header in plain mode too


def test_detail_card_shows_merge_commit_for_archived_quest(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    events = tmp_path / "arc.jsonl"
    events.write_text("")
    quest_json = tmp_path / "arc.quest.json"
    quest_json.write_text(json.dumps({
        "id": "2026-06-20-arc01", "title": "Archived widget", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / "arc.state.json"
    state_json.write_text(json.dumps({
        "id": "2026-06-20-arc01", "status": "completed", "step": None,
        "merge_commit": "beefcafe1234"}))
    archive.archive_quest(
        tmp_path, "2026-06-20-arc01", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-arc01", now=_NOW, color=False))
    assert "merge_commit:  beefcafe1234" in card   # read via archive_read.read_state_json


def test_detail_card_renders_parent_line(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    # Parent quest's live home supplies the resolved title.
    phome = tmp_path / ".imp" / "quests" / "prnt09"
    phome.mkdir(parents=True)
    (phome / "quest.json").write_text(json.dumps(
        {"id": "prnt09", "title": "Parent Q", "base_branch": "main"}))
    cdir = tmp_path / ".imp" / "quests" / "2026-06-20-chld09"
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": "2026-06-20-chld09", "title": "Child", "base_branch": "main",
        "parent": "prnt09",
        "created_at": "2026-06-20T11:00:00+00:00", "layout_version": LAYOUT_VERSION}))
    (cdir / "events.jsonl").write_text("")
    State(id="2026-06-20-chld09", status="paused", step="spec-writer",
          step_attempt=0).save(cdir / "state")
    lines = main._detail_card(tmp_path, "2026-06-20-chld09", now=_NOW, color=False)
    parent_line = next(ln for ln in lines if ln.startswith("parent:"))
    assert parent_line.endswith("prnt09 · Parent Q")   # id · title, middle dot
    base_idx = next(i for i, ln in enumerate(lines) if ln.startswith("base_branch:"))
    assert lines[base_idx + 1].startswith("parent:")   # positioned right after base_branch


def test_detail_card_omits_parent_line_when_absent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    cdir = tmp_path / ".imp" / "quests" / "2026-06-20-nop09"
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": "2026-06-20-nop09", "title": "No parent", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00", "layout_version": LAYOUT_VERSION}))
    (cdir / "events.jsonl").write_text("")
    State(id="2026-06-20-nop09", status="paused", step="spec-writer",
          step_attempt=0).save(cdir / "state")
    card = "\n".join(main._detail_card(tmp_path, "2026-06-20-nop09", now=_NOW, color=False))
    assert "parent:" not in card


def _archive_dep(control: Path, qid: str, title: str) -> None:
    """Archive a dep so is_archived is True and its title resolves from the archive record."""
    events = control / f"{qid}.jsonl"
    events.write_text("")
    qj = control / f"{qid}.quest.json"
    qj.write_text(json.dumps({
        "id": qid, "title": title, "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00", "depends_on": []}))
    sj = control / f"{qid}.state.json"
    sj.write_text(json.dumps({
        "id": qid, "status": "completed", "step": None, "merge_commit": "abc123"}))
    archive.archive_quest(control, qid, events_path=events,
                          quest_json_path=qj, state_json_path=sj)


def test_detail_card_reshapes_depends_on(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    # Live (unmerged) dep resolves its title from the live home.
    lhome = tmp_path / ".imp" / "quests" / "livedp"
    lhome.mkdir(parents=True)
    (lhome / "quest.json").write_text(json.dumps(
        {"id": "livedp", "title": "Live Dep", "base_branch": "main"}))
    # Merged (archived) dep resolves from the archive.
    _archive_dep(tmp_path, "mrgdp1", "Merged Dep")
    # Parent quest's live home supplies the resolved title.
    phome = tmp_path / ".imp" / "quests" / "prntdp"
    phome.mkdir(parents=True)
    (phome / "quest.json").write_text(json.dumps(
        {"id": "prntdp", "title": "Parent P", "base_branch": "main"}))
    cdir = tmp_path / ".imp" / "quests" / "2026-06-20-chd20"
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": "2026-06-20-chd20", "title": "Child", "base_branch": "main",
        "parent": "prntdp",
        "depends_on": ["livedp", "mrgdp1"],
        "created_at": "2026-06-20T11:00:00+00:00", "layout_version": LAYOUT_VERSION}))
    (cdir / "events.jsonl").write_text("")
    State(id="2026-06-20-chd20", status="paused", step="spec-writer",
          merge_commit="def456", step_attempt=0).save(cdir / "state")
    lines = main._detail_card(tmp_path, "2026-06-20-chd20", now=_NOW, color=False)
    dep_line = next(ln for ln in lines if ln.startswith("depends_on:"))
    assert dep_line.endswith(
        "livedp · Live Dep (unmerged), mrgdp1 · Merged Dep (merged)")
    base_idx = next(i for i, ln in enumerate(lines) if ln.startswith("base_branch:"))
    parent_idx = next(i for i, ln in enumerate(lines) if ln.startswith("parent:"))
    depends_idx = next(i for i, ln in enumerate(lines) if ln.startswith("depends_on:"))
    merge_idx = next(i for i, ln in enumerate(lines) if ln.startswith("merge_commit:"))
    assert base_idx < parent_idx < depends_idx < merge_idx
    assert parent_idx == base_idx + 1
    assert depends_idx == parent_idx + 1
    assert merge_idx == depends_idx + 1


def test_detail_card_shows_shelved_for_archived_paused_quest(tmp_path: Path) -> None:
    _seed_control(tmp_path)

    # Materialized case: quest.json has no "materialized" key (defaults True).
    # Row status would be "paused"; card must show "shelved".
    events_m = tmp_path / "arc_m.jsonl"
    events_m.write_text("")
    quest_json_m = tmp_path / "arc_m.quest.json"
    quest_json_m.write_text(json.dumps({
        "id": "2026-06-20-shv01", "title": "Shelved widget", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00", "depends_on": []}))
    state_json_m = tmp_path / "arc_m.state.json"
    state_json_m.write_text(json.dumps({
        "id": "2026-06-20-shv01", "status": "paused", "step": None}))
    archive.archive_quest(
        tmp_path, "2026-06-20-shv01", events_path=events_m,
        quest_json_path=quest_json_m, state_json_path=state_json_m)

    card_m = "\n".join(main._detail_card(tmp_path, "2026-06-20-shv01", now=_NOW, color=False))
    status_line_m = next(ln for ln in card_m.splitlines() if ln.startswith("status:"))
    assert "shelved" in status_line_m
    assert "paused" not in status_line_m
    assert "draft" not in status_line_m

    # Un-materialized case: quest.json has "materialized": false.
    # Row status would be "draft" (resolve_display_status maps paused+unmaterialized->draft);
    # card must still show "shelved" because the raw final_state.status is "paused".
    events_u = tmp_path / "arc_u.jsonl"
    events_u.write_text("")
    quest_json_u = tmp_path / "arc_u.quest.json"
    quest_json_u.write_text(json.dumps({
        "id": "2026-06-20-shv02", "title": "Shelved unmaterialized", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00", "depends_on": [],
        "materialized": False}))
    state_json_u = tmp_path / "arc_u.state.json"
    state_json_u.write_text(json.dumps({
        "id": "2026-06-20-shv02", "status": "paused", "step": None}))
    archive.archive_quest(
        tmp_path, "2026-06-20-shv02", events_path=events_u,
        quest_json_path=quest_json_u, state_json_path=state_json_u)

    card_u = "\n".join(main._detail_card(tmp_path, "2026-06-20-shv02", now=_NOW, color=False))
    status_line_u = next(ln for ln in card_u.splitlines() if ln.startswith("status:"))
    assert "shelved" in status_line_u
    assert "paused" not in status_line_u
    assert "draft" not in status_line_u

    # Mid-step case: state.json has a non-null step (quest archived while a step was
    # in flight). Card must still show "shelved" and must NOT show the step: line.
    events_s = tmp_path / "arc_s.jsonl"
    events_s.write_text("")
    quest_json_s = tmp_path / "arc_s.quest.json"
    quest_json_s.write_text(json.dumps({
        "id": "2026-06-20-shv03", "title": "Shelved mid-step", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00", "depends_on": []}))
    state_json_s = tmp_path / "arc_s.state.json"
    state_json_s.write_text(json.dumps({
        "id": "2026-06-20-shv03", "status": "paused", "step": "spec-writer"}))
    archive.archive_quest(
        tmp_path, "2026-06-20-shv03", events_path=events_s,
        quest_json_path=quest_json_s, state_json_path=state_json_s)

    card_s = "\n".join(main._detail_card(tmp_path, "2026-06-20-shv03", now=_NOW, color=False))
    status_line_s = next(ln for ln in card_s.splitlines() if ln.startswith("status:"))
    assert "shelved" in status_line_s
    assert not any(ln.startswith("step:") for ln in card_s.splitlines())
