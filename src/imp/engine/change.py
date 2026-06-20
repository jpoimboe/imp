"""Change lifecycle (spec §3, §5): immutable id identity, derived handle, worktree
creation, permanent state home at <control_root>/.imp/changes/<id>/."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from imp.engine import worktree
from imp.engine.errors import LayoutMismatch, LoadError, LoadErrors
from imp.engine.state import JsonlEventLog, State
from imp.engine.types import SourceLoc

LAYOUT_VERSION = 1  # bump in the SAME commit as any breaking change-home layout/schema change


def _imp_commit(control_root: Path) -> str:
    """For the refusal breadcrumb; empty string when git is absent or not a repo."""
    proc = worktree._git(control_root, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


_MIGRATIONS: dict[tuple[int, int], Callable[[Path], None]] = {}


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
    base_head: str = ""          # base branch tip recorded at draft creation (drafts only)

    @property
    def handle(self) -> str:
        """Derived display name + filename stem (never a path or ref):
        `slugify(title)-<id>` once named, the bare `<id>` while unnamed."""
        from imp.engine.slug import derive_handle
        return derive_handle(self.title, self.id)

    def rename(self, name: str, *, log: JsonlEventLog) -> str:
        """Content-only rename (no git): set title, rewrite change.json under the
        gitignored .imp/ (never dirties the tracked worktree), emit change_renamed.
        Returns the new handle. The id is immutable, so nothing else moves."""
        old_title = self.title
        self.title = name                        # handle property now derives anew
        meta_path = self.change_dir / "change.json"
        meta = json.loads(meta_path.read_text())
        meta["title"] = name
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
        log.append(agent_id=None, type="change_renamed",
                   payload={"id": self.id, "old_title": old_title,
                            "new_title": name, "handle": self.handle})
        return self.handle

    def set_project_root(self, new_root: Path) -> None:
        self.project_root = new_root
        meta_path = self.change_dir / "change.json"
        meta = json.loads(meta_path.read_text())
        meta["project_root"] = str(new_root)
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

    def materialize(self) -> bool:
        """Go live: fork branch imp/<id> + worktree from base_branch's CURRENT tip,
        flip project_root to the worktree, and persist both to change.json.
        Returns head_changed — whether the base tip moved since draft creation.
        Idempotent: a no-op returning False once already materialized."""
        if self.materialized:
            return False
        current_tip = worktree.head_of(self.control_root, self.base_branch)
        head_changed = current_tip != self.base_head
        wt_path = worktree.create_worktree(self.control_root, self.id, self.base_branch)
        self.project_root = wt_path
        self.materialized = True
        meta_path = self.change_dir / "change.json"
        meta = json.loads(meta_path.read_text())
        meta["project_root"] = str(wt_path)
        meta["materialized"] = True
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
        return head_changed

    @property
    def live_workflow_dir(self) -> Path:
        from imp.providers.config import parse_default_workflow
        name = parse_default_workflow(self.project_root / "imp" / "project.yaml")
        return self.project_root / "imp" / "workflows" / name

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
        depends_on: list[str] | None = None,
    ) -> Change:
        """Create branch imp/<id> + worktree .imp/worktrees/<id>, then seed
        internal state at the control root (.imp/changes/<id>). Raises
        worktree.BranchExists on the (effectively impossible) id collision —
        the caller reports a clean error."""
        wt_path = worktree.create_worktree(control_root, change_id, base_branch)
        change_dir = control_root / ".imp" / "changes" / change_id
        change_dir.mkdir(parents=True)
        (change_dir / "state").mkdir()

        (change_dir / "change.json").write_text(json.dumps({
            "id": change_id, "title": title, "base_branch": base_branch,
            "created_at": datetime.now(UTC).isoformat(),
            "depends_on": list(depends_on or []),
            "project_root": str(wt_path),
            "materialized": True,
            "layout_version": LAYOUT_VERSION,
            "imp_commit": _imp_commit(control_root),
        }, indent=2, sort_keys=True))

        from imp.engine.loader import load_workflow
        from imp.providers.config import apply_var_overrides
        from imp.providers.effective import load_effective_config

        config = load_effective_config(
            project_root=wt_path, control_root=control_root,
        ).config
        # The branch's own tracked workflow IS the source — read it directly from
        # the worktree, no snapshot copy. This seed load doubles as the single
        # validator: raises LoadError on an absent/malformed workflow, or
        # LoadErrors when the workflow parses but fails validation (e.g. a step
        # references a model_tier not present in the merged config).
        try:
            from imp.providers.config import parse_default_workflow
            wf_name = parse_default_workflow(wt_path / "imp" / "project.yaml")
            workflow = load_workflow(
                wt_path / "imp" / "workflows" / wf_name,
                configured_tiers=config.tier_names(),
                project_keys=frozenset(config.project_facts),
            )
        except (LoadError, LoadErrors):
            # Validation now runs post-create_worktree, so a base branch lacking a
            # readable imp/workflows/<name> (or carrying a malformed
            # default-workflow: value) would otherwise leak the just-created
            # branch+worktree. Roll both back so a failed create leaves nothing.
            worktree.destroy_worktree(control_root, change_id)
            shutil.rmtree(change_dir, ignore_errors=True)
            raise
        flat = workflow.flat_steps()
        State(
            id=change_id, step=flat[0].name if flat else None, step_attempt=0,
            status="running",
            vars=apply_var_overrides(workflow.vars, config.var_overrides),
            loop_counts={},
        ).save(change_dir / "state")

        change = cls(
            id=change_id, title=title, base_branch=base_branch,
            control_root=control_root, project_root=wt_path, change_dir=change_dir,
        )
        change.event_log().append(
            agent_id=None, type="change_created",
            payload={"id": change_id, "handle": change.handle, "title": title},
        )
        return change

    @classmethod
    def create_draft(
        cls, control_root: Path, *, title: str, change_id: str, base_branch: str,
        depends_on: list[str] | None = None,
    ) -> Change:
        """An un-materialized draft: NO branch, NO worktree. State is seeded at the
        permanent home; project_root points at the main checkout so the spec
        brainstorm runs read-only there. The base branch's current tip is recorded
        as base_head for the later go-live head_changed check. Validates the
        workflow against the main checkout; with no branch/worktree to roll back,
        a bad/absent workflow drops the just-created home and re-raises."""
        change_dir = control_root / ".imp" / "changes" / change_id
        change_dir.mkdir(parents=True)
        (change_dir / "state").mkdir()
        try:
            base_head = worktree.head_of(control_root, base_branch)
        except RuntimeError:
            shutil.rmtree(change_dir, ignore_errors=True)
            raise

        (change_dir / "change.json").write_text(json.dumps({
            "id": change_id, "title": title, "base_branch": base_branch,
            "created_at": datetime.now(UTC).isoformat(),
            "depends_on": list(depends_on or []),
            "project_root": str(control_root),
            "materialized": False,
            "base_head": base_head,
            "layout_version": LAYOUT_VERSION,
            "imp_commit": _imp_commit(control_root),
        }, indent=2, sort_keys=True))

        from imp.engine.loader import load_workflow
        from imp.providers.config import apply_var_overrides, parse_default_workflow
        from imp.providers.effective import load_effective_config

        config = load_effective_config(
            project_root=control_root, control_root=control_root,
        ).config
        try:
            wf_name = parse_default_workflow(control_root / "imp" / "project.yaml")
            workflow = load_workflow(
                control_root / "imp" / "workflows" / wf_name,
                configured_tiers=config.tier_names(),
                project_keys=frozenset(config.project_facts),
            )
        except (LoadError, LoadErrors):
            shutil.rmtree(change_dir, ignore_errors=True)
            raise
        flat = workflow.flat_steps()
        State(
            id=change_id, step=flat[0].name if flat else None, step_attempt=0,
            status="running",
            vars=apply_var_overrides(workflow.vars, config.var_overrides),
            loop_counts={},
        ).save(change_dir / "state")

        change = cls(
            id=change_id, title=title, base_branch=base_branch,
            control_root=control_root, project_root=control_root,
            change_dir=change_dir, materialized=False, base_head=base_head,
        )
        change.event_log().append(
            agent_id=None, type="change_created",
            payload={"id": change_id, "handle": change.handle, "title": title},
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
            # if migrate() succeeds, meta is now stale; re-read change.json before use
        project_root = (
            Path(meta["project_root"]) if meta.get("project_root")
            else (control_root / ".imp" / "worktrees" / change_id).resolve()
        )
        return cls(
            id=meta["id"], title=meta["title"], base_branch=meta["base_branch"],
            control_root=control_root, project_root=project_root,
            change_dir=change_dir,
            materialized=meta.get("materialized", True),
            base_head=meta.get("base_head", ""),
        )
