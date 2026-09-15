# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Quest lifecycle (spec §3, §5): immutable id identity, free-form title, worktree
creation, permanent state home at project_state_dir(control_root)/quests/<id>/."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from imp.engine import durable, worktree
from imp.engine.env import quest_var_tmp_root
from imp.engine.errors import LayoutMismatch, LoadError, LoadErrors
from imp.engine.seed import seed_workflow_start
from imp.engine.state import JsonlEventLog, State
from imp.engine.types import SourceLoc
from imp.paths import project_state_dir

LAYOUT_VERSION = 23  # bump in the SAME commit as any breaking quest-home layout/schema change


def _write_quest_json(meta_path: Path, meta: dict[str, Any]) -> None:
    encoded = json.dumps(meta, indent=2, sort_keys=True).encode()
    durable.durable_replace_bytes(meta_path, encoded)


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
    """Live quest-home ids: dirs under <state dir>/quests/ holding a quest.json
    (archive excluded)."""
    root = project_state_dir(control_root) / "quests"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".")
                  and (p / "quest.json").exists())


def live_stems(control_root: Path) -> frozenset[str]:
    """Four-letter stems of every live quest home — the mint avoid-set."""
    return frozenset(cid[:4] for cid in live_quest_ids(control_root))


@dataclass
class Quest:
    id: str                      # immutable identity (spec §1), minted at imp new
    title: str                   # mutable human name
    base_branch: str
    control_root: Path
    project_root: Path           # the worktree
    quest_dir: Path             # permanent home <state dir>/quests/<id>
    materialized: bool = True    # False only for an un-materialized draft (no worktree)
    orig_base_head: str = ""     # base tip captured at draft creation; NEVER a diff base
    cur_base_head: str = ""      # the SHA the branch is currently planted on (the fork)
    workflow: str | None = None  # per-quest workflow name; None = project default
    origin: str = "draft"        # provenance: draft | backlog (Backlog only)
    create_mode: str = "new"     # how the conversation was started: new | chat (leave disposition)
    seed_opening: str | None = None  # title+description seed for a fresh resume open
    parent: str | None = None  # parent quest id (create_draft provenance); None when standalone
    depends_on: list[str] = field(default_factory=list)  # dependency quest ids (gates go-live)
    vars: dict[str, object] = field(default_factory=dict)  # per-quest workflow var overrides
    provider: str | None = None  # per-quest provider pin; None = follow configured order

    def rename(self, name: str, *, log: JsonlEventLog) -> str:
        """Content-only rename (no git): set title, rewrite quest.json in the
        out-of-tree state dir (never dirties the tracked worktree), emit quest_renamed.
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

    def set_base_branch(self, branch: str) -> None:
        """Re-point the fork and re-record orig_base_head, keeping head_changed coherent."""
        head = worktree.head_of(self.control_root, branch)
        self.base_branch = branch
        self.orig_base_head = head
        meta_path = self.quest_dir / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["base_branch"] = branch
        meta["orig_base_head"] = head
        _write_quest_json(meta_path, meta)

    def reload_base(self) -> None:
        """Re-read the base from quest.json: the daemon may have changed it since load."""
        meta = json.loads((self.quest_dir / "quest.json").read_text())
        self.base_branch = meta["base_branch"]
        self.orig_base_head = meta.get("orig_base_head", "")

    def set_seed_opening(self, text: str | None) -> None:
        self.seed_opening = text
        meta_path = self.quest_dir / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["seed_opening"] = text
        _write_quest_json(meta_path, meta)

    def set_provider(self, provider: str | None) -> None:
        self.provider = provider
        meta_path = self.quest_dir / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["provider"] = provider
        _write_quest_json(meta_path, meta)

    def reload_provider(self) -> None:
        """Re-read the pin onto THIS instance. Another process may have written
        it since we were loaded, and the daemon's set_provider callback mutates
        this same object — rebinding to a freshly loaded Quest would strand both
        the reconcile points and the wake predicate on a copy nobody writes."""
        meta = json.loads((self.quest_dir / "quest.json").read_text())
        self.provider = meta.get("provider")

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
        self.reload_base()
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
    def agent_quest_home(self) -> str:
        """The quest home as agents address it: worktree-relative through the
        id-scoped symlink when the agent runs in a worktree, absolute when it runs
        in the main checkout, where no symlink exists."""
        return (str(self.quest_dir) if self.project_root == self.control_root
                else f".imp/quests/{self.id}")

    @property
    def live_workflow_dir(self) -> Path:
        from imp.assets import resolve_workflow_dir
        from imp.providers.effective import resolve_default_workflow
        name = self.workflow or resolve_default_workflow(control_root=self.control_root)
        return resolve_workflow_dir(self.project_root, name)

    @property
    def control_workflow_dir(self) -> Path:
        """Mirror of live_workflow_dir rooted at the control root, not the worktree."""
        from imp.assets import resolve_workflow_dir
        from imp.providers.effective import resolve_default_workflow
        name = self.workflow or resolve_default_workflow(control_root=self.control_root)
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
        """Create branch imp/<id> + worktree <state dir>/worktrees/<id>, then seed
        internal state at <state dir>/quests/<id>. Raises
        worktree.BranchExists on the (effectively impossible) id collision —
        the caller reports a clean error."""
        wt_path = worktree.create_worktree(control_root, quest_id, base_branch)
        cur_base_head = worktree.head_of(control_root, base_branch)
        quest_dir = project_state_dir(control_root) / "quests" / quest_id
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
            "vars": {},
            "provider": None,
        }, indent=2, sort_keys=True))

        from imp.engine.loader import load_workflow
        from imp.providers.effective import (
            load_effective_config,
            validate_project_memory,
        )
        from imp.providers.memory import load_memory_snapshot

        try:
            config = load_effective_config(control_root=control_root).config
            validate_project_memory(
                wt_path, load_memory_snapshot(wt_path / "imp" / "memory"))
            from imp.assets import resolve_workflow_dir
            wf_name = workflow or config.default_workflow
            loaded_workflow = load_workflow(
                resolve_workflow_dir(wt_path, wf_name),
                configured_tiers=config.tier_names(),
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
        create_mode: str = "new", vars: Mapping[str, object] | None = None,
        provider: str | None = None,
    ) -> Quest:
        """An un-materialized draft: NO branch, NO worktree. State is seeded at the
        permanent home; project_root points at the main checkout so the spec
        brainstorm runs read-only there. The base branch's current tip is recorded
        as orig_base_head for the later go-live head_changed check. Validates the
        workflow against the main checkout; with no branch/worktree to roll back,
        a bad/absent workflow drops the just-created home and re-raises."""
        quest_dir = project_state_dir(control_root) / "quests" / quest_id
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
            "vars": dict(vars or {}),
            "provider": provider,
        }, indent=2, sort_keys=True))

        if defer_seed:
            # Skip load/validate/seed so workflow skew can't veto stub creation;
            # the cursor is seeded at first resume instead.
            State(
                id=quest_id, step=None, step_attempt=0, status="new",
            ).save(quest_dir / "state")
        else:
            from imp.engine.loader import load_workflow
            from imp.providers.effective import (
                load_effective_config,
                validate_project_memory,
            )
            from imp.providers.memory import load_memory_snapshot

            try:
                config = load_effective_config(control_root=control_root).config
                validate_project_memory(
                    control_root,
                    load_memory_snapshot(control_root / "imp" / "memory"))
                from imp.assets import resolve_workflow_dir
                wf_name = workflow or config.default_workflow
                loaded_workflow = load_workflow(
                    resolve_workflow_dir(control_root, wf_name),
                    configured_tiers=config.tier_names(),
                )
                state = State(id=quest_id, step=None, step_attempt=0, status="new")
                seed_workflow_start(state, loaded_workflow, config, quest_vars=vars)
            except (LoadError, LoadErrors):
                shutil.rmtree(quest_dir, ignore_errors=True)
                raise
            state.save(quest_dir / "state")

        quest = cls(
            id=quest_id, title=title, base_branch=base_branch,
            control_root=control_root, project_root=control_root,
            quest_dir=quest_dir, materialized=False, orig_base_head=orig_base_head,
            workflow=workflow, origin=origin, create_mode=create_mode,
            seed_opening=seed_opening, parent=parent,
            depends_on=list(depends_on or []),
            vars=dict(vars or {}),
            provider=provider,
        )
        quest.event_log().append(
            agent_id=None, type="quest_created",
            payload={"id": quest_id, "title": title},
        )
        return quest

    @classmethod
    def load(cls, control_root: Path, quest_id: str) -> Quest:
        quest_dir = project_state_dir(control_root) / "quests" / quest_id
        if not (quest_dir / "quest.json").is_file():
            raise LoadError(f"no such quest {quest_id!r}", SourceLoc(file=quest_dir, line=1))
        meta = json.loads((quest_dir / "quest.json").read_text())
        stored = meta.get("layout_version", 0)
        if stored != LAYOUT_VERSION:
            raise LayoutMismatch(
                stored=stored, engine=LAYOUT_VERSION,
                imp_commit=meta.get("imp_commit", ""), quest_dir=quest_dir)
        project_root = (
            Path(meta["project_root"]) if meta.get("project_root")
            else (project_state_dir(control_root) / "worktrees" / quest_id).resolve()
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
            vars=dict(meta.get("vars") or {}),
            provider=meta.get("provider"),
        )
