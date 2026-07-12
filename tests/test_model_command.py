from types import SimpleNamespace

from imp.cli.model_command import validate_model_arg


def test_validate_rejects_malformed_syntax() -> None:
    err = validate_model_arg("anthropic:", quest=SimpleNamespace())
    assert err is not None and "anthropic:" in err


def test_validate_rejects_unavailable_provider(monkeypatch) -> None:
    import imp.cli.model_command as mod
    monkeypatch.setattr(mod, "_available_for_quest", lambda quest: ["anthropic"])
    err = validate_model_arg("openai:gpt-5", quest=SimpleNamespace())
    assert err is not None and "openai" in err


def test_validate_accepts_available_provider(monkeypatch) -> None:
    import imp.cli.model_command as mod
    monkeypatch.setattr(mod, "_available_for_quest", lambda quest: ["anthropic"])
    assert validate_model_arg(
        "anthropic:claude-fable-5", quest=SimpleNamespace()) is None


def test_validate_bare_model_needs_no_provider_check(monkeypatch) -> None:
    import imp.cli.model_command as mod
    monkeypatch.setattr(mod, "_available_for_quest", lambda quest: [])
    assert validate_model_arg("claude-fable-5", quest=SimpleNamespace()) is None


def test_render_roster_one_per_line_hides_bare_aliases() -> None:
    from imp.cli.model_command import render_roster
    from imp.providers.config import ProviderModel
    from imp.providers.model_roster import ProviderRoster

    current = ProviderModel("claude_code", "claude-opus-4-8[1m]")
    rosters = [
        ProviderRoster("claude_code", (
            "claude-opus-4-8", "opus", "claude-fable-5[1m]")),
        ProviderRoster("anthropic", ("claude-sonnet-4-6",)),
    ]
    lines = render_roster(current, rosters)
    assert "Current: claude_code · claude-opus-4-8[1m]   (this step)" in lines
    assert "  claude_code" in lines
    assert "  anthropic" in lines
    # one model per line, bare name, 4-space indent
    assert "    claude-opus-4-8" in lines
    assert "    claude-fable-5[1m]" in lines
    assert "    claude-sonnet-4-6" in lines
    # the bare alias is hidden, but a dated id merely CONTAINING "opus" is kept
    assert "    opus" not in lines
    # no `·`-joined model line and no description side-notes (the Current line excepted)
    body = [ln for ln in lines if not ln.startswith("Current:")]
    assert all(" · " not in ln for ln in body)
    assert any("Switch with" in line for line in lines)


def test_print_model_roster_threads_cache_and_current(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    import imp.cli.model_command as mod
    from imp.providers.config import ProviderModel
    from imp.providers.model_roster import ProviderRoster

    state = SimpleNamespace(
        step="brainstorm", project_facts={},
        resolved_steps=None, resolved_tiers=None)
    monkeypatch.setattr("imp.engine.state.State.load", lambda state_dir: state)
    monkeypatch.setattr(
        "imp.providers.effective.load_effective_config",
        lambda **kw: SimpleNamespace(
            config=SimpleNamespace(tiers={}, providers=())))
    monkeypatch.setattr("imp.engine.loader.load_workflow", lambda *a, **k: object())
    monkeypatch.setattr(
        "imp.engine.model_override.resolve_current_model",
        lambda state, wf, cfg, step: ProviderModel("claude_code", "opus"))
    monkeypatch.setattr(
        "imp.providers.claude_model_cache.read_claude_model_options",
        lambda: ["SENTINEL-CACHE"])

    captured: dict[str, object] = {}

    def _discover(config, *, claude_options):
        captured["claude_options"] = claude_options
        return [ProviderRoster("claude_code", ("claude-opus-4-8",))]

    monkeypatch.setattr("imp.providers.model_roster.discover_roster", _discover)

    quest = SimpleNamespace(
        state_dir=tmp_path, project_root=tmp_path,
        control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    mod.print_model_roster(quest=quest, output_fn=out.append)

    # the best-effort cache read is threaded into discovery
    assert captured["claude_options"] == ["SENTINEL-CACHE"]
    # the resolved current step's model is marked current
    assert any("Current: claude_code · opus" in line for line in out)
    assert any("  claude_code" in line for line in out)


def test_validate_model_available_rejects_unavailable_provider() -> None:
    from imp.cli.model_command import validate_model_available

    reason = validate_model_available(
        "openai:gpt-5.5", available=["anthropic", "claude_code"])
    assert reason is not None
    assert "openai" in reason
    assert "not available" in reason


def test_validate_model_available_allows_available_provider() -> None:
    from imp.cli.model_command import validate_model_available

    assert validate_model_available(
        "anthropic:claude-fable-5", available=["anthropic"]) is None


def test_validate_model_available_allows_bare_model() -> None:
    from imp.cli.model_command import validate_model_available

    # No `provider:` — nothing to check; passes through regardless of `available`.
    assert validate_model_available(
        "claude-fable-5", available=["anthropic"]) is None


def test_validate_model_arg_still_rejects_unavailable_via_delegation(
    monkeypatch,
) -> None:
    # The existing change-based entry keeps its behavior after the refactor.
    import imp.cli.model_command as mod
    from imp.cli.model_command import validate_model_arg

    monkeypatch.setattr(mod, "_available_for_quest", lambda quest: ["anthropic"])
    reason = validate_model_arg("openai:gpt-5.5", quest=object())
    assert reason is not None
    assert "openai" in reason
    assert "not available" in reason
