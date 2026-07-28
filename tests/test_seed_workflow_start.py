from pathlib import Path

from imp.assets import resolve_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.quest import Quest
from imp.engine.seed import seed_workflow_start
from imp.engine.state import State
from imp.providers.effective import load_effective_config
from tests.conftest import _git, _seed_control


def _seed_with_var(root: Path) -> None:
    """A seeded control root whose workflow declares `n: int = 7` and whose
    local config layer overrides it to 42. The workflow.yaml change is committed
    (it is read from the checked-out tree); config.yaml lives under gitignored
    .imp/ and is read from disk directly."""
    _seed_control(root)
    src = root / "imp" / "workflows" / "imp"
    (src / "workflow.yaml").write_text(
        'name: t\ndescription: ""\n'
        "vars:\n  n: {type: int, default: 7}\n"
        "phases:\n  - name: p\n    label: P\n    steps:\n      - {name: s, file: agents/stub.md}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "declare var n")
    (root / ".imp" / "config.yaml").write_text(
        "providers: [anthropic]\n"
        "tiers:\n"
        "  low: {anthropic: x}\n  medium: {anthropic: x}\n  high: {anthropic: x}\n"
        "vars:\n  n: 42\n"
    )


def test_seed_workflow_start_seeds_cursor_and_vars(tmp_path: Path) -> None:
    _seed_with_var(tmp_path)
    config = load_effective_config(
        project_root=tmp_path, control_root=tmp_path,
    ).config
    workflow = load_workflow(
        resolve_workflow_dir(tmp_path, "imp"),
        configured_tiers=config.tier_names(),
    )
    state = State(id="q", step=None, step_attempt=0, status="new")

    seed_workflow_start(state, workflow, config)

    assert state.step == "s"
    assert state.step_label is None      # step `s` has no label: → honestly None
    assert state.phase_label == "P"
    assert state.vars["n"] == 42


def test_create_routes_seeded_cursor_and_var_override(tmp_path: Path) -> None:
    _seed_with_var(tmp_path)
    ch = Quest.create(tmp_path, title="X", quest_id="seedq", base_branch="main")
    state = State.load(ch.quest_dir / "state")
    assert state.step == "s"
    assert state.step_label is None      # step `s` has no label: → honestly None
    assert state.phase_label == "P"
    assert state.vars["n"] == 42


def test_create_draft_routes_seeded_cursor_and_var_override(tmp_path: Path) -> None:
    _seed_with_var(tmp_path)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="seeddr", base_branch="main")
    state = State.load(ch.quest_dir / "state")
    assert state.step == "s"
    assert state.step_label is None      # step `s` has no label: → honestly None
    assert state.phase_label == "P"
    assert state.vars["n"] == 42
