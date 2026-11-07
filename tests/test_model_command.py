from types import SimpleNamespace

import pytest

from imp.cli.model_command import render_roster, validate_model_arg
from imp.providers.claude_model_cache import ClaudeModelOption
from imp.providers.config import ProviderModel
from imp.providers.model_roster import ProviderRoster, discover_roster, group_models


def _roster(provider: str, *names: str) -> ProviderRoster:
    return ProviderRoster(provider, group_models(provider, names))


def _discovered(monkeypatch, *providers: str, cached: tuple[str, ...] = ()) -> list[ProviderRoster]:
    monkeypatch.setattr(
        "imp.providers.model_roster.available_providers", lambda config: list(providers))
    return discover_roster(
        SimpleNamespace(tiers={}, providers=()),
        claude_options=[ClaudeModelOption(value=name) for name in cached])


def _section(lines: list[str], provider: str) -> list[str]:
    start = lines.index(f"  {provider}")
    end = start + 1
    while lines[end].startswith("    "):
        end += 1
    return lines[start + 1:end]


def _marked(lines: list[str]) -> list[str]:
    return [line for line in lines if line.endswith("   current")]


_OPENAI_LAYOUT = [
    "    Astra",
    "      gpt-6-astra",
    "    Sol",
    "      gpt-6.1-sol",
    "      gpt-5.6-sol",
    "    Terra",
    "      gpt-6.1-terra",
    "      gpt-5.6-terra",
    "    Luna",
    "      gpt-6-luna",
    "      gpt-5.6-luna",
    "    GPT-5",
    "      gpt-5.5",
    "      gpt-5.4",
    "      gpt-5.4-mini",
    "      gpt-5.3-codex",
    "      gpt-5",
]


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
    current = ProviderModel("claude", "claude-opus-4-8[1m]")
    rosters = [
        _roster("claude", "claude-opus-4-8", "opus", "claude-fable-5[1m]"),
        _roster("anthropic", "claude-sonnet-4-6"),
    ]
    lines = render_roster(current, rosters)
    assert "Current: claude · claude-opus-4-8[1m]   (this step)" in lines
    assert "  claude" in lines
    assert "  anthropic" in lines
    # one model per line, bare name, 6-space indent
    assert "      claude-opus-4-8" in lines
    assert "      claude-fable-5[1m]" in lines
    assert "      claude-sonnet-4-6" in lines
    # the bare alias is hidden, but a dated id merely CONTAINING "opus" is kept
    assert "      opus" not in lines
    # no `·`-joined model line and no description side-notes (the Current line excepted)
    body = [ln for ln in lines if not ln.startswith("Current:")]
    assert all(" · " not in ln for ln in body)
    assert any("Switch with" in line for line in lines)


