"""Change.create reads the project layer from imp/project.yaml in the worktree
(spec §7 step 3)."""
import shutil
import subprocess
from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir
from imp.engine.change import Change
from imp.providers import register_builtin_providers, resolve


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _registry() -> None:
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def test_change_create_uses_floor_tiers_without_a_tiers_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OPENAI_API_KEY", "x")  # an env-available vendor
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "imp").mkdir()
    # project.yaml carries NO tiers — the floor must supply them.
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    # user config supplies the providers list (user-homed).
    xdg = tmp_path / "xdg" / "imp"
    xdg.mkdir(parents=True)
    (xdg / "config.yaml").write_text("providers: [openai]\n")
    # Minimal default workflow so load_workflow succeeds.
    (root / "imp" / "workflows").mkdir()
    shutil.copytree(bundled_workflow_dir("imp"), root / "imp" / "workflows" / "imp")
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")

    change = Change.create(root, title="t", change_id="2026-06-08-x", base_branch="main")
    assert change.id == "2026-06-08-x"
