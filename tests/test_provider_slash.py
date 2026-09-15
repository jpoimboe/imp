"""`/provider` in both of handle_slash_line's worlds (Scope 3; AC 8d, 8e, 18d,
21, 21b)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.cli.slash import (
    PROVIDER_ALREADY,
    PROVIDER_PINNED,
    REPROMPT,
    handle_slash_line,
)
from imp.providers.config import ProviderModel
from tests.test_provider_reconcile import _seed, _tiers


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _pin_on_disk(quest) -> str | None:
    # .get: the minimal fixture writes quest.json without the field until a
    # surface sets it, which is exactly what "wrote no pin" looks like.
    return json.loads(
        (quest.quest_dir / "quest.json").read_text()).get("provider")


def test_in_session_a_valid_name_returns_the_control_marker(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    out: list[str] = []

    result = handle_slash_line("/provider codex", quest=quest,
                               output_fn=out.append)

    assert result == "/provider codex"
    # The client sends the frame; a direct write here would go behind the
    # running engine's back.
    assert _pin_on_disk(quest) is None
    assert out == []


def test_pre_session_writes_the_pin_and_reprompts(
    tmp_path: Path, floor_availability,
) -> None:
    # AC 21b(a): the same end state as `imp new --provider codex`.
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    out: list[str] = []

    result = handle_slash_line("/provider codex", quest=quest,
                               output_fn=out.append, pre_session=True)

    assert result is REPROMPT
    assert _pin_on_disk(quest) == "codex"
    assert quest.provider == "codex"
    assert out[-1] == PROVIDER_PINNED.format(name="codex")
    assert not any("Unknown command" in line for line in out)


def test_bare_provider_prints_the_roster_and_switches_nothing(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    out: list[str] = []

    assert handle_slash_line("/provider", quest=quest,
                             output_fn=out.append) is REPROMPT
    assert "Current: claude   (this quest)" in out
    assert "  codex" in out
    assert _pin_on_disk(quest) is None


def test_bare_provider_pre_session_needs_no_state(
    tmp_path: Path, floor_availability,
) -> None:
    # AC 21b(b): print_model_roster asserts state.step is not None; this must not.
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    (quest.state_dir / "state.json").unlink()
    out: list[str] = []

    assert handle_slash_line("/provider", quest=quest, output_fn=out.append,
                             pre_session=True) is REPROMPT
    assert "Current: claude   (this quest)" in out


def test_an_unknown_name_prints_the_flag_error_without_its_prefix(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    out: list[str] = []

    assert handle_slash_line("/provider bogus", quest=quest,
                             output_fn=out.append) is REPROMPT
    assert out[-1] == ("unknown provider 'bogus' (available here: claude, "
                       "codex; needs setup: anthropic, openai)")


def test_an_unavailable_name_is_refused_with_its_auth_hint(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude"})
    out: list[str] = []

    assert handle_slash_line("/provider codex", quest=quest,
                             output_fn=out.append) is REPROMPT
    assert out[-1] == "codex is not available — run `codex login`"


def test_the_current_vendor_is_refused_as_redundant(
    tmp_path: Path, floor_availability,
) -> None:
    # AC 8d: no pin written, and no marker to leave showing.
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    out: list[str] = []

    assert handle_slash_line("/provider claude", quest=quest,
                             output_fn=out.append) is REPROMPT
    assert out[-1] == PROVIDER_ALREADY.format(name="claude")
    assert _pin_on_disk(quest) is None


def test_the_guard_compares_the_tier_map_not_a_step_override(
    tmp_path: Path, floor_availability,
) -> None:
    # AC 8e: on a step whose /model override names openai, `openai` is a real
    # switch and `claude_code` is the redundant one. A guard read off
    # resolve_current_model inverts both.
    quest, state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex", "openai"})
    state.resolved_tiers = _tiers("claude")
    state.resolved_steps = {"w": ProviderModel(provider="openai",
                                               model="gpt-5.6-terra")}
    state.step = "w"
    state.save(quest.state_dir)
    out: list[str] = []

    assert handle_slash_line("/provider openai", quest=quest,
                             output_fn=out.append) == "/provider openai"
    assert handle_slash_line("/provider claude", quest=quest,
                             output_fn=out.append) is REPROMPT
    assert out[-1] == PROVIDER_ALREADY.format(name="claude")


def test_pre_session_re_pinning_the_pinned_vendor_is_redundant(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    quest.set_provider("codex")
    out: list[str] = []

    assert handle_slash_line("/provider codex", quest=quest,
                             output_fn=out.append, pre_session=True) is REPROMPT
    assert out[-1] == PROVIDER_ALREADY.format(name="codex")


def test_help_advertises_provider_at_the_pinned_column() -> None:
    from imp.cli.slash import _help
    out: list[str] = []
    _help(out.append, materialized=False, pre_session=True)
    assert "  /provider [name]   Show or switch the provider for this quest" in out


def test_provider_is_tab_completable() -> None:
    from imp.cli.prompt import SlashCompleter
    assert "provider" in SlashCompleter._BUILTINS


def test_the_refusal_copy_is_verbatim() -> None:
    assert PROVIDER_ALREADY.format(name="codex") == (
        "Already on codex — nothing changed.")
    assert PROVIDER_PINNED.format(name="codex") == (
        "This quest will run on codex.")


@pytest.mark.anyio
async def test_pre_session_provider_resolves_the_quest_onto_that_vendor(
    tmp_path: Path, floor_availability,
) -> None:
    # AC 21b(a) end to end: /provider codex at the opening prompt reaches the
    # same resolved state as `imp new --provider codex`, because both write the
    # one field the narrowing derives from.
    from imp.engine.executor import RunResult, run_quest
    from imp.providers import BUILTIN_FLOOR
    from tests.test_executor import _seed_minimal_workflow
    from tests.test_executor_provider_pin import (
        _floor_config,
        _ok_factory,
        _reopen,
    )

    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)                       # providers: [claude_code]
    floor_availability({"claude", "codex"}, factory=_ok_factory)

    assert handle_slash_line("/provider codex", quest=quest,
                             output_fn=lambda _l: None,
                             pre_session=True) is REPROMPT
    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED

    tiers = _reopen(quest).resolved_tiers
    assert {t: (pm.provider, pm.model) for t, pm in tiers.items()} == {
        t: ("codex", model)
        for t, model in BUILTIN_FLOOR["codex"].tier_defaults.items()}