def test_print_model_roster_threads_cache_and_current(monkeypatch, tmp_path) -> None:
    import imp.cli.model_command as mod

    state = SimpleNamespace(
        step="brainstorm", resolved_steps=None, resolved_tiers=None)
    monkeypatch.setattr("imp.engine.state.State.load", lambda state_dir: state)
    monkeypatch.setattr(
        "imp.providers.effective.load_effective_config",
        lambda **kw: SimpleNamespace(
            config=SimpleNamespace(tiers={}, providers=())))
    monkeypatch.setattr("imp.engine.loader.load_workflow", lambda *a, **k: object())
    monkeypatch.setattr(
        "imp.engine.model_override.resolve_current_model",
        lambda state, wf, cfg, step: ProviderModel("claude", "opus"))
    monkeypatch.setattr(
        "imp.providers.claude_model_cache.read_claude_model_options",
        lambda: ["SENTINEL-CACHE"])

    captured: dict[str, object] = {}

    def _discover(config, *, claude_options):
        captured["claude_options"] = claude_options
        return [_roster("claude", "claude-opus-4-8")]

    monkeypatch.setattr("imp.providers.model_roster.discover_roster", _discover)

    quest = SimpleNamespace(
        state_dir=tmp_path, project_root=tmp_path,
        control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    mod.print_model_roster(quest=quest, output_fn=out.append)

    # the best-effort cache read is threaded into discovery
    assert captured["claude_options"] == ["SENTINEL-CACHE"]
    # the resolved current step's model is marked current
    assert any("Current: claude · opus" in line for line in out)
    assert any("  claude" in line for line in out)


def test_validate_model_available_rejects_unavailable_provider() -> None:
    from imp.cli.model_command import validate_model_available

    reason = validate_model_available(
        "openai:gpt-5.5", available=["anthropic", "claude"])
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


def test_render_roster_hides_the_bare_gpt_5_6_alias() -> None:
    from imp.providers.catalog import catalog_aliases

    # The bare alias is a catalog key (so limits resolution can cap it) but must
    # never be suggested: an operator writing it into a `tiers:` block would get
    # the flagship at every tier.
    assert "gpt-5.6" in catalog_aliases("openai")
    current = ProviderModel("openai", "gpt-5.6-sol")
    rosters = [_roster("openai", *catalog_aliases("openai"))]
    lines = render_roster(current, rosters)
    assert "      gpt-5.6" not in lines
    assert "      gpt-5.6-sol   current" in lines
    assert "      gpt-5.6-terra" in lines
    assert "      gpt-5" in lines


def test_roster_model_names_flattens_dedups_and_keeps_aliases(
    monkeypatch, tmp_path,
) -> None:
    import imp.cli.model_command as mod

    monkeypatch.setattr(
        "imp.providers.effective.load_effective_config",
        lambda **kw: SimpleNamespace(config=SimpleNamespace(tiers={}, providers=())))
    monkeypatch.setattr(
        "imp.providers.claude_model_cache.read_claude_model_options",
        lambda: [])

    def _discover(config, *, claude_options):
        return [
            _roster("claude", "claude-opus-4-8", "opus", "claude-fable-5[1m]"),
            _roster("anthropic", "claude-sonnet-4-6", "opus"),
        ]

    monkeypatch.setattr("imp.providers.model_roster.discover_roster", _discover)

    quest = SimpleNamespace(project_root=tmp_path, control_root=tmp_path)
    names = mod.roster_model_names(quest)

    # flattened across providers in each roster's group order, dedup drops the
    # second "opus", and the bare alias "opus" is KEPT (no display filter in discovery)
    assert names == [
        "claude-fable-5[1m]", "claude-opus-4-8", "opus", "claude-sonnet-4-6"]


def test_claude_catalog_renders_the_documented_layout(monkeypatch) -> None:
    rosters = _discovered(monkeypatch, "claude", cached=("claude-fable-5-1[1m]",))
    assert render_roster(ProviderModel("claude", "claude-opus-5-5"), rosters) == [
        "Current: claude · claude-opus-5-5   (this step)",
        "",
        "Available:",
        "  claude",
        "    Fable",
        "      claude-fable-5-1[1m]",
        "      claude-fable-5",
        "      claude-fable-5[1m]",
        "    Opus",
        "      claude-opus-5-5   current",
        "      claude-opus-5-5[1m]",
        "      claude-opus-5",
        "      claude-opus-5[1m]",
        "      claude-opus-4-8",
        "      claude-opus-4-8[1m]",
        "    Sonnet",
        "      claude-sonnet-5-5",
        "      claude-sonnet-5-5[1m]",
        "      claude-sonnet-4-6",
        "    Haiku",
        "      claude-haiku-5-5",
        "      claude-haiku-4-5",
        "",
        "Switch with  /model <name>  or  /model provider:model",
    ]


def test_openai_catalog_renders_the_documented_layout_unmarked(monkeypatch) -> None:
    rosters = _discovered(monkeypatch, "openai", "codex")
    lines = render_roster(ProviderModel("claude", "claude-opus-5-5"), rosters)
    assert _section(lines, "openai") == _OPENAI_LAYOUT
    assert _section(lines, "codex") == _OPENAI_LAYOUT
    assert _marked(lines) == []


def test_only_the_row_under_the_current_provider_is_marked() -> None:
    rosters = [_roster("claude", "claude-opus-5-5"), _roster("anthropic", "claude-opus-5-5")]
    lines = render_roster(ProviderModel("claude", "claude-opus-5-5"), rosters)
    assert _section(lines, "claude") == ["    Opus", "      claude-opus-5-5   current"]
    assert _section(lines, "anthropic") == ["    Opus", "      claude-opus-5-5"]


def test_a_current_hidden_alias_is_revealed_last_in_its_group(monkeypatch) -> None:
    rosters = _discovered(monkeypatch, "claude")
    section = _section(render_roster(ProviderModel("claude", "opus"), rosters), "claude")
    assert section[section.index("    Sonnet") - 1] == "      opus   current"
    assert section[section.index("    Sonnet") - 2] == "      claude-opus-4-8[1m]"


def test_a_current_unrecognised_model_gets_a_row_under_other(monkeypatch) -> None:
    rosters = _discovered(monkeypatch, "claude")
    section = _section(render_roster(ProviderModel("claude", "foo"), rosters), "claude")
    assert section[-2:] == ["    Other", "      foo   current"]


def test_no_extra_row_or_other_heading_for_any_other_current_model(monkeypatch) -> None:
    rosters = _discovered(monkeypatch, "claude")
    lines = render_roster(ProviderModel("claude", "claude-opus-5"), rosters)
    assert "    Other" not in lines
    assert not [ln for ln in lines if ln.split()[:1] in (["opus"], ["foo"])]


@pytest.mark.parametrize(("first", "second", "model", "group"), [
    ("claude", "anthropic", "opus", "    Opus"),
    ("openai", "codex", "gpt-5.6", "    GPT-5"),
])
def test_a_hidden_alias_is_revealed_only_under_the_current_provider(
    first: str, second: str, model: str, group: str,
) -> None:
    rosters = [_roster(first, model), _roster(second, model)]
    lines = render_roster(ProviderModel(first, model), rosters)
    assert _section(lines, first) == [group, f"      {model}   current"]
    assert _section(lines, second) == []


def test_a_current_unlisted_model_gets_no_row_under_another_provider() -> None:
    rosters = [_roster("claude", "claude-opus-5"), _roster("anthropic", "claude-opus-5")]
    lines = render_roster(ProviderModel("claude", "foo"), rosters)
    assert _section(lines, "anthropic") == ["    Opus", "      claude-opus-5"]


def test_current_gpt_5_6_shows_above_gpt_5_5(monkeypatch) -> None:
    rosters = _discovered(monkeypatch, "openai")
    section = _section(render_roster(ProviderModel("openai", "gpt-5.6"), rosters), "openai")
    start = section.index("    GPT-5")
    assert section[start + 1:start + 3] == ["      gpt-5.6   current", "      gpt-5.5"]


def test_a_current_provider_absent_from_the_rosters_marks_nothing() -> None:
    rosters = [_roster("anthropic", "claude-opus-5")]
    lines = render_roster(ProviderModel("claude", "claude-opus-5"), rosters)
    assert _marked(lines) == []
    assert lines[0] == "Current: claude · claude-opus-5   (this step)"


def test_a_provider_with_no_family_table_renders_under_other() -> None:
    lines = render_roster(ProviderModel("claude", "x"), [_roster("scripted", "b", "a")])
    assert _section(lines, "scripted") == ["    Other", "      a", "      b"]


def test_a_group_of_only_hidden_names_has_no_heading() -> None:
    lines = render_roster(
        ProviderModel("claude", "claude-fable-5"),
        [_roster("claude", "opus", "claude-fable-5")])
    assert _section(lines, "claude") == ["    Fable", "      claude-fable-5   current"]


def _completion_names(monkeypatch, tmp_path, *providers: str) -> list[str]:
    import imp.cli.model_command as mod

    monkeypatch.setattr(
        "imp.providers.model_roster.available_providers", lambda config: list(providers))
    monkeypatch.setattr(
        "imp.providers.effective.load_effective_config",
        lambda **kw: SimpleNamespace(config=SimpleNamespace(tiers={}, providers=())))
    monkeypatch.setattr(
        "imp.providers.claude_model_cache.read_claude_model_options", lambda: [])
    return mod.roster_model_names(SimpleNamespace(project_root=tmp_path, control_root=tmp_path))


def test_completion_follows_the_new_order_with_aliases_on_claude(monkeypatch, tmp_path) -> None:
    assert _completion_names(monkeypatch, tmp_path, "claude") == [
        "claude-fable-5", "claude-fable-5[1m]",
        "claude-opus-5-5", "claude-opus-5-5[1m]", "claude-opus-5", "claude-opus-5[1m]",
        "claude-opus-4-8", "claude-opus-4-8[1m]", "opus",
        "claude-sonnet-5-5", "claude-sonnet-5-5[1m]", "claude-sonnet-4-6", "sonnet",
        "claude-haiku-5-5", "claude-haiku-4-5", "haiku",
    ]


def test_completion_lists_gpt_5_6_above_gpt_5_5(monkeypatch, tmp_path) -> None:
    names = _completion_names(monkeypatch, tmp_path, "openai")
    assert names[names.index("gpt-5.6") + 1] == "gpt-5.5"


def test_completion_has_no_duplicates_across_providers(monkeypatch, tmp_path) -> None:
    names = _completion_names(monkeypatch, tmp_path, "claude", "anthropic", "openai", "codex")
    assert len(names) == len(set(names))
    assert {"opus", "sonnet", "haiku", "gpt-5.6"} <= set(names)


def test_completion_gains_no_name_from_the_current_model(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "imp.engine.model_override.resolve_current_model",
        lambda state, wf, cfg, step: ProviderModel("claude", "foo"))
    assert "foo" not in _completion_names(monkeypatch, tmp_path, "claude")
