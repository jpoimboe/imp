from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from imp.engine.model_override import apply_model_override, current_provider_model
from imp.providers.config import ProviderModel


@dataclass
class _Step:
    name: str
    tier: str


class _Workflow:
    def __init__(self, steps: list[_Step]) -> None:
        self._steps = steps

    def flat_steps(self) -> list[_Step]:
        return self._steps


def _state(resolved_steps, resolved_tiers, step="brainstorm"):
    return SimpleNamespace(
        step=step, resolved_steps=resolved_steps, resolved_tiers=resolved_tiers)


def test_current_provider_model_prefers_step_then_tier() -> None:
    wf = _Workflow([_Step("brainstorm", "high")])
    tiers = {"high": ProviderModel("claude", "opus")}
    # No per-step entry: falls back to the tier map.
    st = _state({}, tiers)
    assert current_provider_model(st, wf, "brainstorm") == ProviderModel(
        "claude", "opus")
    # Per-step entry wins.
    st2 = _state({"brainstorm": ProviderModel("openai", "gpt-5")}, tiers)
    assert current_provider_model(st2, wf, "brainstorm") == ProviderModel(
        "openai", "gpt-5")


def test_apply_bare_model_keeps_current_provider(monkeypatch) -> None:
    import imp.engine.model_override as mod
    wf = _Workflow([_Step("brainstorm", "high")])
    monkeypatch.setattr(mod, "load_workflow", lambda *a, **k: wf)
    st = _state({}, {"high": ProviderModel("claude", "opus")})
    quest = SimpleNamespace(workflow_dir="wf", state_dir="sd")
    monkeypatch.setattr(mod.State, "save", lambda self, d: None)

    apply_model_override(quest, st, "claude-fable-5", step_name="brainstorm")

    assert st.resolved_steps["brainstorm"] == ProviderModel(
        "claude", "claude-fable-5")


def test_apply_provider_model_switches_provider(monkeypatch) -> None:
    import imp.engine.model_override as mod
    wf = _Workflow([_Step("brainstorm", "high")])
    monkeypatch.setattr(mod, "load_workflow", lambda *a, **k: wf)
    monkeypatch.setattr(mod.State, "save", lambda self, d: None)
    st = _state({}, {"high": ProviderModel("claude", "opus")})
    quest = SimpleNamespace(workflow_dir="wf", state_dir="sd")

    apply_model_override(
        quest, st, "anthropic:claude-fable-5[1m]", step_name="brainstorm")

    assert st.resolved_steps["brainstorm"] == ProviderModel(
        "anthropic", "claude-fable-5[1m]")


def test_apply_initializes_none_resolved_steps(monkeypatch) -> None:
    import imp.engine.model_override as mod
    wf = _Workflow([_Step("brainstorm", "high")])
    monkeypatch.setattr(mod, "load_workflow", lambda *a, **k: wf)
    monkeypatch.setattr(mod.State, "save", lambda self, d: None)
    st = _state(None, {"high": ProviderModel("claude", "opus")})
    quest = SimpleNamespace(workflow_dir="wf", state_dir="sd")

    apply_model_override(quest, st, "claude-fable-5", step_name="brainstorm")

    assert st.resolved_steps == {
        "brainstorm": ProviderModel("claude", "claude-fable-5")}


def test_resolve_current_model_prefers_step_override() -> None:
    from imp.engine.model_override import resolve_current_model

    wf = _Workflow([_Step("brainstorm", "high")])
    st = _state(
        {"brainstorm": ProviderModel("openai", "gpt-5.5")},
        {"high": ProviderModel("claude", "claude-opus-4-8[1m]")})
    assert resolve_current_model(st, wf, object(), "brainstorm") == ProviderModel(
        "openai", "gpt-5.5")


def test_resolve_current_model_uses_tier_when_no_override() -> None:
    from imp.engine.model_override import resolve_current_model

    wf = _Workflow([_Step("brainstorm", "high")])
    st = _state(
        None, {"high": ProviderModel("claude", "claude-opus-4-8[1m]")})
    assert resolve_current_model(st, wf, object(), "brainstorm") == ProviderModel(
        "claude", "claude-opus-4-8[1m]")


def test_resolve_current_model_resolves_tiers_when_unresolved(monkeypatch) -> None:
    import imp.engine.model_override as mod
    from imp.engine.model_override import resolve_current_model

    wf = _Workflow([_Step("brainstorm", "high")])
    st = _state(None, None)  # opening prompt: nothing resolved yet
    monkeypatch.setattr(
        mod, "resolve_run",
        lambda workflow, config: (
            {"high": ProviderModel("anthropic", "claude-fable-5[1m]")}, {}, {}))
    assert resolve_current_model(st, wf, object(), "brainstorm") == ProviderModel(
        "anthropic", "claude-fable-5[1m]")


@pytest.mark.parametrize("provider", ["openai", "codex"])
def test_apply_astra_selection_persists_the_exact_provider_model(
        monkeypatch, provider: str) -> None:
    import imp.engine.model_override as mod

    monkeypatch.setattr(
        mod, "load_workflow", lambda *a, **k: _Workflow([_Step("brainstorm", "high")]))
    monkeypatch.setattr(mod.State, "save", lambda self, d: None)
    state = _state({}, {"high": ProviderModel("claude", "opus")})
    quest = SimpleNamespace(workflow_dir="wf", state_dir="sd")

    apply_model_override(
        quest, state, f"{provider}:gpt-6-astra", step_name="brainstorm")

    assert state.resolved_steps["brainstorm"] == ProviderModel(
        provider, "gpt-6-astra")
