# tests/test_change.py
import json
from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.state import State
from tests.conftest import _git, _init_repo, _seed_control


def _seed_control_without_workflow(root: Path) -> None:
    """Seeds the control root with imp/config.yaml but NO imp/workflows/imp,
    so that workflow loading raises LoadError."""
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
                       change_id="abc123", base_branch="main")
    assert ch.id == "abc123"
    assert ch.handle == "add-oauth-abc123"   # derived from title + id
    assert ch.base_branch == "main"
    wtpath = tmp_path / ".imp" / "worktrees" / "abc123"
    assert ch.project_root == wtpath
    assert (ch.change_dir / "change.json").exists()
    # create() does not take a snapshot; no workflow/ snapshot dir yet.
    assert not (ch.change_dir / "workflow").exists()
    # workflow_dir IS the branch's own tracked workflow dir.
    assert ch.workflow_dir == wtpath / "imp" / "workflows" / "imp"
    assert (ch.workflow_dir / "workflow.yaml").exists()
    state = State.load(ch.change_dir / "state")
    assert state.id == "abc123" and state.step == "s" and state.status == "running"


def test_change_home_lives_at_control_root_outside_worktree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="hom123", base_branch="main")
    assert ch.change_dir == tmp_path / ".imp" / "changes" / "hom123"
    # The home is OUTSIDE the worktree: no inner .imp/change/ is created there.
    assert not (ch.project_root / ".imp" / "change").exists()
    assert (ch.change_dir / "change.json").exists()
    assert (ch.change_dir / "state" / "state.json").exists()


def test_load_change_by_id(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="X", change_id="mychng", base_branch="main")
    b = Change.load(tmp_path, "mychng")
    assert b.id == "mychng" and b.title == "X" and b.base_branch == "main"


def test_change_json_has_no_slug_or_auto_titled(tmp_path: Path) -> None:
    import json
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="zzz999", base_branch="main")
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert set(meta) == {"id", "title", "base_branch", "created_at",
                         "depends_on", "project_root", "materialized",
                         "layout_version", "imp_commit"}


def test_workflow_dir_is_live_dir_when_no_snapshot_exists(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="isolat", base_branch="main")
    # The executed workflow lives in the worktree's tracked tree...
    assert ch.workflow_dir == ch.project_root / "imp" / "workflows" / "imp"
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
    assert not (tmp_path / ".imp" / "changes" / "nowf").exists()


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
    workflow_yaml = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
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
    assert not (tmp_path / ".imp" / "changes" / "badwf").exists()


def test_workflow_dir_prefers_snapshot_when_present(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X",
                       change_id="snap01", base_branch="main")
    snapshot = ch.change_dir / "workflow"
    snapshot.mkdir()
    (snapshot / "workflow.yaml").write_text("name: frozen\n")
    # Once the snapshot exists, workflow_dir resolves to it, NOT the live dir.
    assert ch.workflow_dir == snapshot



def test_create_persists_project_root(tmp_path: Path) -> None:
    import json
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="prr", base_branch="main")
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["project_root"] == str((tmp_path / ".imp" / "worktrees" / "prr").resolve())


def test_load_reads_persisted_project_root(tmp_path: Path) -> None:
    import json
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="lrr", base_branch="main")
    # Rewrite the field to a sentinel to prove load READS it rather than
    # re-deriving project_root by worktree convention.
    meta_path = ch.change_dir / "change.json"
    meta = json.loads(meta_path.read_text())
    meta["project_root"] = "/sentinel/root"
    meta_path.write_text(json.dumps(meta))
    assert Change.load(tmp_path, "lrr").project_root == Path("/sentinel/root")


def test_set_project_root_rewrites_change_json(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="spr", base_branch="main")
    ch.set_project_root(Path("/new/wt"))
    assert ch.project_root == Path("/new/wt")
    assert Change.load(tmp_path, "spr").project_root == Path("/new/wt")


def test_live_workflow_dir_ignores_the_snapshot(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X",
                       change_id="snap03", base_branch="main")
    snapshot = ch.change_dir / "workflow"
    snapshot.mkdir()
    (snapshot / "workflow.yaml").write_text("name: frozen\n")
    # live_workflow_dir always points at the branch dir, even with a snapshot present.
    assert ch.live_workflow_dir == ch.project_root / "imp" / "workflows" / "imp"
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


