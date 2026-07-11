"""Quest lifecycle (spec §3, §5): immutable id identity, free-form title, worktree
creation, permanent state home at <control_root>/.imp/quests/<id>/."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from imp.agents.prompts import render_label
from imp.engine import worktree
from imp.engine.errors import LayoutMismatch, LoadError, LoadErrors
from imp.engine.state import JsonlEventLog, State
from imp.engine.types import SourceLoc

LAYOUT_VERSION = 5  # bump in the SAME commit as any breaking change-home layout/schema change


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
    """Live change-home ids: dirs under .imp/quests/ holding a quest.json (archive excluded)."""
    root = control_root / ".imp" / "quests"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".")
                  and (p / "quest.json").exists())


def live_stems(control_root: Path) -> frozenset[str]:
    """Four-letter stems of every live change home — the mint avoid-set."""
    return frozenset(cid[:4] for cid in live_quest_ids(control_root))


def _migrate_v1_to_v2(quest_dir: Path) -> None:
    """v1->v2: rename base_head->orig_base_head and backfill cur_base_head via
    `git merge-base base_branch HEAD` in the change's project_root."""
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


_MIGRATIONS: dict[tuple[int, int], Callable[[Path], None]] = {
    (1, 2): _migrate_v1_to_v2,
    (2, 3): _migrate_v2_to_v3,
    (3, 4): _migrate_v3_to_v4,
    (4, 5): _migrate_v4_to_v5,
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
    workflow: str | None = None  # per-Quest workflow name; None = project default
    origin: str = "draft"        # provenance: idea | draft | followup (Backlog only)
    seed_opening: str | None = None  # title+description seed for a fresh resume open

    def rename(self, name: str, *, log: JsonlEventLog) -> str:
        """Content-only rename (no git): set title, rewrite quest.json under the
        gitignored .imp/ (never dirties the tracked worktree), emit change_renamed.
        Returns the id. The id is immutable, so nothing else moves."""
        old_title = self.title
        self.title = name
        meta_path = self.quest_dir / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["title"] = name
        _write_quest_json(meta_path, meta)
        log.append(agent_id=None, type="change_renamed",
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

    def materialize(self) -> bool:
        """Go live: fork branch imp/<id> + worktree from base_branch's CURRENT tip,
        flip project_root to the worktree, and persist project_root + cur_base_head.
        Returns head_changed — whether the base tip moved since draft creation.
        Idempotent: a no-op returning False once already materialized."""
        if self.materialized:
            return False
        if not self.title.strip():
            raise ValueError(
                f"Quest {self.id} has no title; call rename_quest to name it "
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

        (quest_dir / "quest.json").write_text(json.dumps({
            "id": quest_id, "title": title, "base_branch": base_branch,
            "created_at": datetime.now(UTC).isoformat(),
            "depends_on": list(depends_on or []),
            "project_root": str(wt_path),
            "materialized": True,
            "cur_base_head": cur_base_head,
            "layout_version": LAYOUT_VERSION,
            "imp_commit": _imp_commit(control_root),
            "workflow": workflow,
        }, indent=2, sort_keys=True))

        from imp.engine.loader import load_workflow
        from imp.providers.config import apply_var_overrides
        from imp.providers.effective import load_effective_config

        config = load_effective_config(
            project_root=wt_path, control_root=control_root,
        ).config
        try:
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
        flat = loaded_workflow.flat_steps()
        State(
            id=quest_id, step=flat[0].name if flat else None, step_attempt=0,
            step_label=(
                render_label(flat[0].label, change=None, log=None, state=None)
                or flat[0].name
            ) if flat else None,
            phase_label=(
                render_label(flat[0].phase_label, change=None, log=None, state=None)
                if flat[0].phase_label is not None else None
            ) if flat else None,
            status="new",
            vars=apply_var_overrides(loaded_workflow.vars, config.var_overrides),
            loop_counts={},
        ).save(quest_dir / "state")

        link_quest_home(wt_path, quest_dir)
        change = cls(
            id=quest_id, title=title, base_branch=base_branch,
            control_root=control_root, project_root=wt_path, quest_dir=quest_dir,
            cur_base_head=cur_base_head, workflow=workflow,
        )
        change.event_log().append(
            agent_id=None, type="change_created",
            payload={"id": quest_id, "title": title},
        )
        return change

    @classmethod
    def create_draft(
        cls, control_root: Path, *, title: str, quest_id: str, base_branch: str,
        depends_on: list[str] | None = None, workflow: str | None = None,
        origin: str = "draft", seed_opening: str | None = None,
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
        try:
            orig_base_head = worktree.head_of(control_root, base_branch)
        except RuntimeError:
            shutil.rmtree(quest_dir, ignore_errors=True)
            raise

        (quest_dir / "quest.json").write_text(json.dumps({
            "id": quest_id, "title": title, "base_branch": base_branch,
            "created_at": datetime.now(UTC).isoformat(),
            "depends_on": list(depends_on or []),
            "project_root": str(control_root),
            "materialized": False,
            "orig_base_head": orig_base_head,
            "layout_version": LAYOUT_VERSION,
            "imp_commit": _imp_commit(control_root),
            "workflow": workflow,
            "origin": origin,
            "seed_opening": seed_opening,
        }, indent=2, sort_keys=True))

        from imp.engine.loader import load_workflow
        from imp.providers.config import apply_var_overrides, parse_default_workflow
        from imp.providers.effective import load_effective_config

        config = load_effective_config(
            project_root=control_root, control_root=control_root,
        ).config
        try:
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
        flat = loaded_workflow.flat_steps()
        State(
            id=quest_id, step=flat[0].name if flat else None, step_attempt=0,
            step_label=(
                render_label(flat[0].label, change=None, log=None, state=None)
                or flat[0].name
            ) if flat else None,
            phase_label=(
                render_label(flat[0].phase_label, change=None, log=None, state=None)
                if flat[0].phase_label is not None else None
            ) if flat else None,
            status="new",
            vars=apply_var_overrides(loaded_workflow.vars, config.var_overrides),
            loop_counts={},
        ).save(quest_dir / "state")

        change = cls(
            id=quest_id, title=title, base_branch=base_branch,
            control_root=control_root, project_root=control_root,
            quest_dir=quest_dir, materialized=False, orig_base_head=orig_base_head,
            workflow=workflow, origin=origin, seed_opening=seed_opening,
        )
        change.event_log().append(
            agent_id=None, type="change_created",
            payload={"id": quest_id, "title": title},
        )
        return change

    @classmethod
    def load(cls, control_root: Path, quest_id: str) -> Quest:
        quest_dir = control_root / ".imp" / "quests" / quest_id
        if not quest_dir.is_dir():
            raise LoadError(f"no such Quest {quest_id!r}", SourceLoc(file=quest_dir, line=1))
        meta = json.loads((quest_dir / "quest.json").read_text())
        stored = meta.get("layout_version", 0)
        if stored != LAYOUT_VERSION:
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
            seed_opening=meta.get("seed_opening"),
        )
