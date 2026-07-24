"""imp init writes imp/project.yaml (no bindings) + bootstraps user config; it is
idempotent on a clone that already has the committed project file (spec §4)."""
import subprocess
from pathlib import Path

import pytest
import yaml

from imp.cli.init import IntegrationBranchUnresolved, run_init
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
    assert proj["default-workflow"] == "default"
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

    facts = parse_project_facts(root / "imp" / "project.yaml")
    assert facts == {}


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


def test_init_prompts_for_base_on_existing_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--allow-empty", "-qm", "seed"],
        check=True,
    )
    prompts: list[str] = []

    def _input(p: str = "") -> str:
        prompts.append(p)
        return "release"

    run_init(
        root,
        input_fn=_input,
        output_fn=lambda _s: None,
        picker_fn=lambda _a: ["claude_code"],
        detect_theme_fn=lambda: "dark",
        detect_verify_fn=lambda _p: None,
    )
    proj = yaml.safe_load((root / "imp" / "project.yaml").read_text())
    assert proj["default-base"] == "release"
    assert any("integrate into" in p for p in prompts)


def test_init_unreadable_prompt_writes_no_project_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--allow-empty", "-qm", "seed"],
        check=True,
    )

    def _eof(_p: str = "") -> str:
        raise EOFError

    with pytest.raises(IntegrationBranchUnresolved):
        run_init(
            root,
            input_fn=_eof,
            output_fn=lambda _s: None,
            picker_fn=lambda _a: ["claude_code"],
            detect_theme_fn=lambda: "dark",
            detect_verify_fn=lambda _p: None,
        )
    assert not (root / "imp" / "project.yaml").exists()
    assert not (root / "imp").exists()  # resolve runs before mkdir — no partial artifact


def test_init_fresh_repo_records_base_without_integration_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "repo"
    root.mkdir()
    prompts: list[str] = []

    def _input(p: str = "") -> str:
        prompts.append(p)
        return ""  # accept the "Initialize a git repository?" default (Y)

    run_init(
        root,
        input_fn=_input,
        output_fn=lambda _s: None,
        picker_fn=lambda _a: ["claude_code"],
        detect_theme_fn=lambda: "dark",
        detect_verify_fn=lambda _p: None,
    )
    proj = yaml.safe_load((root / "imp" / "project.yaml").read_text())
    # Value depends on git's init.defaultBranch; the precise-value case is
    # covered by test_init_records_checked_out_branch_without_remote.
    assert "default-base" in proj                       # base always recorded
    assert not any("integrate into" in p for p in prompts)  # no integration prompt


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


def test_init_scaffolds_inert_memory_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imp.providers.memory import strip_html_comments

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, xdg, ["claude_code"])

    global_md = root / "imp" / "memory" / "all.md"
    wf_md = root / "imp" / "memory" / "default" / "all.md"
    assert global_md.is_file()
    assert wf_md.is_file()
    # Both are comment-only: they strip to empty and inject nothing.
    assert strip_html_comments(global_md.read_text()).strip() == ""
    assert strip_html_comments(wf_md.read_text()).strip() == ""
    # Both scaffolds carry a "sparingly" caution.
    assert "sparingly" in global_md.read_text().lower()
    assert "sparingly" in wf_md.read_text().lower()


def test_init_memory_snapshot_resolves_to_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imp.providers.memory import load_memory_snapshot, resolve_memory

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, xdg, ["claude_code"])

    snap = load_memory_snapshot(root / "imp" / "memory")
    assert set(snap) == {"all.md", "default/all.md"}
    # A freshly initialized project injects no memory at any step.
    assert resolve_memory(snap, workflow="default", step="threshold") == ""


def test_init_facts_hint_points_at_memory_not_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, xdg, ["claude_code"])

    text = (root / "imp" / "project.yaml").read_text()
    assert "imp/memory/" in text        # points at the new home for standing prose
    assert "guidance:" not in text      # the removed key's stub is gone
    assert "# facts:" in text           # facts guidance survives


def test_init_reinit_without_scaffolds_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression guard for the extended commit pathspec: an already-initialized
    # clone whose imp/memory/ scaffolds are absent must re-run cleanly. The
    # scaffolds are written only on a fresh init, so `_commit_init_files` must
    # stage only the memory paths that actually exist — `git add` with a
    # pathspec matching no file exits non-zero.
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "imp").mkdir()
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    _run(root, xdg, ["claude_code"])  # must NOT raise CalledProcessError
    assert not (root / "imp" / "memory").exists()  # no scaffold on re-init
