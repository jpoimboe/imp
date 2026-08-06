"""Workflow snapshot refresh on run_quest entry (spec: Workflow snapshot
refresh on resume). Covers the content-comparison + crash-safe refresh
helpers and the run_quest wiring / workflow_refreshed event."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from imp.agents.skills import load_internal_skill
from imp.assets import bundled_skills_dir
from imp.engine.executor import (
    RunResult,
    _backfill_var_defaults,
    _reconcile_resolved_run,
    _refresh_workflow_snapshot,
    _trees_identical,
    run_quest,
)
from imp.engine.loader import load_workflow
from imp.engine.state import State
from imp.engine.types import SourceLoc, VarDecl, Vars
from imp.providers.config import ProviderModel
from imp.providers.effective import load_effective_config
from tests.test_executor import _register_fake, _seed_minimal_workflow

_VSRC = SourceLoc(file=Path("wf"), line=1)


def _init_git(root: Path) -> None:
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True)


def _git_commit_all(root: Path, msg: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", msg],
        check=True, capture_output=True)


def _state_with_vars(vars_: dict) -> State:
    return State(id="c", step="s", step_attempt=0, status="running", vars=vars_)


def test_backfill_seeds_var_declared_after_quest_created() -> None:
    """A var added to the workflow after a change was created is missing from that
    change's persisted vars (defaults are seeded only at creation). Backfill adds
    its default so a StrictUndefined template reference doesn't crash on resume."""
    state = _state_with_vars({"fast_path": False})
    decls = Vars((
        VarDecl(name="fast_path", type="bool", default=False, source=_VSRC),
        VarDecl(name="auto_merge", type="bool", default=True, source=_VSRC),
    ))
    added = _backfill_var_defaults(state, decls)
    assert added == ["auto_merge"]
    assert state.vars == {"fast_path": False, "auto_merge": True}


