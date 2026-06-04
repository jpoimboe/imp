"""Change lifecycle (spec §3, §5): immutable id identity, derived slug, worktree
creation, state home inside the worktree at <worktree>/.imp/change/."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from imp.engine import worktree
from imp.engine.errors import LoadError
from imp.engine.state import JsonlEventLog, State
from imp.engine.types import SourceLoc


@dataclass
class Change:
    id: str                      # immutable identity (spec §1), minted at imp new
    title: str                   # mutable human name
    base_branch: str
    control_root: Path
    project_root: Path           # the worktree
    change_dir: Path             # <worktree>/.imp/change

    @property
    def slug(self) -> str:
        """Derived display name (spec filename + human output ONLY — never a path
        or ref). `id[:10]` is the fixed-width YYYY-MM-DD prefix of the id."""
        from imp.engine.slug import dated_slug
        return dated_slug(self.title, date=self.id[:10])

    @property
    def workflow_dir(self) -> Path:
        return self.change_dir / "workflow"

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
    ) -> Change:
        """Create branch imp/<id> + worktree .imp/worktrees/<id>, then seed
        internal state inside the worktree. Raises worktree.BranchExists on the
        (effectively impossible) id collision — the caller reports a clean error."""
        wt_path = worktree.create_worktree(control_root, change_id, base_branch)
        change_dir = wt_path / ".imp" / "change"
        change_dir.mkdir(parents=True)

        src_wf = control_root / "imp" / "workflows" / "default"
        if not src_wf.is_dir():
            raise LoadError(f"no default workflow at {src_wf}", SourceLoc(file=src_wf, line=1))
        shutil.copytree(src_wf, change_dir / "workflow")
        (change_dir / "state").mkdir()

        (change_dir / "change.json").write_text(json.dumps({
            "id": change_id, "title": title, "base_branch": base_branch,
            "created_at": datetime.now(UTC).isoformat(),
        }, indent=2, sort_keys=True))

        from imp.engine.loader import load_workflow
        from imp.providers.config import apply_var_overrides, load_config

        config = load_config(control_root)
        workflow = load_workflow(
            change_dir / "workflow", configured_roles=config.role_names()
        )
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
            # Carries BOTH id (new canonical key) and the derived slug (the
            # narrative renderers read payload["slug"] — keep emitting it). [item 12]
            payload={"id": change_id, "slug": change.slug, "title": title},
        )
        return change

    @classmethod
    def load(cls, control_root: Path, change_id: str) -> Change:
        # .resolve() to match create (create_worktree returns a resolved path).
        wt_path = (control_root / ".imp" / "worktrees" / change_id).resolve()
        change_dir = wt_path / ".imp" / "change"
        if not change_dir.is_dir():
            raise LoadError(f"no such Change {change_id!r}", SourceLoc(file=change_dir, line=1))
        meta = json.loads((change_dir / "change.json").read_text())
        return cls(
            id=meta["id"], title=meta["title"], base_branch=meta["base_branch"],
            control_root=control_root, project_root=wt_path, change_dir=change_dir,
        )
