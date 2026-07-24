"""Quest lifecycle (spec §3, §5): immutable id identity, free-form title, worktree
creation, permanent state home at <control_root>/.imp/quests/<id>/."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from imp.engine import worktree
from imp.engine.env import quest_var_tmp_root
from imp.engine.errors import LayoutMismatch, LoadError, LoadErrors, MigrationLockHeld
from imp.engine.event_migrations import migrate_events
from imp.engine.seed import seed_workflow_start
from imp.engine.state import (
    JsonlEventLog,
    LockHeld,
    ProjectLock,
    State,
    classify_pause_halt,
    parse_events_jsonl,
)
from imp.engine.types import SourceLoc

LAYOUT_VERSION = 13  # bump in the SAME commit as any breaking quest-home layout/schema change


def _write_quest_json(meta_path: Path, meta: dict[str, Any]) -> None:
    """Atomically rewrite quest.json (tmp file, fsync, os.replace); mirrors State.save."""
    fd, tmp_name = tempfile.mkstemp(prefix=".quest-", dir=meta_path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, meta_path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _fold_phantom(phantom: Path, home: Path) -> None:
    """Move phantom-only files into home; home wins on collision."""
    for src in phantom.rglob("*"):
        if src.is_dir():
            continue
        dest = home / src.relative_to(phantom)
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dest)


def link_quest_home(worktree_path: Path, quest_dir: Path) -> None:
    """Create idempotent id-scoped symlink <worktree>/.imp/quests/<id> → quest_dir."""
    link = worktree_path / ".imp" / "quests" / quest_dir.name
    target = quest_dir.resolve()
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == target:
            return
        link.unlink()
    elif link.is_dir():
        _fold_phantom(link, quest_dir)
        shutil.rmtree(link)
    elif link.exists():
        link.unlink()
    link.symlink_to(target)


def _imp_commit(control_root: Path) -> str:
    """For the refusal breadcrumb; empty string when git is absent or not a repo."""
    proc = worktree._git(control_root, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def live_quest_ids(control_root: Path) -> list[str]:
    """Live quest-home ids: dirs under .imp/quests/ holding a quest.json (archive excluded)."""
    root = control_root / ".imp" / "quests"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".")
                  and (p / "quest.json").exists())


def live_stems(control_root: Path) -> frozenset[str]:
    """Four-letter stems of every live quest home — the mint avoid-set."""
    return frozenset(cid[:4] for cid in live_quest_ids(control_root))


def _migrate_v1_to_v2(quest_dir: Path) -> None:
    """v1->v2: rename base_head->orig_base_head and backfill cur_base_head via
    `git merge-base base_branch HEAD` in the quest's project_root."""
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["orig_base_head"] = meta.pop("base_head", "")
    cur = ""
    project_root = meta.get("project_root", "")
    base_branch = meta.get("base_branch", "")
    if project_root and base_branch:
        proc = worktree._git(Path(project_root), "merge-base", base_branch, "HEAD")
        if proc.returncode == 0:
            cur = proc.stdout.strip()
    meta["cur_base_head"] = cur
    meta["layout_version"] = 2
    _write_quest_json(meta_path, meta)


def _migrate_v2_to_v3(quest_dir: Path) -> None:
    """v2->v3: state.json gained `reincarnations` (auto-defaulted on State.load);
    only the recorded layout_version advances."""
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 3
    _write_quest_json(meta_path, meta)


def _migrate_v3_to_v4(quest_dir: Path) -> None:
    """v3->v4: state.json gained `max_reincarnations` (auto-defaulted on
    State.load); only the recorded layout_version advances."""
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 4
    _write_quest_json(meta_path, meta)


def _migrate_v4_to_v5(quest_dir: Path) -> None:
    """v4->v5: state.json gained `resume_target` (auto-defaulted on State.load);
    only the recorded layout_version advances."""
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 5
    _write_quest_json(meta_path, meta)


