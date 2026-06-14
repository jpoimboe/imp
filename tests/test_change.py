# tests/test_change.py
from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.state import State
from tests.conftest import _git, _init_repo, _seed_control


def _seed_control_without_workflow(root: Path) -> None:
    """A repo with committed imp/config.yaml but NO imp/workflows/default — so
    Change.create gets past create_worktree and config load, then fails the seed
    workflow load."""
    _init_repo(root)
    (root / "imp").mkdir()
    (root / "imp" / "config.yaml").write_text(
        "providers: [anthropic]\n"
        "tiers:\n  low: {anthropic: x}\n  medium: {anthropic: x}\n  high: {anthropic: x}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "config only")


def test_create_change_makes_worktree_and_state(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Add OAuth",
                       change_id="2026-06-04-abc123", base_branch="main")
    assert ch.id == "2026-06-04-abc123"
    assert ch.slug == "2026-06-04-add-oauth"     # derived from title + id's date
    assert ch.base_branch == "main"
    wtpath = tmp_path / ".imp" / "worktrees" / "2026-06-04-abc123"
    assert ch.project_root == wtpath
    assert (wtpath / ".imp" / "change" / "change.json").exists()
    # create() does not take a snapshot; no .imp/change/workflow/ dir yet.
    assert not (wtpath / ".imp" / "change" / "workflow").exists()
    # workflow_dir IS the branch's own tracked workflow dir.
    assert ch.workflow_dir == wtpath / "imp" / "workflows" / "default"
    assert (ch.workflow_dir / "workflow.yaml").exists()
    state = State.load(wtpath / ".imp" / "change" / "state")
    assert state.id == "2026-06-04-abc123" and state.step == "s" and state.status == "running"


def test_load_change_by_id(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="X", change_id="2026-06-04-mychng", base_branch="main")
    b = Change.load(tmp_path, "2026-06-04-mychng")
    assert b.id == "2026-06-04-mychng" and b.title == "X" and b.base_branch == "main"


def test_change_json_has_no_slug_or_auto_titled(tmp_path: Path) -> None:
    import json
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="2026-06-04-zzz999", base_branch="main")
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert set(meta) == {"id", "title", "base_branch", "created_at"}


def test_workflow_dir_is_live_dir_when_no_snapshot_exists(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="2026-06-04-isolat", base_branch="main")
    # The executed workflow lives in the worktree's tracked tree...
    assert ch.workflow_dir == ch.project_root / "imp" / "workflows" / "default"
    assert "phases:" in (ch.workflow_dir / "workflow.yaml").read_text()
    # ...and there is no gitignored second copy.
    assert not (ch.change_dir / "workflow").exists()


def test_create_without_workflow_raises_and_leaves_no_orphan(tmp_path: Path) -> None:
    _seed_control_without_workflow(tmp_path)
    with pytest.raises(LoadError):
        Change.create(tmp_path, title="X", change_id="nowf", base_branch="main")
    # The branch+worktree created before validation must be torn down.
    assert not (tmp_path / ".imp" / "worktrees" / "nowf").exists()
    assert "imp/nowf" not in _git(tmp_path, "branch", "--list", "imp/nowf").stdout


def test_create_with_invalid_workflow_raises_and_leaves_no_orphan(
    tmp_path: Path,
) -> None:
    """A workflow that PARSES but fails validators raises LoadErrors (not
    LoadError). Change.create must still tear down the worktree+branch — a
    validator failure is just as much a failed create as a missing workflow."""
    # Seed a valid repo, then overwrite workflow.yaml so the step references
    # an unconfigured/unknown tier NAME (`nonexistent_tier`). The YAML still
    # parses (no LoadError), but _v_step_tier_configured fires and LoadErrors
    # is raised.
    _seed_control(tmp_path)
    workflow_yaml = tmp_path / "imp" / "workflows" / "default" / "workflow.yaml"
    workflow_yaml.write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s, file: agents/stub.md, tier: nonexistent_tier}\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "break tier")

    with pytest.raises(LoadErrors):
        Change.create(tmp_path, title="X", change_id="badwf", base_branch="main")
    # The branch+worktree created before validation must be torn down.
    assert not (tmp_path / ".imp" / "worktrees" / "badwf").exists()
    assert "imp/badwf" not in _git(tmp_path, "branch", "--list", "imp/badwf").stdout


def test_workflow_dir_prefers_snapshot_when_present(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X",
                       change_id="2026-06-14-snap01", base_branch="main")
    snapshot = ch.change_dir / "workflow"
    snapshot.mkdir()
    (snapshot / "workflow.yaml").write_text("name: frozen\n")
    # Once the snapshot exists, workflow_dir resolves to it, NOT the live dir.
    assert ch.workflow_dir == snapshot



def test_live_workflow_dir_ignores_the_snapshot(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X",
                       change_id="2026-06-14-snap03", base_branch="main")
    snapshot = ch.change_dir / "workflow"
    snapshot.mkdir()
    (snapshot / "workflow.yaml").write_text("name: frozen\n")
    # live_workflow_dir always points at the branch dir, even with a snapshot present.
    assert ch.live_workflow_dir == ch.project_root / "imp" / "workflows" / "default"
    assert ch.workflow_dir == snapshot


def test_leaf_edit_pinned_in_graph_live_on_reload(tmp_path: Path) -> None:
    """A loaded Workflow is pinned (graph + leaves read once); a fresh load
    reflects an edited agent leaf. This is the self-test loop's guarantee: the
    running cursor is frozen, resume's re-load picks up the edit."""
    from imp.engine.loader import load_workflow

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="live", base_branch="main")

    wf_first = load_workflow(ch.workflow_dir)
    assert "prompt body" in wf_first.agents["stub"].prompt_template_source

    agent_md = ch.workflow_dir / "agents" / "stub.md"
    agent_md.write_text(agent_md.read_text().replace("prompt body", "EDITED BODY"))

    # The already-loaded graph is frozen — the edit is inert for this run.
    assert "EDITED BODY" not in wf_first.agents["stub"].prompt_template_source
    # A fresh load (what resume does) sees the edited bytes.
    wf_second = load_workflow(ch.workflow_dir)
    assert "EDITED BODY" in wf_second.agents["stub"].prompt_template_source
