"""`imp init` scaffolding."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from imp.cli.init import run_init


def _inputs(*answers: str) -> object:
    it = iter(answers)
    return lambda _prompt="": next(it)


def test_writes_config_workflows_gitignore(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path,
        input_fn=_inputs("anthropic", "ANTHROPIC_API_KEY"),
        output_fn=lambda _msg: None,
    )
    cfg = tmp_path / ".imp" / "config.yaml"
    assert cfg.is_file()
    data = yaml.safe_load(cfg.read_text())
    assert "roles" in data
    assert "default" in data["roles"]
    prefer = data["roles"]["default"]["prefer"]
    assert prefer[0]["provider"] == "anthropic"

    wf = tmp_path / ".imp" / "workflows" / "default" / "workflow.yaml"
    assert wf.is_file()

    imp_gitignore = tmp_path / ".imp" / ".gitignore"
    assert imp_gitignore.is_file()
    assert "config.yaml" in imp_gitignore.read_text()
    assert "changes/" in imp_gitignore.read_text()


def test_appends_to_project_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("# existing\nfoo/\n")
    run_init(
        project_root=tmp_path,
        input_fn=_inputs("anthropic", "ANTHROPIC_API_KEY"),
        output_fn=lambda _msg: None,
    )
    text = (tmp_path / ".gitignore").read_text()
    assert "foo/" in text  # didn't clobber
    assert ".imp/config.yaml" in text or ".imp/changes/" in text


def test_init_idempotent_refuses_existing(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "config.yaml").write_text("roles: {}\n")
    with pytest.raises(FileExistsError):
        run_init(
            project_root=tmp_path,
            input_fn=_inputs("anthropic", "ANTHROPIC_API_KEY"),
            output_fn=lambda _msg: None,
        )


def test_supports_both_providers(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path,
        input_fn=_inputs(
            "anthropic openai", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        ),
        output_fn=lambda _msg: None,
    )
    data = yaml.safe_load((tmp_path / ".imp" / "config.yaml").read_text())
    # Each role should list both providers in some prefer entry — assert
    # at least one role has openai available.
    has_openai = any(
        any(e["provider"] == "openai" for e in role["prefer"])
        for role in data["roles"].values()
    )
    assert has_openai


def test_warns_on_unknown_provider(tmp_path: Path) -> None:
    warnings: list[str] = []
    run_init(
        project_root=tmp_path,
        input_fn=_inputs("anthropic googleai", "ANTHROPIC_API_KEY"),
        output_fn=warnings.append,
    )
    assert any("googleai" in msg for msg in warnings)
    # And the valid provider still gets configured
    data = yaml.safe_load((tmp_path / ".imp" / "config.yaml").read_text())
    assert data["roles"]["default"]["prefer"][0]["provider"] == "anthropic"