def _rewrite_events_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    """Atomically overwrite events.jsonl (tmp + fsync + os.replace) with one
    compact JSON object per line — the on-disk shape JsonlEventLog._persist writes."""
    fd, tmp_name = tempfile.mkstemp(prefix=".events-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            for ev in events:
                f.write(json.dumps(ev))
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _rewrite_state_json(state_path: Path, data: dict[str, Any]) -> None:
    """Atomically rewrite state.json (tmp file, fsync, os.replace); mirrors State.save."""
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", dir=state_path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, state_path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _migrate_v5_to_v6(quest_dir: Path) -> None:
    """v5->v6: rewrite events.jsonl from the pre-triad schema to the triad schema
    (hutu62) via migrate_events, then stamp layout_version. The event rewrite
    precedes the version stamp so a failed rewrite leaves the home at v5 and the
    migration re-runs on the next load."""
    events_path = quest_dir / "events.jsonl"
    if events_path.exists():
        migrated = migrate_events(parse_events_jsonl(events_path.read_bytes()))
        _rewrite_events_jsonl(events_path, migrated)
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 6
    _write_quest_json(meta_path, meta)


def _migrate_v6_to_v7(quest_dir: Path) -> None:
    """v6->v7: backfill state.json's `started_at` (added this release) from the
    quest's events.jsonl — first user_message ts, else first event, else the
    quest's created_at. A home without a state.json (un-materialized seed) is
    skipped for the backfill; it gets started_at stamped live at first run. The
    backfill precedes the version stamp so a failed hop re-runs on next load."""
    from imp.engine.state import derive_started_at
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    state_dir = quest_dir / "state"
    if (state_dir / "state.json").exists():
        events_path = quest_dir / "events.jsonl"
        events = (parse_events_jsonl(events_path.read_bytes())
                  if events_path.exists() else [])
        state = State.load(state_dir)
        if state.started_at is None:
            state.started_at = derive_started_at(events, meta.get("created_at", ""))
            state.save(state_dir)
    meta["layout_version"] = 7
    _write_quest_json(meta_path, meta)


def _backfill_halt_condition(events_path: Path) -> str | None:
    """The last workflow_paused reason -> halt-condition, via the shared
    classifier (interrupt_reason wins -> clean). None when there is no such event."""
    if not events_path.exists():
        return None
    last: dict[str, Any] | None = None
    for ev in parse_events_jsonl(events_path.read_bytes()):
        if ev.get("type") == "workflow_paused":
            last = ev
    if last is None:
        return None
    payload = last.get("payload") or {}
    return classify_pause_halt(payload.get("reason", ""), payload.get("interrupt_reason"))


def _migrate_v7_to_v8(quest_dir: Path) -> None:
    """v7->v8: state.json gained `halt_condition`. Backfill it for a paused live
    home from the last workflow_paused reason so a not-live failure reads `error`
    and a clean stop leaves it None (displayed as paused); a human interrupt reads
    clean regardless of the engine reason. Non-paused homes need no backfill (the
    field auto-defaults None). The state rewrite precedes the version stamp so a
    partial failure re-runs. The archive is deliberately untouched — archived
    display derives from final_state.status, never the halt-condition."""
    state_path = quest_dir / "state" / "state.json"
    if state_path.exists():
        state_data = json.loads(state_path.read_text())
        if (state_data.get("status") == "paused"
                and state_data.get("halt_condition") is None):
            halt = _backfill_halt_condition(quest_dir / "events.jsonl")
            if halt is not None:
                state_data["halt_condition"] = halt
                _rewrite_state_json(state_path, state_data)
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 8
    _write_quest_json(meta_path, meta)


def _migrate_v8_to_v9(quest_dir: Path) -> None:
    """v8->v9: quest.json gained `create_mode` ("new"|"chat"). Backfill an existing
    home to "new" so nothing already in the active list gains shelve-on-leave, then
    stamp the version. Both keys land in one atomic quest.json rewrite (os.replace),
    so a partial failure leaves the home at v8 and the hop re-runs on next load. The
    archive is deliberately untouched — create_mode is never read from an archived
    blob; a resurrected shelved home is backfilled by the load-time migrate() path."""
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["create_mode"] = "new"
    meta["layout_version"] = 9
    _write_quest_json(meta_path, meta)


def _migrate_v9_to_v10(quest_dir: Path) -> None:
    """v9->v10: the persisted Status vocabulary was realigned to the display words.
    Rewrite a live home's state.json: completed->merged; saved->paused (defensive,
    expected count zero); a paused failure carrying halt_condition="error"->error with
    the halt cleared; any residual halt_condition="error"->None (the halt-condition no
    longer carries error). The state rewrite precedes the version stamp so a partial
    failure re-runs. A home without state.json (un-materialized seed) advances only the
    version stamp. The archive migrates separately via _DATA_VERSION 2->3."""
    state_path = quest_dir / "state" / "state.json"
    if state_path.exists():
        data = json.loads(state_path.read_text())
        status = data.get("status")
        if status == "completed":
            data["status"] = "merged"
        elif status == "saved":
            data["status"] = "paused"
        elif status == "paused" and data.get("halt_condition") == "error":
            data["status"] = "error"
            data["halt_condition"] = None
        if data.get("halt_condition") == "error":
            data["halt_condition"] = None
        _rewrite_state_json(state_path, data)
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 10
    _write_quest_json(meta_path, meta)


def _migrate_v10_to_v11(quest_dir: Path) -> None:
    """v10->v11: the resume_target vocabulary split the single `prompt` value into
    `prompt-interrupted`/`prompt-paused` (the persisted steer tell). Rewrite a live
    home's state.json: resume_target == "prompt" -> "prompt-paused" (a pre-split
    leave carries no recoverable tell, so it defaults to the boundary `paused`
    surface); "agent"/absent are unchanged. The state rewrite precedes the version
    stamp so a partial failure re-runs. A home without state.json advances only the
    version stamp. The archive migrates separately via _DATA_VERSION 3->4."""
    state_path = quest_dir / "state" / "state.json"
    if state_path.exists():
        data = json.loads(state_path.read_text())
        if data.get("resume_target") == "prompt":
            data["resume_target"] = "prompt-paused"
        _rewrite_state_json(state_path, data)
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 11
    _write_quest_json(meta_path, meta)


def _migrate_v11_to_v12(quest_dir: Path) -> None:
    """v11->v12: the resume disposition field `resume_mode`
    (Literal["solicit","continue"] | None) was folded to a plain
    `resume_should_continue: bool`. Rewrite a live home's state.json: when the
    old resume_mode key is present, set resume_should_continue =
    (resume_mode == "continue") and drop resume_mode. The key-presence guard
    makes the hop a true no-op on re-run (once resume_mode is gone the bool is
    left untouched) so a partial failure — state rewritten, version stamp not yet
    written — re-runs without flipping an armed continue-resume back to False.
    The state rewrite precedes the version stamp so a partial failure re-runs. A
    home without state.json (un-materialized seed) advances
    only the version stamp. The archive is untouched (_DATA_VERSION unchanged):
    resume_mode is always null in a completed/merged quest and is never read
    from an archived blob."""
    state_path = quest_dir / "state" / "state.json"
    if state_path.exists():
        data = json.loads(state_path.read_text())
        if "resume_mode" in data:
            data["resume_should_continue"] = data.pop("resume_mode") == "continue"
        _rewrite_state_json(state_path, data)
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 12
    _write_quest_json(meta_path, meta)


def _migrate_v12_to_v13(quest_dir: Path) -> None:
    """v12->v13: the bundled default workflow was renamed `imp` -> `default`.
    Rewrite a live home's quest.json so an explicit `"workflow": "imp"` becomes
    `"default"`; a null workflow (the project-default follower) is left untouched
    and simply tracks the renamed directory. Single-file rewrite via the atomic
    _write_quest_json helper; the `== "imp"` guard makes the hop a no-op on re-run."""
    meta_path = quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    if meta.get("workflow") == "imp":
        meta["workflow"] = "default"
    meta["layout_version"] = 13
    _write_quest_json(meta_path, meta)


_MIGRATIONS: dict[tuple[int, int], Callable[[Path], None]] = {
    (1, 2): _migrate_v1_to_v2,
    (2, 3): _migrate_v2_to_v3,
    (3, 4): _migrate_v3_to_v4,
    (4, 5): _migrate_v4_to_v5,
    (5, 6): _migrate_v5_to_v6,
    (6, 7): _migrate_v6_to_v7,
    (7, 8): _migrate_v7_to_v8,
    (8, 9): _migrate_v8_to_v9,
    (9, 10): _migrate_v9_to_v10,
    (10, 11): _migrate_v10_to_v11,
    (11, 12): _migrate_v11_to_v12,
    (12, 13): _migrate_v12_to_v13,
}


def migrate(old_v: int, new_v: int, quest_dir: Path) -> None:
    """Chain step-by-step migrations old_v→old_v+1→…→new_v.
    Raises LayoutMismatch when stored > engine or when any hop has no migration."""
    def _mismatch() -> LayoutMismatch:
        meta = json.loads((quest_dir / "quest.json").read_text())
        return LayoutMismatch(stored=old_v, engine=new_v,
                              imp_commit=meta.get("imp_commit", ""),
                              quest_dir=quest_dir)
    if old_v > new_v:
        raise _mismatch()
    current = old_v
    while current < new_v:
        fn = _MIGRATIONS.get((current, current + 1))
        if fn is None:
            raise _mismatch()
        fn(quest_dir)
        current += 1


@dataclass
class Quest:
    id: str                      # immutable identity (spec §1), minted at imp new
    title: str                   # mutable human name
    base_branch: str
    control_root: Path
    project_root: Path           # the worktree
    quest_dir: Path             # permanent home <control_root>/.imp/quests/<id>
    materialized: bool = True    # False only for an un-materialized draft (no worktree)
    orig_base_head: str = ""     # base tip captured at draft creation; NEVER a diff base
    cur_base_head: str = ""      # the SHA the branch is currently planted on (the fork)
    workflow: str | None = None  # per-quest workflow name; None = project default
    origin: str = "draft"        # provenance: draft | backlog (Backlog only)
    create_mode: str = "new"     # how the conversation was started: new | chat (leave disposition)
    seed_opening: str | None = None  # title+description seed for a fresh resume open
    parent: str | None = None  # parent quest id (create_draft provenance); None when standalone
    depends_on: list[str] = field(default_factory=list)  # dependency quest ids (gates go-live)

    def rename(self, name: str, *, log: JsonlEventLog) -> str:
        """Content-only rename (no git): set title, rewrite quest.json under the
        gitignored .imp/ (never dirties the tracked worktree), emit quest_renamed.
        Returns the id. The id is immutable, so nothing else moves."""
        old_title = self.title
        self.title = name
        meta_path = self.quest_dir / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["title"] = name
        _write_quest_json(meta_path, meta)
        log.append(agent_id=None, type="quest_renamed",
                   payload={"id": self.id, "old_title": old_title,
                            "new_title": name})
        return self.id

    def set_project_root(self, new_root: Path) -> None:
        self.project_root = new_root
        meta_path = self.quest_dir / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["project_root"] = str(new_root)
        _write_quest_json(meta_path, meta)

    def set_origin(self, origin: str) -> None:
        self.origin = origin
        meta_path = self.quest_dir / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["origin"] = origin
        _write_quest_json(meta_path, meta)

    def set_depends_on(self, depends_on: list[str]) -> None:
        self.depends_on = list(depends_on)
        meta_path = self.quest_dir / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["depends_on"] = list(depends_on)
        _write_quest_json(meta_path, meta)

    def set_seed_opening(self, text: str | None) -> None:
        self.seed_opening = text
        meta_path = self.quest_dir / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["seed_opening"] = text
        _write_quest_json(meta_path, meta)

    def set_title(self, name: str) -> None:
        """Metadata-only rename: set title, rewrite quest.json, emit no event."""
        self.title = name
        meta_path = self.quest_dir / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["title"] = name
        _write_quest_json(meta_path, meta)

    def materialize(self) -> bool:
        """Go live: fork branch imp/<id> + worktree from base_branch's CURRENT tip,
        flip project_root to the worktree, and persist project_root + cur_base_head.
        Returns head_changed — whether the base tip moved since draft creation.
        Idempotent: a no-op returning False once already materialized."""
        if self.materialized:
            return False
        if not self.title.strip():
            raise ValueError(
                f"quest {self.id} has no title; call rename_quest to name it "
                "before going live")
        current_tip = worktree.head_of(self.control_root, self.base_branch)
        head_changed = current_tip != self.orig_base_head
        wt_path = worktree.create_worktree(self.control_root, self.id, self.base_branch)
        self.project_root = wt_path
        self.materialized = True
        self.cur_base_head = current_tip
        meta_path = self.quest_dir / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["project_root"] = str(wt_path)
        meta["materialized"] = True
        meta["cur_base_head"] = current_tip
        _write_quest_json(meta_path, meta)
        link_quest_home(wt_path, self.quest_dir)
        return head_changed

    def relink_home(self) -> None:
        if not self.materialized or self.project_root == self.control_root:
            return
        link_quest_home(self.project_root, self.quest_dir)

    @property
    def live_workflow_dir(self) -> Path:
        from imp.assets import resolve_workflow_dir
        from imp.providers.config import parse_default_workflow
        name = self.workflow or parse_default_workflow(
            self.project_root / "imp" / "project.yaml")
        return resolve_workflow_dir(self.project_root, name)

    @property
    def control_workflow_dir(self) -> Path:
        """Mirror of live_workflow_dir rooted at the control root, not the worktree."""
        from imp.assets import resolve_workflow_dir
        from imp.providers.config import parse_default_workflow
        name = self.workflow or parse_default_workflow(
            self.control_root / "imp" / "project.yaml")
        return resolve_workflow_dir(self.control_root, name)

    @property
    def workflow_dir(self) -> Path:
        snapshot = self.quest_dir / "workflow"
        return snapshot if snapshot.is_dir() else self.live_workflow_dir

    @property
    def state_dir(self) -> Path:
        return self.quest_dir / "state"

    @property
    def events_path(self) -> Path:
        return self.quest_dir / "events.jsonl"

    @property
    def tmp_dir(self) -> Path:
        """Per-quest scratch root /tmp/imp-<uid>/<id>/, reaped at quest end."""
        return Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / self.id

    @property
    def var_tmp_dir(self) -> Path:
        """Per-quest overlay scratch root <var-tmp-root>/imp-<uid>/<id>/, reaped at quest end."""
        return quest_var_tmp_root() / f"imp-{os.getuid()}" / self.id

    def event_log(self) -> JsonlEventLog:
        return JsonlEventLog(self.events_path)

    @classmethod
    def create(
        cls, control_root: Path, *, title: str, quest_id: str, base_branch: str,
        depends_on: list[str] | None = None, workflow: str | None = None,
    ) -> Quest:
        """Create branch imp/<id> + worktree .imp/worktrees/<id>, then seed
        internal state at the control root (.imp/quests/<id>). Raises
        worktree.BranchExists on the (effectively impossible) id collision —
        the caller reports a clean error."""
        wt_path = worktree.create_worktree(control_root, quest_id, base_branch)
        cur_base_head = worktree.head_of(control_root, base_branch)
        quest_dir = control_root / ".imp" / "quests" / quest_id
        quest_dir.mkdir(parents=True)
        (quest_dir / "state").mkdir()
        (quest_dir / "steps").mkdir()

        (quest_dir / "quest.json").write_text(json.dumps({
            "id": quest_id, "title": title, "base_branch": base_branch,
            "created_at": datetime.now(UTC).isoformat(),
            "create_mode": "new",
            "depends_on": list(depends_on or []),
            "project_root": str(wt_path),
            "materialized": True,
            "cur_base_head": cur_base_head,
            "layout_version": LAYOUT_VERSION,
            "imp_commit": _imp_commit(control_root),
            "workflow": workflow,
        }, indent=2, sort_keys=True))

        from imp.engine.loader import load_workflow
        from imp.providers.effective import (
            load_effective_config,
            validate_project_memory,
        )

        config = load_effective_config(
            project_root=wt_path, control_root=control_root,
        ).config
        try:
            validate_project_memory(wt_path, config.memory, config.project_facts)
            from imp.assets import resolve_workflow_dir
            from imp.providers.config import parse_default_workflow
            wf_name = workflow or parse_default_workflow(
                wt_path / "imp" / "project.yaml")
            loaded_workflow = load_workflow(
                resolve_workflow_dir(wt_path, wf_name),
                configured_tiers=config.tier_names(),
                project_keys=frozenset(config.project_facts),
            )
        except (LoadError, LoadErrors):
            # State is not yet written; tear down the worktree so no orphan is left.
            worktree.destroy_worktree(control_root, quest_id)
            shutil.rmtree(quest_dir, ignore_errors=True)
            raise
        state = State(id=quest_id, step=None, step_attempt=0, status="new")
        seed_workflow_start(state, loaded_workflow, config)
        state.save(quest_dir / "state")

        link_quest_home(wt_path, quest_dir)
        quest = cls(
            id=quest_id, title=title, base_branch=base_branch,
            control_root=control_root, project_root=wt_path, quest_dir=quest_dir,
            cur_base_head=cur_base_head, workflow=workflow,
            depends_on=list(depends_on or []),
        )
        quest.event_log().append(
            agent_id=None, type="quest_created",
            payload={"id": quest_id, "title": title},
        )
        return quest

    @classmethod
    def create_draft(
        cls, control_root: Path, *, title: str, quest_id: str, base_branch: str,
        depends_on: list[str] | None = None, workflow: str | None = None,
        origin: str = "draft", seed_opening: str | None = None,
        parent: str | None = None, defer_seed: bool = False,
        create_mode: str = "new",
    ) -> Quest:
        """An un-materialized draft: NO branch, NO worktree. State is seeded at the
        permanent home; project_root points at the main checkout so the spec
        brainstorm runs read-only there. The base branch's current tip is recorded
        as orig_base_head for the later go-live head_changed check. Validates the
        workflow against the main checkout; with no branch/worktree to roll back,
        a bad/absent workflow drops the just-created home and re-raises."""
        quest_dir = control_root / ".imp" / "quests" / quest_id
        quest_dir.mkdir(parents=True)
        (quest_dir / "state").mkdir()
        (quest_dir / "steps").mkdir()
        try:
            orig_base_head = worktree.head_of(control_root, base_branch)
        except RuntimeError:
            shutil.rmtree(quest_dir, ignore_errors=True)
            raise

        (quest_dir / "quest.json").write_text(json.dumps({
            "id": quest_id, "title": title, "base_branch": base_branch,
            "created_at": datetime.now(UTC).isoformat(),
            "create_mode": create_mode,
            "depends_on": list(depends_on or []),
            "project_root": str(control_root),
            "materialized": False,
            "orig_base_head": orig_base_head,
            "layout_version": LAYOUT_VERSION,
            "imp_commit": _imp_commit(control_root),
            "workflow": workflow,
            "origin": origin,
            "seed_opening": seed_opening,
            "parent": parent,
        }, indent=2, sort_keys=True))

        if defer_seed:
            # Skip load/validate/seed so workflow skew can't veto stub creation;
            # the cursor is seeded at first resume instead.
            State(
                id=quest_id, step=None, step_attempt=0, status="new",
            ).save(quest_dir / "state")
        else:
            from imp.engine.loader import load_workflow
            from imp.providers.config import parse_default_workflow
            from imp.providers.effective import (
                load_effective_config,
                validate_project_memory,
            )

            config = load_effective_config(
                project_root=control_root, control_root=control_root,
            ).config
            try:
                validate_project_memory(
                    control_root, config.memory, config.project_facts)
                from imp.assets import resolve_workflow_dir
                wf_name = workflow or parse_default_workflow(
                    control_root / "imp" / "project.yaml")
                loaded_workflow = load_workflow(
                    resolve_workflow_dir(control_root, wf_name),
                    configured_tiers=config.tier_names(),
                    project_keys=frozenset(config.project_facts),
                )
            except (LoadError, LoadErrors):
                shutil.rmtree(quest_dir, ignore_errors=True)
                raise
            state = State(id=quest_id, step=None, step_attempt=0, status="new")
            seed_workflow_start(state, loaded_workflow, config)
            state.save(quest_dir / "state")

        quest = cls(
            id=quest_id, title=title, base_branch=base_branch,
            control_root=control_root, project_root=control_root,
            quest_dir=quest_dir, materialized=False, orig_base_head=orig_base_head,
            workflow=workflow, origin=origin, create_mode=create_mode,
            seed_opening=seed_opening, parent=parent,
            depends_on=list(depends_on or []),
        )
        quest.event_log().append(
            agent_id=None, type="quest_created",
            payload={"id": quest_id, "title": title},
        )
        return quest

    @classmethod
    def load(cls, control_root: Path, quest_id: str, *,
             migrate_if_lockable: bool = False) -> Quest:
        quest_dir = control_root / ".imp" / "quests" / quest_id
        if not (quest_dir / "quest.json").is_file():
            raise LoadError(f"no such quest {quest_id!r}", SourceLoc(file=quest_dir, line=1))
        meta = json.loads((quest_dir / "quest.json").read_text())
        stored = meta.get("layout_version", 0)
        if stored != LAYOUT_VERSION:
            if migrate_if_lockable:
                try:
                    with ProjectLock(quest_dir / "lock").acquire(timeout=0.0):
                        migrate(stored, LAYOUT_VERSION, quest_dir)
                except LockHeld:
                    raise MigrationLockHeld(quest_dir=quest_dir) from None
            else:
                migrate(stored, LAYOUT_VERSION, quest_dir)
            meta = json.loads((quest_dir / "quest.json").read_text())
        project_root = (
            Path(meta["project_root"]) if meta.get("project_root")
            else (control_root / ".imp" / "worktrees" / quest_id).resolve()
        )
        return cls(
            id=meta["id"], title=meta["title"], base_branch=meta["base_branch"],
            control_root=control_root, project_root=project_root,
            quest_dir=quest_dir,
            materialized=meta.get("materialized", True),
            orig_base_head=meta.get("orig_base_head", ""),
            cur_base_head=meta.get("cur_base_head", ""),
            workflow=meta.get("workflow"),
            origin=meta.get("origin", "draft"),
            create_mode=meta.get("create_mode", "new"),
            seed_opening=meta.get("seed_opening"),
            parent=meta.get("parent"),
            depends_on=list(meta.get("depends_on") or []),
        )
