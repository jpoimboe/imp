"""Change lifecycle (spec §3, §5): slug identity, worktree creation, state
home inside the worktree at <worktree>/.imp/change/."""

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
    slug: str
    title: str
    base_branch: str
    control_root: Path
    project_root: Path           # the worktree
    change_dir: Path             # <worktree>/.imp/change

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
        cls, control_root: Path, *, title: str, slug: str, base_branch: str,
    ) -> Change:
        """Create branch+worktree (caller has already verified the slug is free
        and the gitignore precondition holds), then seed internal state inside
        the worktree. May raise worktree.BranchExists on a slug race."""
        wt_path = worktree.create_worktree(control_root, slug, base_branch)
        change_dir = wt_path / ".imp" / "change"
        change_dir.mkdir(parents=True)

        src_wf = control_root / "imp" / "workflows" / "default"
        if not src_wf.is_dir():
            raise LoadError(f"no default workflow at {src_wf}", SourceLoc(file=src_wf, line=1))
        shutil.copytree(src_wf, change_dir / "workflow")
        (change_dir / "state").mkdir()

        (change_dir / "change.json").write_text(json.dumps({
            "slug": slug, "title": title, "base_branch": base_branch,
            "created_at": datetime.now(UTC).isoformat(),
        }, indent=2, sort_keys=True))

        from imp.engine.loader import load_workflow
        from imp.providers.config import load_config

        configured_roles = load_config(control_root).role_names()
        workflow = load_workflow(change_dir / "workflow", configured_roles=configured_roles)
        flat = workflow.flat_steps()
        State(
            slug=slug, step=flat[0].name if flat else None, step_attempt=0,
            status="running", vars=dict(workflow.vars.defaults()), loop_counts={},
        ).save(change_dir / "state")

        change = cls(
            slug=slug, title=title, base_branch=base_branch,
            control_root=control_root, project_root=wt_path, change_dir=change_dir,
        )
        change.event_log().append(
            agent_id=None, type="change_created",
            payload={"slug": slug, "title": title},
        )
        return change

    @classmethod
    def load(cls, control_root: Path, slug: str) -> Change:
        # .resolve() to match Change.create (create_worktree returns a resolved
        # path); otherwise a symlinked control_root yields differently-normalized
        # project_root between create and load.
        wt_path = (control_root / ".imp" / "worktrees" / slug).resolve()
        change_dir = wt_path / ".imp" / "change"
        if not change_dir.is_dir():
            raise LoadError(f"no such Change {slug!r}", SourceLoc(file=change_dir, line=1))
        meta = json.loads((change_dir / "change.json").read_text())
        return cls(
            slug=meta["slug"], title=meta["title"], base_branch=meta["base_branch"],
            control_root=control_root, project_root=wt_path, change_dir=change_dir,
        )