def test_create_marks_materialized(tmp_path: Path) -> None:
    import json
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="mat1", base_branch="main")
    assert ch.materialized is True
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["materialized"] is True


def test_load_defaults_materialized_true_when_absent(tmp_path: Path) -> None:
    import json
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="mat2", base_branch="main")
    meta_path = ch.change_dir / "change.json"
    meta = json.loads(meta_path.read_text())
    del meta["materialized"]          # a pre-flag (legacy) change.json
    meta_path.write_text(json.dumps(meta))
    loaded = Change.load(tmp_path, "mat2")
    assert loaded.materialized is True
    assert loaded.base_head == ""


def test_create_draft_makes_no_worktree_or_branch(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", change_id="drf1", base_branch="main")
    assert ch.materialized is False
    assert ch.project_root == tmp_path                       # the main checkout
    assert not (tmp_path / ".imp" / "worktrees" / "drf1").exists()
    assert "imp/drf1" not in _git(tmp_path, "branch", "--list", "imp/drf1").stdout
    state = State.load(ch.change_dir / "state")
    assert state.step == "s" and state.status == "running"   # seeded at the first step
    events_text = (ch.change_dir / "events.jsonl").read_text()
    events = [json.loads(line) for line in events_text.splitlines() if line.strip()]
    assert any(e["type"] == "change_created" for e in events)


def test_create_draft_records_base_head(tmp_path: Path) -> None:
    import json
    _seed_control(tmp_path)
    tip = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    ch = Change.create_draft(tmp_path, title="X", change_id="drf2", base_branch="main")
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["base_head"] == tip
    assert meta["materialized"] is False
    assert meta["project_root"] == str(tmp_path)


def test_create_draft_refuses_bad_workflow_and_leaves_no_home(tmp_path: Path) -> None:
    _seed_control_without_workflow(tmp_path)
    with pytest.raises(LoadError):
        Change.create_draft(tmp_path, title="X", change_id="drf3", base_branch="main")
    assert not (tmp_path / ".imp" / "changes" / "drf3").exists()


def test_create_draft_refuses_invalid_workflow_and_leaves_no_home(
    tmp_path: Path,
) -> None:
    """A workflow that PARSES but fails validators raises LoadErrors. The
    just-created change home must be cleaned up (no orphan)."""
    _seed_control(tmp_path)
    workflow_yaml = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    workflow_yaml.write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: p\n    steps:\n"
        "      - {name: s, file: agents/stub.md, tier: nonexistent_tier}\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "break tier")
    with pytest.raises(LoadErrors):
        Change.create_draft(tmp_path, title="X", change_id="drf4", base_branch="main")
    assert not (tmp_path / ".imp" / "changes" / "drf4").exists()


def test_create_draft_refuses_bad_branch_and_leaves_no_home(tmp_path: Path) -> None:
    """If the base branch does not exist, head_of raises RuntimeError. The
    just-created change home must be cleaned up (no orphan)."""
    _seed_control(tmp_path)
    with pytest.raises(RuntimeError):
        Change.create_draft(
            tmp_path, title="X", change_id="drf5", base_branch="nonexistent-branch"
        )
    assert not (tmp_path / ".imp" / "changes" / "drf5").exists()


def test_materialize_forks_worktree_and_flips_root(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", change_id="mz1", base_branch="main")
    head_changed = ch.materialize()
    wt = tmp_path / ".imp" / "worktrees" / "mz1"
    assert wt.exists()
    assert "imp/mz1" in _git(tmp_path, "branch", "--list", "imp/mz1").stdout
    assert ch.project_root == wt and ch.materialized is True
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["project_root"] == str(wt) and meta["materialized"] is True
    assert head_changed is False          # base unchanged since draft creation


def test_materialize_reports_head_changed_when_base_advanced(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", change_id="mz2", base_branch="main")
    (tmp_path / "newfile.txt").write_text("x\n")     # advance the base branch tip
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "advance base")
    assert ch.materialize() is True


def test_materialize_is_idempotent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", change_id="mz3", base_branch="main")
    ch.materialize()
    root_after_first = ch.project_root
    assert ch.materialize() is False                 # second call: no-op
    assert ch.project_root == root_after_first
