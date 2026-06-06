"""`imp init` scaffolding (post 2026-06-04-roles-to-tiers)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from imp.cli.init import run_init


def _inputs(*answers: str) -> object:
    it = iter(answers)
    return lambda _prompt="": next(it)


def _picks(*selected: str) -> object:
    return lambda _available: list(selected)


@pytest.fixture(autouse=True)
def _stub_theme_detection(monkeypatch):
    # init tests must NOT fire a real OSC-11 query at the terminal — pin the
    # detector to a deterministic 'ansi'. Persistence of a detected theme is
    # covered separately via the detect_theme_fn injection point.
    monkeypatch.setattr("imp.cli.init._default_detect_theme", lambda: "ansi")


def test_writes_config_workflows_gitignore(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("anthropic"),
        input_fn=_inputs("ANTHROPIC_API_KEY"),
        output_fn=lambda _msg: None,
    )
    cfg = tmp_path / "imp" / "config.yaml"
    assert cfg.is_file()
    data = yaml.safe_load(cfg.read_text())
    assert set(data) <= {
        "providers", "tiers", "provider_envs",
        "lint_command", "test_command", "editing_mode", "vars", "theme",
    }
    assert data["theme"] == "ansi"      # stubbed detector
    assert "roles" not in data
    assert "model_rules" not in data
    assert "model_tiers" not in data
    assert data["providers"] == ["anthropic"]
    assert set(data["tiers"]) == {"low", "medium", "high"}
    assert data["tiers"]["low"]["anthropic"] == "haiku"
    assert data["tiers"]["medium"]["anthropic"] == "sonnet"
    assert data["tiers"]["high"]["anthropic"] == "opus"

    wf = tmp_path / "imp" / "workflows" / "default" / "workflow.yaml"
    assert wf.is_file()
    # No per-.imp .gitignore anymore; .imp/ is ignored wholesale.
    assert not (tmp_path / ".imp").exists()


def test_persists_detected_theme(tmp_path: Path) -> None:
    # The detected theme is written to config.yaml so the runtime never re-queries.
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs("ANTHROPIC_API_KEY"), output_fn=lambda _msg: None,
        detect_theme_fn=lambda: "dark",
    )
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["theme"] == "dark"


def test_appends_to_project_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("# existing\nfoo/\n")
    run_init(
        project_root=tmp_path, picker_fn=_picks("anthropic"),
        input_fn=_inputs("ANTHROPIC_API_KEY"),
        output_fn=lambda _msg: None,
    )
    text = (tmp_path / ".gitignore").read_text()
    assert "foo/" in text and ".imp/" in text


def test_init_idempotent_refuses_existing(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("providers: [anthropic]\n")
    with pytest.raises(FileExistsError):
        run_init(
            project_root=tmp_path, picker_fn=_picks("anthropic"),
            input_fn=_inputs("ANTHROPIC_API_KEY"),
            output_fn=lambda _msg: None,
        )


def test_supports_two_providers_writes_both_in_tier_table(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("claude_code", "openai"),
        input_fn=_inputs("OPENAI_API_KEY"),    # claude_code has no env prompt
        output_fn=lambda _msg: None,
    )
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["providers"] == ["claude_code", "openai"]
    medium = data["tiers"]["medium"]
    assert medium["claude_code"] == "sonnet"
    assert medium["openai"] == "gpt-5.3-codex"
    high = data["tiers"]["high"]
    assert high["claude_code"] == "opus"
    assert high["openai"] == "gpt-5.5"
    low = data["tiers"]["low"]
    assert low["claude_code"] == "haiku"
    assert low["openai"] == "gpt-5.4-mini"


def test_supports_claude_code_no_env_prompt(tmp_path: Path) -> None:
    def _no_input(_prompt: str = "") -> str:
        raise AssertionError(f"unexpected input prompt: {_prompt!r}")

    run_init(
        project_root=tmp_path, picker_fn=_picks("claude_code"),
        input_fn=_no_input, output_fn=lambda _msg: None,
    )
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["providers"] == ["claude_code"]
    # provider_envs is omitted entirely when no env-driven provider was picked.
    assert "provider_envs" not in data


def test_picker_sees_all_providers(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def _picker(available: list[str]) -> list[str]:
        seen.append(list(available))
        return ["anthropic"]

    run_init(
        project_root=tmp_path, picker_fn=_picker,
        input_fn=_inputs("ANTHROPIC_API_KEY"),
        output_fn=lambda _msg: None,
    )
    assert seen == [["claude_code", "anthropic", "openai", "codex"]]


def test_empty_picker_result_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        run_init(
            project_root=tmp_path, picker_fn=lambda _available: [],
            input_fn=_inputs(), output_fn=lambda _msg: None,
        )


def test_init_scaffolds_commented_lint_command(tmp_path: Path) -> None:
    run_init(
        tmp_path, input_fn=lambda _p: "",
        output_fn=lambda _m: None,
        picker_fn=lambda _avail: ["anthropic"],
    )
    text = (tmp_path / "imp" / "config.yaml").read_text()
    assert "# lint_command:" in text


def test_init_provider_envs_uses_default_when_blank(tmp_path: Path) -> None:
    run_init(
        tmp_path,
        input_fn=lambda _p: "",            # accept default env name
        output_fn=lambda _m: None,
        picker_fn=lambda _avail: ["openai"],
    )
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["provider_envs"] == {"openai": {"env": "OPENAI_API_KEY"}}


def test_init_provider_envs_honors_custom_name(tmp_path: Path) -> None:
    run_init(
        tmp_path,
        input_fn=lambda _p: "OAI_KEY",
        output_fn=lambda _m: None,
        picker_fn=lambda _avail: ["openai"],
    )
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["provider_envs"] == {"openai": {"env": "OAI_KEY"}}
