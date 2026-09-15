from __future__ import annotations

from pathlib import Path

from imp.cli.init import run_init


def _run(project: Path, monkeypatch, lines: list[str]) -> None:
    # Pin the user-config path into a tmp dir so the test never touches a real
    # machine config; pick the providers non-interactively.
    user_cfg = project / "userhome" / "config.yaml"
    monkeypatch.setattr("imp.providers.paths.global_config_path",
                        lambda: user_cfg)
    import imp.cli.init as initmod
    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    # The confirmation screen accepts what init detected; `input_fn` answers the
    # git question, so a repo-less tmp dir still gets one.
    monkeypatch.setattr(
        initmod, "_interactive_confirm_screen",
        lambda _root, state, **_kw: initmod.ScreenAccepted(state), raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    run_init(
        project,
        input_fn=lambda _p: "",
        output_fn=lines.append,
        picker_fn=lambda _a: ["claude"],
        detect_theme_fn=lambda: "ansi",
    )


def test_fresh_machine_already_initialized_project(
    tmp_path: Path, monkeypatch
) -> None:
    # "Fresh workstation, project already in git": config.yaml exists but no
    # machine config yet → wrote_user_config=True path.
    imp_dir = tmp_path / "imp"
    imp_dir.mkdir()
    (imp_dir / "config.yaml").write_text("default-workflow: imp\nvars: {}\n")
    (imp_dir / "workflows").mkdir()
    (imp_dir / "workflows" / "imp").mkdir()

    lines: list[str] = []
    _run(tmp_path, monkeypatch, lines)
    text = "\n".join(lines)

    assert "Project already initialized" in text
    assert "Bootstrapped" in text


def test_already_initialized_message_separates_project_and_machine(
    tmp_path: Path, monkeypatch
) -> None:
    # First init creates imp/ AND the user config.
    first: list[str] = []
    _run(tmp_path, monkeypatch, first)
    # Second init: project already initialized, machine config already present.
    second: list[str] = []
    _run(tmp_path, monkeypatch, second)
    text = "\n".join(second)
    assert "Project already initialized" in text
    # The machine/user-config status is a SEPARATE statement, not joined into the
    # project sentence by an em dash.
    machine_lines = [ln for ln in second if "machine config" in ln]
    assert machine_lines  # at least one line mentions machine config
    project_lines = [ln for ln in second if "Project already initialized" in ln]
    assert project_lines  # and the project line is present
    assert machine_lines != project_lines  # they are on distinct output lines
