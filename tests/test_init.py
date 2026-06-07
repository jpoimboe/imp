"""`imp init` scaffolding."""

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
    assert "roles" in data
    assert "default" in data["roles"]
    prefer = data["roles"]["default"]["prefer"]
    assert prefer[0]["provider"] == "anthropic"

    wf = tmp_path / "imp" / "workflows" / "default" / "workflow.yaml"
    assert wf.is_file()

    # No per-.imp .gitignore anymore; .imp/ is ignored wholesale by project .gitignore.
    assert not (tmp_path / ".imp").exists()


def test_appends_to_project_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("# existing\nfoo/\n")
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("anthropic"),
        input_fn=_inputs("ANTHROPIC_API_KEY"),
        output_fn=lambda _msg: None,
    )
    text = (tmp_path / ".gitignore").read_text()
    assert "foo/" in text  # didn't clobber
    assert ".imp/" in text  # wholesale engine-state ignore


def test_init_idempotent_refuses_existing(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("roles: {}\n")
    with pytest.raises(FileExistsError):
        run_init(
            project_root=tmp_path,
            picker_fn=_picks("anthropic"),
            input_fn=_inputs("ANTHROPIC_API_KEY"),
            output_fn=lambda _msg: None,
        )


def test_supports_both_providers(tmp_path: Path) -> None:
    run_init(
        project_root=tmp_path,
        picker_fn=_picks("anthropic", "openai"),
        input_fn=_inputs("ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
        output_fn=lambda _msg: None,
    )
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    has_openai = any(
        any(e["provider"] == "openai" for e in role["prefer"])
        for role in data["roles"].values()
    )
    assert has_openai


def test_supports_claude_code(tmp_path: Path) -> None:
    # claude_code uses the local `claude` CLI — no env-var prompt, and no
    # entry in the informational `providers:` block.
    def _no_input(_prompt: str = "") -> str:
        raise AssertionError(f"unexpected input prompt: {_prompt!r}")

    run_init(
        project_root=tmp_path,
        picker_fn=_picks("claude_code"),
        input_fn=_no_input,
        output_fn=lambda _msg: None,
    )
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    assert data["roles"]["default"]["prefer"][0]["provider"] == "claude_code"
    assert "claude_code" not in data.get("providers", {})


def test_picker_sees_all_three_providers(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def _picker(available: list[str]) -> list[str]:
        seen.append(list(available))
        return ["anthropic"]

    run_init(
        project_root=tmp_path,
        picker_fn=_picker,
        input_fn=_inputs("ANTHROPIC_API_KEY"),
        output_fn=lambda _msg: None,
    )
    assert seen == [["claude_code", "anthropic", "openai"]]


def test_empty_picker_result_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        run_init(
            project_root=tmp_path,
            picker_fn=lambda _available: [],
            input_fn=_inputs(),
            output_fn=lambda _msg: None,
        )


def test_init_code_and_code_review_prefer_heads(tmp_path: Path) -> None:
    """Generated config: code prefer-head = haiku; code_review Anthropic entry =
    sonnet; code_review OpenAI entry = gpt-5.2 (the 1-vendor column resolves as
    specified; the constraint handles the 2-vendor column)."""
    run_init(tmp_path, input_fn=lambda _p: "", output_fn=lambda _m: None,
             picker_fn=lambda _a: ["anthropic", "openai"])
    cfg = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    code = cfg["roles"]["code"]["prefer"]
    review = cfg["roles"]["code_review"]["prefer"]
    assert code[0] == {"provider": "anthropic", "model": "haiku"}
    assert review[0] == {"provider": "anthropic", "model": "sonnet"}
    assert {"provider": "openai", "model": "gpt-5.2"} in review


def test_init_code_review_handles_claude_code_via_default_tier(tmp_path: Path) -> None:
    """A {provider: tier} mapping must include a 'default' tier so providers not
    named explicitly (e.g. claude_code) still resolve."""
    run_init(tmp_path, input_fn=lambda _p: "", output_fn=lambda _m: None,
             picker_fn=lambda _a: ["claude_code", "openai"])
    cfg = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    review = {e["provider"]: e["model"] for e in cfg["roles"]["code_review"]["prefer"]}
    assert review["claude_code"] == "sonnet"  # default tier = coding
    assert review["openai"] == "gpt-5.2"     # openai tier = thinking


def test_init_scaffolds_plan_and_task_lead_roles(tmp_path: Path) -> None:
    run_init(tmp_path, input_fn=lambda _p: "", output_fn=lambda _m: None,
             picker_fn=lambda _a: ["anthropic"])
    cfg = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    roles = cfg["roles"]
    for role in ("plan", "plan_review", "task_lead"):
        assert role in roles, f"{role} missing from scaffolded roles"
        assert roles[role]["prefer"], f"{role} has an empty prefer list"


def test_init_seeds_tier_roles(tmp_path: Path) -> None:
    run_init(
        tmp_path, input_fn=lambda _p: "", output_fn=lambda _m: None,
        picker_fn=lambda _avail: ["anthropic"])
    data = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    for tier in ("fast", "balanced", "powerful"):
        assert tier in data["roles"], f"{tier} role not seeded"
    models = {t: data["roles"][t]["prefer"][0]["model"] for t in
              ("fast", "balanced", "powerful")}
    assert models["fast"] == "haiku"
    assert models["balanced"] == "sonnet"
    assert models["powerful"] == "opus"


def test_scaffolds_bare_model_aliases(tmp_path: Path) -> None:
    run_init(tmp_path, input_fn=lambda _p: "", output_fn=lambda _s: None,
             picker_fn=lambda _avail: ["claude_code"])
    cfg = yaml.safe_load((tmp_path / "imp" / "config.yaml").read_text())
    models = {pm["model"] for role in cfg["roles"].values() for pm in role["prefer"]}
    assert models <= {"opus", "sonnet", "haiku"}, models
    assert "opus" in models and "haiku" in models


def test_init_scaffolds_commented_lint_command(tmp_path: Path) -> None:
    run_init(
        tmp_path,
        input_fn=lambda _p: "",
        output_fn=lambda _m: None,
        picker_fn=lambda _avail: ["anthropic"],
    )
    text = (tmp_path / "imp" / "config.yaml").read_text()
    assert "# lint_command:" in text
