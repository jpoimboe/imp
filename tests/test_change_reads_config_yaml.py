"""Quest.create reads the project layer from the MAIN CHECKOUT's imp/config.yaml,
never the quest worktree's copy (spec §1, AC 10b)."""
import shutil
import subprocess
from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.providers import register_builtin_providers, resolve


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _registry() -> None:
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")  # an env-available vendor
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "imp").mkdir()
    # config.yaml carries NO tiers — the floor must supply them.
    (root / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    # global config supplies the providers list (global-homed).
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("providers: [openai]\n")
    (root / "imp" / "workflows").mkdir()
    shutil.copytree(bundled_workflow_dir("default"), root / "imp" / "workflows" / "imp")
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


def test_change_create_uses_floor_tiers_without_a_tiers_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path, monkeypatch)
    quest = Quest.create(root, title="t", quest_id="2026-06-08-x", base_branch="main")
    assert quest.id == "2026-06-08-x"


def test_create_merges_the_main_checkouts_layer_not_the_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worktree forks the COMMITTED tip, so an uncommitted edit in the main
    checkout is a value only the control root has. It must be the one that seeds
    the quest's vars."""
    root = _repo(tmp_path, monkeypatch)
    # plan_total is declared by the bundled default workflow with default 1.
    (root / "imp" / "config.yaml").write_text(
        "default-workflow: imp\nvars:\n  plan_total: 7\n")

    quest = Quest.create(root, title="t", quest_id="2026-06-08-y", base_branch="main")

    assert quest.project_root != root                       # a real worktree
    committed = (quest.project_root / "imp" / "config.yaml").read_text()
    assert "plan_total" not in committed                    # the branch never saw it
    assert State.load(quest.state_dir).vars["plan_total"] == 7
