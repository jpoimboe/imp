from types import SimpleNamespace

from imp.cli.model_command import validate_model_arg


def test_validate_rejects_malformed_syntax() -> None:
    err = validate_model_arg("anthropic:", change=SimpleNamespace())
    assert err is not None and "anthropic:" in err


def test_validate_rejects_unavailable_provider(monkeypatch) -> None:
    import imp.cli.model_command as mod
    monkeypatch.setattr(mod, "_available_for_change", lambda change: ["anthropic"])
    err = validate_model_arg("openai:gpt-5", change=SimpleNamespace())
    assert err is not None and "openai" in err


def test_validate_accepts_available_provider(monkeypatch) -> None:
    import imp.cli.model_command as mod
    monkeypatch.setattr(mod, "_available_for_change", lambda change: ["anthropic"])
    assert validate_model_arg(
        "anthropic:claude-fable-5", change=SimpleNamespace()) is None


def test_validate_bare_model_needs_no_provider_check(monkeypatch) -> None:
    import imp.cli.model_command as mod
    monkeypatch.setattr(mod, "_available_for_change", lambda change: [])
    assert validate_model_arg("claude-fable-5", change=SimpleNamespace()) is None


def test_render_roster_marks_current_and_groups_by_provider() -> None:
    from imp.cli.model_command import render_roster
    from imp.providers.config import ProviderModel
    from imp.providers.model_roster import ProviderRoster, RosterEntry

    current = ProviderModel("claude_code", "claude-opus-4-8[1m]")
    rosters = [
        ProviderRoster("claude_code", (
            RosterEntry("opus"),
            RosterEntry("sonnet"),
            RosterEntry("claude-fable-5[1m]", "Fable 5 · Most capable"),
        )),
        ProviderRoster("anthropic", (RosterEntry("claude-sonnet-4-6"),)),
    ]
    lines = render_roster(current, rosters)
    assert "Current: claude_code · claude-opus-4-8[1m]   (this step)" in lines
    assert "  claude_code" in lines
    assert "  anthropic" in lines
    # plain (description-less) names share one joined line
    assert "    opus · sonnet" in lines
    # a described entry sits on its own line, description shown
    assert any(
        "claude-fable-5[1m]" in line and "Fable 5 · Most capable" in line
        for line in lines)
    assert any("Switch with" in line for line in lines)


def test_print_model_roster_threads_cache_and_current(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    import imp.cli.model_command as mod
    from imp.providers.config import ProviderModel
    from imp.providers.model_roster import ProviderRoster, RosterEntry

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
        return [ProviderRoster("claude_code", (RosterEntry("opus"),))]

    monkeypatch.setattr("imp.providers.model_roster.discover_roster", _discover)

    change = SimpleNamespace(
        state_dir=tmp_path, project_root=tmp_path,
        control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    mod.print_model_roster(change=change, output_fn=out.append)

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

    monkeypatch.setattr(mod, "_available_for_change", lambda change: ["anthropic"])
    reason = validate_model_arg("openai:gpt-5.5", change=object())
    assert reason is not None
    assert "openai" in reason
    assert "not available" in reason
