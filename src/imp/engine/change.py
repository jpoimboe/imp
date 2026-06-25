"""Change lifecycle (spec §3, §5): immutable id identity, free-form title, worktree
creation, permanent state home at <control_root>/.imp/changes/<id>/."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from imp.agents.prompts import render_label
from imp.engine import worktree
from imp.engine.errors import LayoutMismatch, LoadError, LoadErrors
from imp.engine.state import JsonlEventLog, State
from imp.engine.types import SourceLoc

LAYOUT_VERSION = 2  # bump in the SAME commit as any breaking change-home layout/schema change


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


def link_change_home(worktree_path: Path, change_dir: Path) -> None:
    """Create idempotent id-scoped symlink <worktree>/.imp/changes/<id> → change_dir."""
    link = worktree_path / ".imp" / "changes" / change_dir.name
    target = change_dir.resolve()
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == target:
            return
        link.unlink()
    elif link.is_dir():
        _fold_phantom(link, change_dir)
        shutil.rmtree(link)
    elif link.exists():
        link.unlink()
    link.symlink_to(target)


def _imp_commit(control_root: Path) -> str:
    """For the refusal breadcrumb; empty string when git is absent or not a repo."""
    proc = worktree._git(control_root, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _migrate_v1_to_v2(change_dir: Path) -> None:
    """v1->v2: rename base_head->orig_base_head and backfill cur_base_head via
    `git merge-base base_branch HEAD` in the change's project_root."""
    meta_path = change_dir / "change.json"
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
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))


_MIGRATIONS: dict[tuple[int, int], Callable[[Path], None]] = {(1, 2): _migrate_v1_to_v2}


def migrate(old_v: int, new_v: int, change_dir: Path) -> None:
    """Raises LayoutMismatch when no migration path (old_v, new_v) is registered."""
    fn = _MIGRATIONS.get((old_v, new_v))
    if fn is None:
        meta = json.loads((change_dir / "change.json").read_text())
        raise LayoutMismatch(stored=old_v, engine=new_v,
                             imp_commit=meta.get("imp_commit", ""),
                             change_dir=change_dir)
    fn(change_dir)


