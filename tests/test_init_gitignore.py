"""Fix D: run_init gitignores .imp/ wholesale; committed config under imp/."""

from __future__ import annotations

from pathlib import Path

from imp.cli.init import run_init


def _init(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path,
        input_fn=lambda _p: "",
        output_fn=lambda _l: None,
        picker_fn=lambda _a: ["anthropic"],
    )


def test_imp_gitignore_covers_internal_state(tmp_path: Path) -> None:
    _init(tmp_path)
    # No per-.imp .gitignore anymore; the project .gitignore ignores .imp/ wholesale.
    assert not (tmp_path / ".imp").exists()  # init no longer creates .imp/
    assert ".imp/" in {ln.strip() for ln in (tmp_path / ".gitignore").read_text().splitlines()}


def test_project_gitignore_ignores_imp_wholesale(tmp_path: Path) -> None:
    # A pre-existing .gitignore (e.g. from copytree) must be appended to, not
    # clobbered. The project rule ignores .imp/ wholesale.
    (tmp_path / ".gitignore").write_text(".venv/\n")
    _init(tmp_path)
    first = (tmp_path / ".gitignore").read_text()
    assert ".venv/" in first       # preserved
    assert ".imp/" in first        # wholesale engine-state ignore


def test_wholesale_appended_when_granular_rules_preexist(tmp_path: Path) -> None:
    # REGRESSION: the copied host .gitignore already carries granular .imp/*
    # rules. A substring idempotency filter (`".imp/" in existing`) matches
    # `.imp/changes/` and wrongly suppresses the wholesale `.imp/` append. With
    # line-wise equality it must still be appended.
    (tmp_path / ".gitignore").write_text(
        ".venv/\n.imp/changes/\n.imp/config.yaml\n.imp/lock\n")
    _init(tmp_path)
    lines = {ln.strip() for ln in
             (tmp_path / ".gitignore").read_text().splitlines()}
    assert ".imp/" in lines, "wholesale .imp/ rule was not appended"


def test_imp_dir_fully_ignored_after_init(tmp_path: Path) -> None:
    # Belt-and-suspenders: after run_init over a host-like .gitignore, `git
    # check-ignore` agrees the whole .imp/ tree is ignored, so a seed `git add
    # .` cannot stage engine state.
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(
        ".venv/\n.imp/changes/\n.imp/config.yaml\n.imp/lock\n")
    _init(tmp_path)
    # .imp/ is now ignored wholesale by the project .gitignore.
    # Create a sentinel file inside .imp/ to test the ignore rule.
    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "some-state").write_text("runtime state")
    for p in (".imp/some-state",):
        r = subprocess.run(["git", "check-ignore", p], cwd=tmp_path,
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{p} is NOT gitignored (rc={r.returncode})"
