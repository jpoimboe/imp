# tests/test_change.py
import json
from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.engine.state import State
from tests.conftest import _git, _init_repo, _seed_control


def _seed_control_without_workflow(root: Path) -> None:
    """Seeds the control root with project.yaml and tier config but NO
    imp/workflows/imp, so resolution falls back to the bundled workflow."""
    _init_repo(root)
    (root / "imp").mkdir()
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
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
    assert state.id == "abc123" and state.step == "s" and state.status == "new"


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
                         "cur_base_head", "layout_version", "imp_commit",
                         "workflow"}


def test_workflow_dir_is_live_dir_when_no_snapshot_exists(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="isolat", base_branch="main")
    # The executed workflow lives in the worktree's tracked tree...
    assert ch.workflow_dir == ch.project_root / "imp" / "workflows" / "imp"
    assert "phases:" in (ch.workflow_dir / "workflow.yaml").read_text()
    # ...and there is no gitignored second copy.
    assert not (ch.change_dir / "workflow").exists()


def test_create_without_committed_workflow_resolves_bundled(tmp_path: Path) -> None:
    from imp.assets import bundled_workflow_dir
    _seed_control_without_workflow(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="bund01", base_branch="main")
    # No committed imp/workflows/imp -> the installed bundled workflow runs.
    assert ch.workflow_dir == bundled_workflow_dir("imp")
    assert (tmp_path / ".imp" / "worktrees" / "bund01").exists()


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
    assert loaded.orig_base_head == ""