@dataclass
class Change:
    id: str                      # immutable identity (spec §1), minted at imp new
    title: str                   # mutable human name
    base_branch: str
    control_root: Path
    project_root: Path           # the worktree
    change_dir: Path             # permanent home <control_root>/.imp/changes/<id>
    materialized: bool = True    # False only for an un-materialized draft (no worktree)
    orig_base_head: str = ""     # base tip captured at draft creation; NEVER a diff base
    cur_base_head: str = ""      # the SHA the branch is currently planted on (the fork)
    workflow: str | None = None  # per-Change workflow name; None = project default
    origin: str = "draft"        # provenance: idea | draft | followup (Backlog only)
    seed_opening: str | None = None  # title+description seed for a fresh resume open

    def rename(self, name: str, *, log: JsonlEventLog) -> str:
        """Content-only rename (no git): set title, rewrite change.json under the
        gitignored .imp/ (never dirties the tracked worktree), emit change_renamed.
        Returns the id. The id is immutable, so nothing else moves."""
        old_title = self.title
        self.title = name
        meta_path = self.change_dir / "change.json"
        meta = json.loads(meta_path.read_text())
        meta["title"] = name
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
        log.append(agent_id=None, type="change_renamed",
                   payload={"id": self.id, "old_title": old_title,
                            "new_title": name})
        return self.id

    def set_project_root(self, new_root: Path) -> None:
        self.project_root = new_root
        meta_path = self.change_dir / "change.json"
        meta = json.loads(meta_path.read_text())
        meta["project_root"] = str(new_root)
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

    def set_origin(self, origin: str) -> None:
        self.origin = origin
        meta_path = self.change_dir / "change.json"
        meta = json.loads(meta_path.read_text())
        meta["origin"] = origin
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

    def set_depends_on(self, depends_on: list[str]) -> None:
        meta_path = self.change_dir / "change.json"
        meta = json.loads(meta_path.read_text())
        meta["depends_on"] = list(depends_on)
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

    def materialize(self) -> bool:
        """Go live: fork branch imp/<id> + worktree from base_branch's CURRENT tip,
        flip project_root to the worktree, and persist project_root + cur_base_head.
        Returns head_changed — whether the base tip moved since draft creation.
        Idempotent: a no-op returning False once already materialized."""
        if self.materialized:
            return False
        current_tip = worktree.head_of(self.control_root, self.base_branch)
        head_changed = current_tip != self.orig_base_head
        wt_path = worktree.create_worktree(self.control_root, self.id, self.base_branch)
        self.project_root = wt_path
        self.materialized = True
        self.cur_base_head = current_tip
        meta_path = self.change_dir / "change.json"
        meta = json.loads(meta_path.read_text())
        meta["project_root"] = str(wt_path)
        meta["materialized"] = True
        meta["cur_base_head"] = current_tip
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
        link_change_home(wt_path, self.change_dir)
        return head_changed

    def relink_home(self) -> None:
        if not self.materialized or self.project_root == self.control_root:
            return
        link_change_home(self.project_root, self.change_dir)

    @property
    def live_workflow_dir(self) -> Path:
        from imp.assets import resolve_workflow_dir
        from imp.providers.config import parse_default_workflow
        name = self.workflow or parse_default_workflow(
            self.project_root / "imp" / "project.yaml")
        return resolve_workflow_dir(self.project_root, name)

    @property
    def workflow_dir(self) -> Path:
        snapshot = self.change_dir / "workflow"
        return snapshot if snapshot.is_dir() else self.live_workflow_dir

    @property
    def state_dir(self) -> Path:
        return self.change_dir / "state"

    @property
    def events_path(self) -> Path:
        return self.change_dir / "events.jsonl"

    def event_log(self) -> JsonlEventLog:
        return JsonlEventLog(self.events_path)

    @classmethod
    def create(
        cls, control_root: Path, *, title: str, change_id: str, base_branch: str,
        depends_on: list[str] | None = None, workflow: str | None = None,
    ) -> Change:
        """Create branch imp/<id> + worktree .imp/worktrees/<id>, then seed
        internal state at the control root (.imp/changes/<id>). Raises
        worktree.BranchExists on the (effectively impossible) id collision —
        the caller reports a clean error."""
        wt_path = worktree.create_worktree(control_root, change_id, base_branch)
        cur_base_head = worktree.head_of(control_root, base_branch)
        change_dir = control_root / ".imp" / "changes" / change_id
        change_dir.mkdir(parents=True)
        (change_dir / "state").mkdir()

        (change_dir / "change.json").write_text(json.dumps({
            "id": change_id, "title": title, "base_branch": base_branch,
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
            worktree.destroy_worktree(control_root, change_id)
            shutil.rmtree(change_dir, ignore_errors=True)
            raise
        flat = loaded_workflow.flat_steps()
        State(
            id=change_id, step=flat[0].name if flat else None, step_attempt=0,
            step_label=(
                render_label(flat[0].label, change=None, log=None, state=None)
                or flat[0].name
            ) if flat else None,
            status="new",
            vars=apply_var_overrides(loaded_workflow.vars, config.var_overrides),
            loop_counts={},
        ).save(change_dir / "state")

        link_change_home(wt_path, change_dir)
        change = cls(
            id=change_id, title=title, base_branch=base_branch,
            control_root=control_root, project_root=wt_path, change_dir=change_dir,
            cur_base_head=cur_base_head, workflow=workflow,
        )
        change.event_log().append(
            agent_id=None, type="change_created",
            payload={"id": change_id, "title": title},
        )
        return change

    @classmethod
    def create_draft(
        cls, control_root: Path, *, title: str, change_id: str, base_branch: str,
        depends_on: list[str] | None = None, workflow: str | None = None,
        origin: str = "draft", seed_opening: str | None = None,
    ) -> Change:
        """An un-materialized draft: NO branch, NO worktree. State is seeded at the
        permanent home; project_root points at the main checkout so the spec
        brainstorm runs read-only there. The base branch's current tip is recorded
        as orig_base_head for the later go-live head_changed check. Validates the
        workflow against the main checkout; with no branch/worktree to roll back,
        a bad/absent workflow drops the just-created home and re-raises."""
        change_dir = control_root / ".imp" / "changes" / change_id
        change_dir.mkdir(parents=True)
        (change_dir / "state").mkdir()
        try:
            orig_base_head = worktree.head_of(control_root, base_branch)
        except RuntimeError:
            shutil.rmtree(change_dir, ignore_errors=True)
            raise

        (change_dir / "change.json").write_text(json.dumps({
            "id": change_id, "title": title, "base_branch": base_branch,
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
            shutil.rmtree(change_dir, ignore_errors=True)
            raise
        flat = loaded_workflow.flat_steps()
        State(
            id=change_id, step=flat[0].name if flat else None, step_attempt=0,
            step_label=(
                render_label(flat[0].label, change=None, log=None, state=None)
                or flat[0].name
            ) if flat else None,
            status="new",
            vars=apply_var_overrides(loaded_workflow.vars, config.var_overrides),
            loop_counts={},
        ).save(change_dir / "state")

        change = cls(
            id=change_id, title=title, base_branch=base_branch,
            control_root=control_root, project_root=control_root,
            change_dir=change_dir, materialized=False, orig_base_head=orig_base_head,
            workflow=workflow, origin=origin, seed_opening=seed_opening,
        )
        change.event_log().append(
            agent_id=None, type="change_created",
            payload={"id": change_id, "title": title},
        )
        return change

    @classmethod
    def load(cls, control_root: Path, change_id: str) -> Change:
        change_dir = control_root / ".imp" / "changes" / change_id
        if not change_dir.is_dir():
            raise LoadError(f"no such Change {change_id!r}", SourceLoc(file=change_dir, line=1))
        meta = json.loads((change_dir / "change.json").read_text())
        stored = meta.get("layout_version", 0)
        if stored != LAYOUT_VERSION:
            migrate(stored, LAYOUT_VERSION, change_dir)
            meta = json.loads((change_dir / "change.json").read_text())
        project_root = (
            Path(meta["project_root"]) if meta.get("project_root")
            else (control_root / ".imp" / "worktrees" / change_id).resolve()
        )
        return cls(
            id=meta["id"], title=meta["title"], base_branch=meta["base_branch"],
            control_root=control_root, project_root=project_root,
            change_dir=change_dir,
            materialized=meta.get("materialized", True),
            orig_base_head=meta.get("orig_base_head", ""),
            cur_base_head=meta.get("cur_base_head", ""),
            workflow=meta.get("workflow"),
            origin=meta.get("origin", "draft"),
            seed_opening=meta.get("seed_opening"),
        )