def test_backfill_never_overwrites_existing_value() -> None:
    """An already-set var keeps its persisted value, even one that differs from
    the declared default."""
    state = _state_with_vars({"auto_merge": False})
    decls = Vars((VarDecl(name="auto_merge", type="bool", default=True, source=_VSRC),))
    added = _backfill_var_defaults(state, decls)
    assert added == []
    assert state.vars == {"auto_merge": False}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_trees_identical_matches_same_bytes(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        (d / "sub").mkdir(parents=True)
        (d / "top.txt").write_text("hello")
        (d / "sub" / "n.yaml").write_text("k: 1\n")
    assert _trees_identical(a, b) is True


def test_trees_identical_detects_byte_difference(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("v1")
    (b / "f.txt").write_text("v2")
    assert _trees_identical(a, b) is False


def test_trees_identical_detects_missing_path(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("x")
    (a / "extra.txt").write_text("y")
    (b / "f.txt").write_text("x")
    assert _trees_identical(a, b) is False


def test_refresh_fresh_change_makes_initial_copy_no_flag(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    snapshot = quest.quest_dir / "workflow"
    assert not snapshot.exists()
    # A fresh change's initial snapshot is NOT a "refresh": returns False so
    # run_quest emits no workflow_refreshed event.
    assert _refresh_workflow_snapshot(quest) is False
    assert (snapshot / "workflow.yaml").exists()
    assert (snapshot / "agents" / "writer.md").exists()


def test_refresh_identical_source_no_swap(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    assert _refresh_workflow_snapshot(quest) is False        # initial copy
    snapshot = quest.quest_dir / "workflow"
    before = (snapshot / "workflow.yaml").read_bytes()
    assert _refresh_workflow_snapshot(quest) is False        # unchanged source
    assert (snapshot / "workflow.yaml").read_bytes() == before
    assert not (quest.quest_dir / "workflow.tmp").exists()


def test_refresh_changed_source_replaces_snapshot(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(quest)
    live_yaml = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    live_yaml.write_text(live_yaml.read_text() + "# edited-live\n")
    assert _refresh_workflow_snapshot(quest) is True
    snapshot = quest.quest_dir / "workflow"
    assert "# edited-live" in (snapshot / "workflow.yaml").read_text()
    assert not (quest.quest_dir / "workflow.tmp").exists()
    assert not (quest.quest_dir / "workflow.old").exists()


def test_refresh_missing_live_source_keeps_snapshot(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(quest)
    snapshot = quest.quest_dir / "workflow"
    before = (snapshot / "workflow.yaml").read_bytes()
    # Point the default workflow at a name with no committed dir and no bundle:
    # resolve_workflow_dir then RAISES (unknown workflow), so the live source is
    # unavailable and refresh is a best-effort no-op. (Removing the committed
    # imp/workflows/imp does NOT do this — the bundled "default" workflow is a live
    # fallback, so live_workflow_dir always resolves to an existing dir.)
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: nosuch\n")
    assert _refresh_workflow_snapshot(quest) is False
    assert (snapshot / "workflow.yaml").read_bytes() == before


def test_refresh_unresolvable_live_source_keeps_snapshot(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(quest)
    snapshot = quest.quest_dir / "workflow"
    before = (snapshot / "workflow.yaml").read_bytes()
    # A malformed config.yaml makes the layered resolver RAISE LoadError — the
    # whole project layer fails to parse, not just this key — which propagates
    # through control_workflow_dir and is caught by the helper's best-effort
    # except → snapshot kept, returns False. (A MISSING config.yaml would NOT
    # raise: no layer sets default-workflow, so the resolver returns "default".)
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: [bad yaml\n")
    assert _refresh_workflow_snapshot(quest) is False
    assert (snapshot / "workflow.yaml").read_bytes() == before


def test_refresh_cleans_stale_tmp(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(quest)
    tmp = quest.quest_dir / "workflow.tmp"
    tmp.mkdir()
    (tmp / "stale.yaml").write_text("garbage")
    _refresh_workflow_snapshot(quest)
    assert not tmp.exists()
    assert (quest.quest_dir / "workflow" / "workflow.yaml").exists()


def test_refresh_recovers_workflow_old_when_workflow_missing(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(quest)
    snapshot = quest.quest_dir / "workflow"
    old = quest.quest_dir / "workflow.old"
    os.rename(snapshot, old)                                   # crash mid-swap
    # Make the live source unresolvable too (unknown workflow name), so the
    # snapshot can ONLY reappear if workflow.old was renamed back — never via a
    # fresh copy. (rmtree of imp/workflows/imp would fall back to the bundled
    # "default" workflow, which is a live source, defeating the point.)
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: nosuch\n")
    assert not snapshot.exists()
    assert _refresh_workflow_snapshot(quest) is False
    assert snapshot.is_dir()
    assert (snapshot / "workflow.yaml").exists()
    assert not old.exists()


def test_refresh_deletes_leftover_workflow_old(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(quest)
    snapshot = quest.quest_dir / "workflow"
    old = quest.quest_dir / "workflow.old"
    old.mkdir()
    (old / "junk.yaml").write_text("stale")
    _refresh_workflow_snapshot(quest)
    assert not old.exists()
    assert (snapshot / "workflow.yaml").exists()


@pytest.mark.anyio
async def test_resume_refreshes_unloadable_snapshot(tmp_path: Path) -> None:
    """The pohe33 regression: a frozen snapshot the CURRENT loader rejects is
    replaced from a valid live source on entry, so the run proceeds instead of
    crashing in load_workflow."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    # A snapshot using removed `chrome:` syntax: a mapping with no `phases`,
    # which load_workflow rejects with LoadError("missing 'phases' list").
    snapshot = quest.quest_dir / "workflow"
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "workflow.yaml").write_text("chrome: removed-syntax\n")

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    _refresh_workflow_snapshot(quest)
    assert "chrome" not in (snapshot / "workflow.yaml").read_text()
    assert sum(1 for e in events if e["type"] == "workflow_refreshed") == 1


@pytest.mark.anyio
async def test_unchanged_resume_emits_no_refresh_event(tmp_path: Path) -> None:
    """Identical live source across entries → no swap and no workflow_refreshed
    event (neither the fresh initial copy nor an unchanged re-entry emits)."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    events: list[dict] = []
    await run_quest(quest, on_event=events.append)   # fresh: initial copy
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    _refresh_workflow_snapshot(quest)
    await run_quest(quest, on_event=events.append)   # unchanged: no-op
    assert not [e for e in events if e["type"] == "workflow_refreshed"]


@pytest.mark.anyio
async def test_refresh_dropping_cursor_step_pauses_unknown_step(tmp_path: Path) -> None:
    """A refreshed workflow that no longer contains the cursor step pauses with
    an unknown_step event instead of silent-merging."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)   # cursor seeded at step "w"
    _register_fake({"status": "Success"})
    _refresh_workflow_snapshot(quest)                 # snapshot now contains "w"

    # Drop step "w" from the LIVE source (rename it); the cursor stays at "w".
    wf_yaml = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    wf_yaml.write_text(wf_yaml.read_text().replace("name: w,", "name: renamed,"))

    result = await run_quest(quest, on_event=lambda _e: None)

    assert result is RunResult.PAUSED
    events = list(quest.event_log().read())
    paused = [e for e in events if e.get("type") == "workflow_paused"]
    assert paused and paused[-1]["payload"]["reason"] == "unknown_step"
    assert paused[-1]["payload"]["step"] == "w"
    assert not any(e.get("type") == "quest_merged" for e in events)
    # run_quest's own entry refresh swapped the "w" snapshot to the new live
    # version, which is what drops the cursor step.
    assert "name: renamed" in (
        quest.quest_dir / "workflow" / "workflow.yaml").read_text()


@pytest.mark.anyio
async def test_self_edit_not_adopted_same_process(tmp_path: Path) -> None:
    """AC 1(b): the snapshot is captured once per process. A live-source edit
    made AFTER the first entry is NOT adopted on a later entry in the same
    process — no swap, no workflow_refreshed event. (A merged workflow change is
    adopted only on the next fresh daemon.)"""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    await run_quest(quest, on_event=lambda _e: None)          # first entry: captures
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    _refresh_workflow_snapshot(quest)

    writer = tmp_path / "imp" / "workflows" / "imp" / "agents" / "writer.md"
    writer.write_text(writer.read_text() + "\nlate-edit\n")

    # The snapshot is decoupled from the live source: the live edit above does
    # not leak into the already-captured snapshot.
    snap_writer = quest.quest_dir / "workflow" / "agents" / "writer.md"
    assert "late-edit" not in snap_writer.read_text()

    events: list[dict] = []
    await run_quest(quest, on_event=events.append)            # second entry: gated
    assert not [e for e in events if e["type"] == "workflow_refreshed"]


@pytest.mark.anyio
async def test_new_quest_snapshots_on_first_entry(tmp_path: Path) -> None:
    """AC 1(c): a brand-new quest still gets its initial snapshot on the first
    run_quest entry (the gate skips only second-and-later entries)."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    assert not (quest.quest_dir / "workflow").exists()

    await run_quest(quest, on_event=lambda _e: None)
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    _refresh_workflow_snapshot(quest)
    assert (quest.quest_dir / "workflow" / "workflow.yaml").exists()


def _load_workflow_and_config(quest):
    """Load the refreshed snapshot workflow + config the way run_quest does."""
    config = load_effective_config(control_root=quest.control_root).config
    workflow = load_workflow(quest.workflow_dir)
    return workflow, config


@pytest.mark.anyio
async def test_reconcile_adds_missing_tier_keeps_pinned(tmp_path: Path) -> None:
    """A tier absent from the persisted map is filled in from a fresh resolve;
    an already-present (pinned) tier is left exactly as it was."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    await run_quest(quest, on_event=lambda _e: None)   # persists {low,medium,high}
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    _refresh_workflow_snapshot(quest)
    workflow, config = _load_workflow_and_config(quest)

    state = State.load(quest.state_dir)
    del state.resolved_tiers["high"]                     # simulate a stale map
    state.resolved_tiers["low"] = ProviderModel(provider="fake", model="pinned-low")

    assert _reconcile_resolved_run(state, workflow, config) is True
    assert state.resolved_tiers["high"].provider == "fake"   # re-added
    assert state.resolved_tiers["low"].model == "pinned-low"  # NOT overwritten


@pytest.mark.anyio
async def test_reconcile_no_missing_returns_false_no_overwrite(tmp_path: Path) -> None:
    """When every tier/step key is already present the helper adds nothing,
    returns False, and never overwrites a pinned model (fresh resolve would
    give model 'm'; the pin must survive)."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    await run_quest(quest, on_event=lambda _e: None)
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    _refresh_workflow_snapshot(quest)
    workflow, config = _load_workflow_and_config(quest)

    state = State.load(quest.state_dir)
    state.resolved_tiers["low"] = ProviderModel(provider="fake", model="pinned-low")

    assert _reconcile_resolved_run(state, workflow, config) is False
    assert state.resolved_tiers["low"].model == "pinned-low"


@pytest.mark.anyio
async def test_reconcile_adds_missing_step_override(tmp_path: Path) -> None:
    """A new differ_from step present in the refreshed workflow but absent from
    the persisted resolved_steps is filled in."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    await run_quest(quest, on_event=lambda _e: None)   # resolved_steps == {}
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done

    # Add a differ_from step to the live workflow, then refresh the snapshot.
    wf = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    wf.write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
        "      - {name: w2, file: agents/writer.md, tier: high, differ_from: w}\n"
    )
    _refresh_workflow_snapshot(quest)
    workflow, config = _load_workflow_and_config(quest)

    state = State.load(quest.state_dir)
    assert "w2" not in state.resolved_steps
    assert _reconcile_resolved_run(state, workflow, config) is True
    assert "w2" in state.resolved_steps


@pytest.mark.anyio
async def test_resume_reconciles_new_step_override_keeps_pinned(tmp_path: Path) -> None:
    """A refreshed workflow that adds a new differ_from step → reconcile fills in
    resolved_steps and leaves a pre-pinned tier entry untouched. A new *tier* key
    is integration-unrealizable (config.tiers is always low/medium/high), so the
    tier-merge half is unit-tested instead."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    await run_quest(quest, on_event=lambda _e: None)   # resolves {low,medium,high}
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    _refresh_workflow_snapshot(quest)

    # Pin low to a sentinel so we can prove reconcile never overwrites it.
    state = State.load(quest.state_dir)
    state.resolved_tiers["low"] = ProviderModel(provider="fake", model="pinned-low")
    state.save(quest.state_dir)

    # The workflow gains a new differ_from step w2 (legal tier: high).
    wf = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    wf.write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
        "      - {name: w2, file: agents/writer.md, tier: high, differ_from: w}\n"
    )

    # A merged workflow change is adopted on a FRESH daemon, not a same-process
    # re-entry: clear the once-per-process gate to model that process restart.
    from imp.engine import executor as _executor
    _executor._SNAPSHOT_CAPTURED.clear()
    await run_quest(quest, on_event=lambda _e: None)   # fresh process: refresh + reconcile
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done

    state = State.load(quest.state_dir)
    assert "w2" in state.resolved_steps                        # new step override merged
    assert state.resolved_tiers["low"].model == "pinned-low"   # pinned entry preserved


@pytest.mark.anyio
async def test_var_added_after_creation_backfilled_no_render_crash(tmp_path: Path) -> None:
    """A change created before a var existed has that var missing from its persisted
    vars. When the workflow is updated to declare the var AND a template references
    it under StrictUndefined, resume must backfill the default rather than crash the
    render (regression: integrate/finalize paused on `vars.auto_merge` undefined)."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    # state.vars was seeded from the original (var-less) workflow.
    assert "auto_merge" not in State.load(quest.state_dir).vars

    # Update the LIVE workflow: declare the new var and have the agent reference it.
    wf_dir = tmp_path / "imp" / "workflows" / "imp"
    (wf_dir / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "vars:\n  auto_merge: {type: bool, default: true}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (wf_dir / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go {{ quest.id }} merge={{ vars.auto_merge }}\n"
    )
    _register_fake({"status": "Success"})

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)

    assert result is RunResult.COMPLETED
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    assert State.load(quest.state_dir).vars["auto_merge"] is True
    assert any(e["type"] == "vars_backfilled"
               and e["payload"]["names"] == ["auto_merge"] for e in events)


@pytest.mark.anyio
async def test_unchanged_workflow_resume_skips_reconcile(tmp_path: Path) -> None:
    """Reconciliation is gated on an actual content refresh: an unchanged-workflow
    resume does not re-run resolve_run, so a tier removed from the persisted map
    is NOT silently re-added (no churn on the common resume path)."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _register_fake({"status": "Success"})
    await run_quest(quest, on_event=lambda _e: None)
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    _refresh_workflow_snapshot(quest)

    state = State.load(quest.state_dir)
    del state.resolved_tiers["high"]
    state.save(quest.state_dir)

    await run_quest(quest, on_event=lambda _e: None)   # workflow byte-identical
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    state = State.load(quest.state_dir)
    assert "high" not in state.resolved_tiers


def test_refresh_sources_from_control_root_not_worktree(tmp_path: Path) -> None:
    """The snapshot is copied from the control root's workflow, NOT the change's
    worktree — the imp-on-imp / divergent-worktree case. Before the fix the copy
    source was the worktree, so the snapshot would carry WORKTREE-EDIT."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)   # control-root workflow: name: t
    # A divergent worktree whose committed workflow differs from the control root.
    wt = tmp_path / "wt"
    wt_wf = wt / "imp" / "workflows" / "imp"
    (wt_wf / "agents").mkdir(parents=True)
    (wt_wf / "agents" / "writer.md").write_text("worktree-divergent\n")
    (wt_wf / "workflow.yaml").write_text("name: WORKTREE-EDIT\n")
    quest.project_root = wt

    assert _refresh_workflow_snapshot(quest) is False   # initial copy, no flag
    snap = quest.quest_dir / "workflow" / "workflow.yaml"
    assert "WORKTREE-EDIT" not in snap.read_text()
    assert "name: t" in snap.read_text()                 # the control-root content


def test_refresh_overlays_global_skills_into_snapshot(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    # The minimal live workflow ships no skills/ dir, so any global skill in the
    # snapshot can ONLY have arrived via overlay_global_skills.
    assert _refresh_workflow_snapshot(quest) is False        # initial copy
    snap_skills = quest.quest_dir / "workflow" / "skills"
    for name in ("imp-knowledge", "imp-tool-reference",
                 "imp-driving-terminal-uis", "imp-driving-web-uis"):
        assert (snap_skills / f"{name}.md").is_file(), name


def test_refresh_ignores_worktree_edits_stays_noop(tmp_path: Path) -> None:
    """With the source anchored at the control root, editing ONLY the worktree
    leaves the refresh a no-op (no swap, snapshot bytes unchanged) — a change
    cannot self-poison its snapshot with its own in-flight workflow edits."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    wt = tmp_path / "wt"
    wt_wf = wt / "imp" / "workflows" / "imp"
    wt_wf.mkdir(parents=True)
    (wt_wf / "workflow.yaml").write_text("name: v1-worktree\n")
    quest.project_root = wt

    assert _refresh_workflow_snapshot(quest) is False   # initial copy from control
    snap = quest.quest_dir / "workflow" / "workflow.yaml"
    before = snap.read_bytes()
    # Edit ONLY the worktree; the control-root source is untouched.
    (wt_wf / "workflow.yaml").write_text("name: v2-worktree-edit\n")

    assert _refresh_workflow_snapshot(quest) is False   # no-op: source unchanged
    assert snap.read_bytes() == before
    assert not (quest.quest_dir / "workflow.tmp").exists()


def test_refresh_workflow_skill_shadows_global_on_name_collision(tmp_path: Path) -> None:
    """End-to-end workflow-wins precedence: a workflow that ships its own
    skills/imp-knowledge.md shadows the bundled global pool skill of the same
    name through the snapshot builder — the workflow copy is what lands and what
    load_internal_skill resolves — while the other pool skills still overlay."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    live_skills = tmp_path / "imp" / "workflows" / "imp" / "skills"
    live_skills.mkdir(parents=True)
    sentinel = "# imp-knowledge\nWORKFLOW-OWN COPY — not the global pool.\n"
    (live_skills / "imp-knowledge.md").write_text(sentinel)

    assert _refresh_workflow_snapshot(quest) is False        # initial copy
    snap_skills = quest.quest_dir / "workflow" / "skills"

    # copytree lands the workflow's own file first, so overlay_global_skills
    # skips the colliding name: the snapshot holds the workflow copy verbatim.
    assert (snap_skills / "imp-knowledge.md").read_text() == sentinel
    pool_copy = (bundled_skills_dir() / "imp-knowledge.md").read_text()
    assert (snap_skills / "imp-knowledge.md").read_text() != pool_copy
    # The resolver returns the workflow copy (no frontmatter on the sentinel).
    assert load_internal_skill(snap_skills, "imp-knowledge") == sentinel
    # …and the non-colliding pool skills still overlay alongside it.
    assert (snap_skills / "imp-tool-reference.md").read_text().strip()


def test_snapshot_does_not_copy_the_docs_corpus(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(quest)
    snapshot = quest.quest_dir / "workflow"
    assert snapshot.is_dir()
    # The corpus is resolved in place, never overlaid — the snapshot carries
    # no docs/ copy (contrast with skills/, which IS overlaid).
    assert not (snapshot / "docs").exists()
    assert (snapshot / "skills").is_dir()


def test_capture_pins_to_committed_head_ignoring_dirty_worktree(
        tmp_path: Path) -> None:
    """AC 2(a): with a git-tracked workflow source, the capture reads the
    committed HEAD tree, never the working tree. An uncommitted working-tree
    edit (a merge-queue write mid-flight) is NOT adopted, so the snapshot can
    never be torn or newer than a single committed HEAD."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    (tmp_path / ".gitignore").write_text(".imp/\n.imp-isolated/\n")
    _init_git(tmp_path)
    _git_commit_all(tmp_path, "wf v1")
    live_yaml = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    committed = live_yaml.read_text()
    live_yaml.write_text(committed + "# uncommitted-merge-write\n")

    assert _refresh_workflow_snapshot(quest) is False        # initial capture
    snap_yaml = quest.quest_dir / "workflow" / "workflow.yaml"
    assert "uncommitted-merge-write" not in snap_yaml.read_text()
    assert snap_yaml.read_text() == committed


def test_capture_resolves_workflow_symlink_to_tracked_target(
        tmp_path: Path) -> None:
    """The imp self-hosting layout: imp/workflows/imp is a SYMLINK to the real
    tracked tree (src/imp/assets/workflows/default). The capture must resolve the
    symlink to that real path and archive HEAD of the target tree."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    wf = tmp_path / "imp" / "workflows" / "imp"
    real = tmp_path / "src" / "wfreal"
    real.parent.mkdir(parents=True)
    shutil.move(str(wf), str(real))
    wf.symlink_to(os.path.relpath(real, wf.parent))
    (tmp_path / ".gitignore").write_text(".imp/\n.imp-isolated/\n")
    _init_git(tmp_path)
    _git_commit_all(tmp_path, "wf via symlink")
    # Dirty the working tree of the real target; capture must pin to HEAD.
    real_yaml = real / "workflow.yaml"
    real_yaml.write_text(real_yaml.read_text() + "# dirty\n")

    assert _refresh_workflow_snapshot(quest) is False
    snap = quest.quest_dir / "workflow"
    assert (snap / "workflow.yaml").exists()
    assert "# dirty" not in (snap / "workflow.yaml").read_text()
    assert (snap / "agents" / "writer.md").exists()


def test_capture_non_git_source_uses_plain_copy(tmp_path: Path) -> None:
    """AC 2(b): a non-git-tracked source (a normal user project's static
    workflow) falls back to the plain copy, so the snapshot equals the live
    tree — including a working-tree edit, since there is no HEAD to pin to."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)   # tmp_path is NOT a git repo
    live_yaml = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    live_yaml.write_text(live_yaml.read_text() + "# live-only\n")

    assert _refresh_workflow_snapshot(quest) is False
    snap_yaml = quest.quest_dir / "workflow" / "workflow.yaml"
    assert "# live-only" in snap_yaml.read_text()


def test_git_capture_replaces_snapshot_crash_safe(tmp_path: Path) -> None:
    """AC 5 on the git path: a committed workflow change IS adopted on a fresh
    capture, and the .tmp/.old swap leaves no crash artifacts behind."""
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    (tmp_path / ".gitignore").write_text(".imp/\n.imp-isolated/\n")
    _init_git(tmp_path)
    _git_commit_all(tmp_path, "wf v1")
    assert _refresh_workflow_snapshot(quest) is False        # initial git capture

    live_yaml = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    live_yaml.write_text(live_yaml.read_text() + "# committed-v2\n")
    _git_commit_all(tmp_path, "wf v2")

    assert _refresh_workflow_snapshot(quest) is True         # committed change adopted
    snap = quest.quest_dir / "workflow"
    assert "# committed-v2" in (snap / "workflow.yaml").read_text()
    assert not (quest.quest_dir / "workflow.tmp").exists()
    assert not (quest.quest_dir / "workflow.old").exists()
