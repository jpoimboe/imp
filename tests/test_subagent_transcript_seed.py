from __future__ import annotations

from pathlib import Path

from imp.agents.tools.context import ToolContext


def _ctx(tmp_path: Path, agent_id: str = "step:plan/coder") -> ToolContext:
    run = tmp_path / ".imp" / "change"
    run.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        project_root=tmp_path,
        fs_permissions=(),
        events_path=run / "events.jsonl",
        parent_transcript_dir=run / "transcripts" / "plan",
        agent_id=agent_id,
    )


# --- C1: disk-seeding the subagent counter ----------------------------------

def test_seed_past_existing_dirs(tmp_path: Path) -> None:
    """With implementer-1 and implementer-2 present, the next allocated id is >= 3
    — a fresh dispatch never re-targets implementer-1."""
    from imp.agents.subagents import _seed_subagent_counter
    ctx = _ctx(tmp_path)
    base = ctx.parent_transcript_dir / "subagents"
    (base / "implementer-1").mkdir(parents=True)
    (base / "implementer-2").mkdir()
    _seed_subagent_counter(ctx)
    assert ctx.next_subagent_invocation_id() == 3


def test_seed_global_max_across_names(tmp_path: Path) -> None:
    """The counter is one global sequence shared across subagent names: mixed
    names seed to the global max suffix (3), not a per-name count."""
    from imp.agents.subagents import _seed_subagent_counter
    ctx = _ctx(tmp_path)
    base = ctx.parent_transcript_dir / "subagents"
    for name in ("implementer-1", "quality-reviewer-2", "implementer-3"):
        (base / name).mkdir(parents=True, exist_ok=True)
    _seed_subagent_counter(ctx)
    assert ctx._subagent_counter == 3
    assert ctx.next_subagent_invocation_id() == 4


def test_seed_noop_without_dir_or_with_advanced_counter(tmp_path: Path) -> None:
    """Guard: seeds only when subagents/ exists AND the counter is still 0; never
    re-seeds after the first dispatch."""
    from imp.agents.subagents import _seed_subagent_counter
    ctx = _ctx(tmp_path)
    _seed_subagent_counter(ctx)                 # subagents/ dir absent -> no-op
    assert ctx.next_subagent_invocation_id() == 1
    base = ctx.parent_transcript_dir / "subagents"
    (base / "implementer-9").mkdir(parents=True)  # appears AFTER counter advanced
    _seed_subagent_counter(ctx)                 # counter != 0 -> no re-seed
    assert ctx.next_subagent_invocation_id() == 2


def test_allocate_fresh_run_numbering_unchanged(tmp_path: Path) -> None:
    """With no subagents/ dir the allocator numbers from 1, unchanged from today."""
    from imp.agents.subagents import _allocate_subagent_dir
    ctx = _ctx(tmp_path)
    inv_id, target = _allocate_subagent_dir(ctx, "implementer")
    assert inv_id == 1
    assert target == ctx.parent_transcript_dir / "subagents" / "implementer-1"


def test_allocate_returns_seeded_fresh_dir(tmp_path: Path) -> None:
    """The allocator seeds first, so with implementer-1 present it returns id 2."""
    from imp.agents.subagents import _allocate_subagent_dir
    ctx = _ctx(tmp_path)
    base = ctx.parent_transcript_dir / "subagents"
    (base / "implementer-1").mkdir(parents=True)
    inv_id, target = _allocate_subagent_dir(ctx, "implementer")
    assert inv_id == 2
    assert target == base / "implementer-2"
