"""Change lifecycle (spec §3, §5): slug identity, worktree creation, state
home inside the worktree at <worktree>/.imp/change/."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from imp.engine import worktree
from imp.engine.errors import LoadError, LoadErrors
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
    auto_titled: bool = False    # provisional random slug (renamable in spec phase)

    @property
    def workflow_dir(self) -> Path:
        # The Change's own worktree carries the tracked workflow; the branch IS
        # the per-Change workflow source. No gitignored snapshot copy.
        return self.project_root / "imp" / "workflows" / "default"

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
        auto_titled: bool = False,
    ) -> Change:
        """Create branch+worktree (caller has already verified the slug is free
        and the gitignore precondition holds), then seed internal state inside
        the worktree. May raise worktree.BranchExists on a slug race."""
        wt_path = worktree.create_worktree(control_root, slug, base_branch)
        change_dir = wt_path / ".imp" / "change"
        change_dir.mkdir(parents=True)
        (change_dir / "state").mkdir()

        (change_dir / "change.json").write_text(json.dumps({
            "slug": slug, "title": title, "base_branch": base_branch,
            "auto_titled": auto_titled,
            "created_at": datetime.now(UTC).isoformat(),
        }, indent=2, sort_keys=True))

        from imp.engine.loader import load_workflow
        from imp.providers.config import apply_var_overrides, load_config

        config = load_config(control_root)
        # The branch's own tracked workflow IS the source — read it directly from
        # the worktree, no snapshot copy. This seed load doubles as the single
        # validator: raises LoadError on an absent/malformed workflow, or
        # LoadErrors when the workflow parses but fails validation (e.g. a step
        # references a model_role not present in imp/config.yaml).
        try:
            workflow = load_workflow(
                wt_path / "imp" / "workflows" / "default",
                configured_roles=config.role_names(),
            )
        except (LoadError, LoadErrors):
            # Validation now runs post-create_worktree, so a base branch lacking a
            # readable imp/workflows/default would otherwise leak the just-created
            # branch+worktree. Roll both back so a failed create leaves nothing.
            worktree.destroy_worktree(control_root, slug)
            raise
        flat = workflow.flat_steps()
        State(
            slug=slug, step=flat[0].name if flat else None, step_attempt=0,
            status="running",
            vars=apply_var_overrides(workflow.vars, config.var_overrides),
            loop_counts={},
        ).save(change_dir / "state")

        change = cls(
            slug=slug, title=title, base_branch=base_branch,
            control_root=control_root, project_root=wt_path, change_dir=change_dir,
            auto_titled=auto_titled,
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
            auto_titled=meta.get("auto_titled", False),
        )