def test_create_draft_makes_no_worktree_or_branch(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", change_id="drf1", base_branch="main")
    assert ch.materialized is False
    assert ch.project_root == tmp_path                       # the main checkout
    assert not (tmp_path / ".imp" / "worktrees" / "drf1").exists()
    assert "imp/drf1" not in _git(tmp_path, "branch", "--list", "imp/drf1").stdout
    state = State.load(ch.change_dir / "state")
    assert state.step == "s" and state.status == "new"   # seeded at the first step
    events_text = (ch.change_dir / "events.jsonl").read_text()
    events = [json.loads(line) for line in events_text.splitlines() if line.strip()]
    assert any(e["type"] == "change_created" for e in events)


def test_create_draft_records_orig_base_head(tmp_path: Path) -> None:
    import json
    _seed_control(tmp_path)
    tip = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    ch = Change.create_draft(tmp_path, title="X", change_id="drf2", base_branch="main")
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["orig_base_head"] == tip
    assert "base_head" not in meta
    assert "cur_base_head" not in meta
    assert meta["materialized"] is False
    assert meta["project_root"] == str(tmp_path)


def test_create_draft_without_committed_workflow_resolves_bundled(tmp_path: Path) -> None:
    from imp.assets import bundled_workflow_dir
    _seed_control_without_workflow(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", change_id="drf3", base_branch="main")
    # No committed imp/workflows/imp -> the installed bundled workflow runs.
    assert ch.live_workflow_dir == bundled_workflow_dir("imp")
    assert (tmp_path / ".imp" / "changes" / "drf3").exists()


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


def test_create_persists_cur_base_head(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    tip = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    ch = Change.create(tmp_path, title="X", change_id="cbh1", base_branch="main")
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["cur_base_head"] == tip
    assert ch.cur_base_head == tip


def test_materialize_persists_cur_base_head_and_reports_head_changed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", change_id="cbh2", base_branch="main")
    orig = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    head_changed = ch.materialize()
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["cur_base_head"] == orig
    assert ch.cur_base_head == orig
    assert head_changed == (meta["cur_base_head"] != meta["orig_base_head"])
    assert head_changed is False


def test_materialize_reports_head_changed_true(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    orig_tip = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    ch = Change.create_draft(tmp_path, title="X", change_id="hc1", base_branch="main")
    assert ch.orig_base_head == orig_tip
    # Add a commit to main so the base tip moves between draft and materialize.
    (tmp_path / "extra.txt").write_text("extra\n")
    _git(tmp_path, "add", "extra.txt")
    _git(tmp_path, "commit", "-m", "extra commit")
    new_tip = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    assert new_tip != orig_tip
    head_changed = ch.materialize()
    assert head_changed is True
    assert ch.cur_base_head == new_tip
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["cur_base_head"] == new_tip


def test_load_exposes_orig_and_cur_base_head(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", change_id="cbh3", base_branch="main")
    ch.materialize()
    loaded = Change.load(tmp_path, "cbh3")
    assert loaded.orig_base_head == ch.orig_base_head
    assert loaded.cur_base_head == ch.cur_base_head
    assert loaded.cur_base_head != ""


def test_migrate_v1_to_v2(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="mig1", base_branch="main")
    meta_path = ch.change_dir / "change.json"
    meta = json.loads(meta_path.read_text())
    # Reconstruct a v1 change.json: old `base_head` key, no orig/cur, version 1.
    old_tip = meta.pop("cur_base_head")
    meta["base_head"] = old_tip
    meta["layout_version"] = 1
    meta_path.write_text(json.dumps(meta))
    loaded = Change.load(tmp_path, "mig1")
    migrated = json.loads(meta_path.read_text())
    assert migrated["layout_version"] == 5
    assert "base_head" not in migrated
    assert migrated["orig_base_head"] == old_tip
    assert migrated["cur_base_head"] == old_tip      # backfill = merge-base(main, branch HEAD)
    assert loaded.orig_base_head == old_tip
    assert loaded.cur_base_head == old_tip


def test_migrate_v2_to_v3(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="mig2", base_branch="main")
    meta_path = ch.change_dir / "change.json"
    meta = json.loads(meta_path.read_text())
    # Reconstruct a v2 change.json (before the reincarnations field bump).
    meta["layout_version"] = 2
    meta_path.write_text(json.dumps(meta))
    Change.load(tmp_path, "mig2")
    migrated = json.loads(meta_path.read_text())
    assert migrated["layout_version"] == 5


def test_migrate_v3_to_v4(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="mig3", base_branch="main")
    meta_path = ch.change_dir / "change.json"
    meta = json.loads(meta_path.read_text())
    # Reconstruct a v3 change.json (before the max_reincarnations field bump).
    meta["layout_version"] = 3
    meta_path.write_text(json.dumps(meta))
    Change.load(tmp_path, "mig3")
    migrated = json.loads(meta_path.read_text())
    assert migrated["layout_version"] == 5


def test_create_draft_persists_and_resolves_workflow_override(tmp_path: Path) -> None:
    import shutil
    _seed_control(tmp_path)
    shutil.copytree(tmp_path / "imp" / "workflows" / "imp",
                    tmp_path / "imp" / "workflows" / "mine")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "fork")
    ch = Change.create_draft(tmp_path, title="X", change_id="dft001",
                             base_branch="main", workflow="mine")
    assert ch.workflow == "mine"
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["workflow"] == "mine"
    loaded = Change.load(tmp_path, "dft001")
    assert loaded.workflow == "mine"
    # The override is consulted ahead of default-workflow: imp.
    assert loaded.live_workflow_dir == tmp_path / "imp" / "workflows" / "mine"


def test_create_draft_unknown_workflow_rolls_back(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    with pytest.raises(LoadError):
        Change.create_draft(tmp_path, title="X", change_id="dft002",
                             base_branch="main", workflow="bogus")
    assert not (tmp_path / ".imp" / "changes" / "dft002").exists()


def test_create_persists_and_resolves_workflow_override(tmp_path: Path) -> None:
    import shutil
    _seed_control(tmp_path)
    shutil.copytree(tmp_path / "imp" / "workflows" / "imp",
                    tmp_path / "imp" / "workflows" / "mine")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "fork")
    ch = Change.create(tmp_path, title="X", change_id="crt001",
                       base_branch="main", workflow="mine")
    assert ch.workflow == "mine"
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["workflow"] == "mine"
    loaded = Change.load(tmp_path, "crt001")
    assert loaded.workflow == "mine"
    assert loaded.live_workflow_dir == loaded.project_root / "imp" / "workflows" / "mine"


def test_create_unknown_workflow_rolls_back(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    with pytest.raises(LoadError):
        Change.create(tmp_path, title="X", change_id="crt002",
                      base_branch="main", workflow="bogus")
    assert not (tmp_path / ".imp" / "changes" / "crt002").exists()


def test_create_draft_records_origin_and_seed_opening(tmp_path: Path) -> None:
    import json
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="Add OAuth", change_id="drforg1", base_branch="main",
        origin="idea", seed_opening="Add OAuth\n\nwire login")
    assert ch.origin == "idea"
    assert ch.seed_opening == "Add OAuth\n\nwire login"
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["origin"] == "idea"
    assert meta["seed_opening"] == "Add OAuth\n\nwire login"


def test_create_draft_defaults_origin_draft_and_seed_none(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", change_id="drforg2", base_branch="main")
    assert ch.origin == "draft"
    assert ch.seed_opening is None


def test_load_reads_origin_and_seed_opening(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(
        tmp_path, title="X", change_id="drforg3", base_branch="main",
        origin="followup", seed_opening="X\n\nbody")
    loaded = Change.load(tmp_path, "drforg3")
    assert loaded.origin == "followup"
    assert loaded.seed_opening == "X\n\nbody"


def test_set_origin_rewrites_change_json(tmp_path: Path) -> None:
    import json as _json

    from imp.engine import worktree
    from imp.engine.change import Change
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    base = worktree.current_branch(tmp_path)
    change = Change.create_draft(
        tmp_path, title="X", change_id="2026-06-24-orig01",
        base_branch=base, origin="draft")
    change.set_origin("idea")
    assert change.origin == "idea"
    meta = _json.loads((change.change_dir / "change.json").read_text())
    assert meta["origin"] == "idea"


def test_set_seed_opening_rewrites_change_json(tmp_path: Path) -> None:
    import json as _json

    from imp.engine import worktree
    from imp.engine.change import Change
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    base = worktree.current_branch(tmp_path)
    change = Change.create_draft(
        tmp_path, title="X", change_id="2026-06-24-seed01",
        base_branch=base, origin="draft")

    change.set_seed_opening("add a --json flag")
    assert change.seed_opening == "add a --json flag"
    meta = _json.loads((change.change_dir / "change.json").read_text())
    assert meta["seed_opening"] == "add a --json flag"

    # An unseeded fallback persists as null.
    change.set_seed_opening(None)
    assert change.seed_opening is None
    meta = _json.loads((change.change_dir / "change.json").read_text())
    assert meta["seed_opening"] is None


def test_set_depends_on_rewrites_change_json(tmp_path: Path) -> None:
    import json as _json

    from imp.engine import worktree
    from imp.engine.change import Change
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    base = worktree.current_branch(tmp_path)
    change = Change.create_draft(
        tmp_path, title="X", change_id="2026-06-24-deps01",
        base_branch=base, origin="draft")
    change.set_depends_on(["a", "b"])
    meta = _json.loads((change.change_dir / "change.json").read_text())
    assert meta["depends_on"] == ["a", "b"]
    change.set_depends_on([])
    meta = _json.loads((change.change_dir / "change.json").read_text())
    assert meta["depends_on"] == []


def test_materialize_raises_on_empty_title_and_creates_nothing(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="   ", change_id="ttl1", base_branch="main")
    with pytest.raises(ValueError, match="rename_change"):
        ch.materialize()
    # Nothing was created and the change stays un-materialized.
    assert ch.materialized is False
    assert not (tmp_path / ".imp" / "worktrees" / "ttl1").exists()
    assert "imp/ttl1" not in _git(tmp_path, "branch", "--list", "imp/ttl1").stdout
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["materialized"] is False


def test_materialize_untitled_then_named_recovery(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="", change_id="ttl2", base_branch="main")
    with pytest.raises(ValueError):
        ch.materialize()
    ch.rename("Real title", log=ch.event_log())
    head_changed = ch.materialize()
    wt = tmp_path / ".imp" / "worktrees" / "ttl2"
    assert wt.exists()
    assert ch.materialized is True and ch.project_root == wt
    assert head_changed is False


def test_materialize_idempotent_no_op_never_raises_even_when_title_emptied(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="Titled", change_id="ttl3", base_branch="main")
    ch.materialize()
    root_after_first = ch.project_root
    ch.rename("   ", log=ch.event_log())          # blank the title AFTER materializing
    assert ch.materialize() is False              # idempotent no-op, no raise
    assert ch.project_root == root_after_first


def test_control_workflow_dir_resolves_against_control_root(tmp_path: Path) -> None:
    """control_workflow_dir resolves the committed fork under control_root, even
    when the worktree (project_root) carries a divergent committed workflow —
    live_workflow_dir still points at the worktree."""
    ctl = tmp_path / "ctl"
    wt = tmp_path / "wt"
    for root, marker in ((ctl, "control-side"), (wt, "worktree-side")):
        wf = root / "imp" / "workflows" / "imp"
        (wf / "agents").mkdir(parents=True)
        (wf / "workflow.yaml").write_text(f"name: {marker}\n")
        (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    ch = Change(
        id="c", title="t", base_branch="main",
        control_root=ctl, project_root=wt, change_dir=tmp_path / "cd",
    )
    assert ch.control_workflow_dir == ctl / "imp" / "workflows" / "imp"
    assert ch.live_workflow_dir == wt / "imp" / "workflows" / "imp"


def test_control_workflow_dir_falls_back_to_bundled(tmp_path: Path) -> None:
    """With no committed imp/workflows/imp fork in the control root, the resolver
    falls back to the installed bundled workflow (the downstream-bundled case)."""
    from imp.assets import bundled_workflow_dir
    ctl = tmp_path / "ctl"
    (ctl / "imp").mkdir(parents=True)
    (ctl / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    ch = Change(
        id="c", title="t", base_branch="main",
        control_root=ctl, project_root=tmp_path / "wt", change_dir=tmp_path / "cd",
    )
    assert ch.control_workflow_dir == bundled_workflow_dir("imp")
