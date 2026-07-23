from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.agents.skills import overlay_shared_partials
from imp.assets import bundled_partials_dir
from imp.engine.executor import _refresh_workflow_snapshot
from imp.engine.state import InMemoryEventLog
from tests.test_executor import _seed_minimal_workflow

_RELOCATED = (
    "coding-stance", "action-care", "imp-grounding", "spec-design-principles",
    "brainstorm-dialogue", "memory-authoring", "sandbox-debugging",
    "delegate-exploration", "live-gate-dialogue",
)


def test_bundled_partials_dir_holds_the_relocated_closure() -> None:
    d = bundled_partials_dir()
    assert d.is_dir()
    for name in _RELOCATED:
        assert (d / f"{name}.md").is_file(), name


def test_overlay_copies_shared_partials_into_dest(tmp_path: Path) -> None:
    dest = tmp_path / "partials"
    overlay_shared_partials(dest)
    assert (dest / "action-care.md").read_text() == (
        bundled_partials_dir() / "action-care.md").read_text()


def test_overlay_is_local_first_non_overwriting(tmp_path: Path) -> None:
    dest = tmp_path / "partials"
    dest.mkdir()
    (dest / "coding-stance.md").write_text("LOCAL WINS")
    overlay_shared_partials(dest)
    assert (dest / "coding-stance.md").read_text() == "LOCAL WINS"
    assert (dest / "action-care.md").is_file()   # non-colliding shared still lands


def test_render_resolves_shared_partial_via_fallback(tmp_path: Path) -> None:
    # workflow_dir has no partials/action-care.md; the shared fallback resolves it.
    out = render_prompt(
        '{% include "partials/action-care.md" %}',
        build_context(quest_title="t", event_log=InMemoryEventLog()),
        workflow_dir=tmp_path)
    assert "Measure twice, cut once" in out


def test_refresh_overlays_shared_partials_into_snapshot(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    assert _refresh_workflow_snapshot(quest) is False        # initial copy
    snap = quest.quest_dir / "workflow" / "partials"
    for name in _RELOCATED:
        assert (snap / f"{name}.md").is_file(), name


def test_refresh_workflow_partial_shadows_shared_on_collision(tmp_path: Path) -> None:
    quest, _, _ = _seed_minimal_workflow(tmp_path)
    live_partials = tmp_path / "imp" / "workflows" / "imp" / "partials"
    live_partials.mkdir(parents=True)
    sentinel = "WORKFLOW-OWN coding-stance\n"
    (live_partials / "coding-stance.md").write_text(sentinel)
    assert _refresh_workflow_snapshot(quest) is False        # initial copy
    snap = quest.quest_dir / "workflow" / "partials"
    assert (snap / "coding-stance.md").read_text() == sentinel
    assert (snap / "action-care.md").is_file()   # non-colliding shared still overlays
