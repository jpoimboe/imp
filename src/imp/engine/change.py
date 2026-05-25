"""Change lifecycle: allocate ID, snapshot workflow, write metadata, load existing."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from imp.engine.errors import LoadError
from imp.engine.state import EventLog, State
from imp.engine.types import SourceLoc


@dataclass
class ChangeMetadata:
    id: int
    title: str
    created_at: str  # ISO-8601


@dataclass
class Change:
    id: int
    title: str
    project_root: Path
    change_dir: Path

    @property
    def workflow_dir(self) -> Path:
        return self.change_dir / "workflow"

    @property
    def artifacts_dir(self) -> Path:
        return self.change_dir / "artifacts"

    @property
    def state_dir(self) -> Path:
        return self.change_dir / "state"

    @property
    def events_path(self) -> Path:
        return self.change_dir / "events.jsonl"

    def event_log(self) -> EventLog:
        return EventLog(self.events_path)

    @classmethod
    def create(cls, project_root: Path, title: str | None = None) -> Change:
        changes_root = project_root / ".imp" / "changes"
        changes_root.mkdir(parents=True, exist_ok=True)
        existing = sorted(
            int(p.name) for p in changes_root.iterdir()
            if p.is_dir() and p.name.isdigit()
        )
        new_id = (existing[-1] + 1) if existing else 1
        change_dir = changes_root / str(new_id)
        change_dir.mkdir()

        # Snapshot workflow
        src_wf = project_root / ".imp" / "workflows" / "default"
        if not src_wf.is_dir():
            raise LoadError(
                f"no default workflow at {src_wf}",
                SourceLoc(file=src_wf, line=1),
            )
        shutil.copytree(src_wf, change_dir / "workflow")
        (change_dir / "artifacts").mkdir()
        (change_dir / "state").mkdir()

        title = title or f"Change {new_id}"
        metadata = ChangeMetadata(
            id=new_id,
            title=title,
            created_at=datetime.now(UTC).isoformat(),
        )
        (change_dir / "change.json").write_text(
            json.dumps(asdict(metadata), indent=2, sort_keys=True)
        )

        # Threading configured_roles here ensures V12 fires for real Changes
        # (plan revision #2): without it, undefined model_role references slip
        # past validation.
        from imp.engine.loader import load_workflow
        from imp.providers.config import load_config

        configured_roles = load_config(project_root).role_names()
        workflow = load_workflow(
            change_dir / "workflow", configured_roles=configured_roles
        )
        initial = State(
            change_id=new_id,
            phase=workflow.phases[0].name,
            step=None,
            step_attempt=0,
            phase_attempt=0,
            status="running",
        )
        initial.save(change_dir / "state")

        change = cls(
            id=new_id,
            title=title,
            project_root=project_root,
            change_dir=change_dir,
        )
        change.event_log().append(
            "change_created",
            {"change_id": new_id, "title": title},
        )
        return change

    @classmethod
    def load(cls, project_root: Path, change_id: int) -> Change:
        change_dir = project_root / ".imp" / "changes" / str(change_id)
        if not change_dir.is_dir():
            raise LoadError(
                f"no such Change {change_id}",
                SourceLoc(file=change_dir, line=1),
            )
        meta = json.loads((change_dir / "change.json").read_text())
        return cls(
            id=meta["id"],
            title=meta["title"],
            project_root=project_root,
            change_dir=change_dir,
        )
