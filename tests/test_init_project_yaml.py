"""imp init writes imp/project.yaml (no bindings) + bootstraps user config; it is
idempotent on a clone that already has the committed project file (spec §4)."""
import subprocess
from pathlib import Path

import pytest
import yaml

from imp.cli.init import run_init
from imp.providers.config import parse_project_facts


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")


def _run(root: Path, xdg: Path, providers: list[str]) -> None:
    run_init(
        root,
        input_fn=lambda _p: "",
        output_fn=lambda _s: None,
        picker_fn=lambda _avail: providers,
        detect_theme_fn=lambda: "dark",
    )


def test_init_writes_project_yaml_without_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, xdg, ["claude_code", "openai"])

    proj = yaml.safe_load((root / "imp" / "project.yaml").read_text())
    assert "tiers" not in proj      # bindings come from the code floor
    assert "providers" not in proj  # providers are user-homed
    assert "theme" not in proj
    assert proj["default-workflow"] == "imp"
    assert "workflow" not in proj
    assert not (root / "imp" / "workflows").exists()
    assert not (root / "imp" / "playbooks").exists()


def test_init_bootstraps_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, xdg, ["claude_code", "openai"])

    user = yaml.safe_load((xdg / "imp" / "config.yaml").read_text())
    assert user["providers"] == ["claude_code", "openai"]
    assert user["theme"] == "dark"
    assert user["editing_mode"] == "emacs"


def test_init_idempotent_on_existing_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    root = tmp_path / "repo"
    (root / "imp").mkdir(parents=True)
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    # A teammate's clone has the committed project file but no user config yet.
    _run(root, xdg, ["openai"])  # must NOT raise FileExistsError
    # Project file left untouched; user config now bootstrapped.
    assert (root / "imp" / "project.yaml").read_text() == "default-workflow: imp\n"
    assert (xdg / "imp" / "config.yaml").exists()


def test_init_leaves_existing_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    (xdg / "imp").mkdir(parents=True)
    (xdg / "imp" / "config.yaml").write_text("providers: [anthropic]\ntheme: light\n")
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, xdg, ["openai"])
    # Pre-existing user config preserved (machine bootstrapped once).
    user = yaml.safe_load((xdg / "imp" / "config.yaml").read_text())
    assert user["providers"] == ["anthropic"]


def test_init_appends_commented_facts_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, xdg, ["claude_code"])

    text = (root / "imp" / "project.yaml").read_text()
    assert "# facts:" in text
    assert "{{ project." in text

    facts, guidance = parse_project_facts(root / "imp" / "project.yaml")
    assert facts == {}
    assert guidance == ""


def test_init_records_checked_out_branch_without_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(root)], check=True)
    run_init(
        root,
        input_fn=lambda _p: "",
        output_fn=lambda _s: None,
        picker_fn=lambda _avail: ["claude_code"],
        detect_theme_fn=lambda: "dark",
        detect_verify_fn=lambda _p: None,
    )
    proj = yaml.safe_load((root / "imp" / "project.yaml").read_text())
    assert proj["default-base"] == "trunk"


def test_init_prefers_origin_head_over_checked_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "feature", str(root)], check=True)
    # `symbolic-ref <name> <target>` writes the ref without requiring <target>
    # to exist; a non-`feature` target proves origin/HEAD wins over the checkout.
    subprocess.run(
        ["git", "-C", str(root), "symbolic-ref",
         "refs/remotes/origin/HEAD", "refs/remotes/origin/mainline"],
        check=True,
    )
    lines: list[str] = []
    run_init(
        root,
        input_fn=lambda _p: "",
        output_fn=lines.append,
        picker_fn=lambda _avail: ["claude_code"],
        detect_theme_fn=lambda: "dark",
        detect_verify_fn=lambda _p: None,
    )
    proj = yaml.safe_load((root / "imp" / "project.yaml").read_text())
    assert proj["default-base"] == "mainline"
    assert "Integration branch: mainline" in lines


def test_init_omits_default_base_when_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--allow-empty", "-qm", "seed"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "checkout", "--detach", "-q"], check=True)
    run_init(
        root,
        input_fn=lambda _p: "",
        output_fn=lambda _s: None,
        picker_fn=lambda _avail: ["claude_code"],
        detect_theme_fn=lambda: "dark",
        detect_verify_fn=lambda _p: None,
    )
    proj = yaml.safe_load((root / "imp" / "project.yaml").read_text())
    assert "default-base" not in proj
    assert proj["default-workflow"] == "imp"


def test_init_echoes_integration_branch_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(root)], check=True)
    lines: list[str] = []
    run_init(
        root,
        input_fn=lambda _p: "",
        output_fn=lines.append,
        picker_fn=lambda _avail: ["claude_code"],
        detect_theme_fn=lambda: "dark",
        detect_verify_fn=lambda _p: None,
    )
    assert "Integration branch: trunk" in lines
