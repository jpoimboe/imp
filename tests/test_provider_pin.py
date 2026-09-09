from __future__ import annotations

from pathlib import Path

from imp.engine.provider_pin import current_provider, provider_pending
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.providers.config import ProviderModel
from tests.conftest import _seed_control


def _tiers(provider: str) -> dict[str, ProviderModel]:
    return {t: ProviderModel(provider=provider, model="m")
            for t in ("low", "medium", "high")}


def _quest(tmp_path: Path, *, provider: str | None = None) -> Quest:
    _seed_control(tmp_path)
    return Quest.create_draft(
        tmp_path, title="x", quest_id="pin", base_branch="main",
        provider=provider)


def test_the_pin_is_current_even_before_it_is_applied(tmp_path: Path) -> None:
    # The map still names the vendor being fled; the pin is what the user chose,
    # so a second request naming it must read as redundant.
    quest = _quest(tmp_path, provider="codex")
    state = State.load(quest.state_dir)
    state.resolved_tiers = _tiers("claude_code")

    assert current_provider(quest, state) == "codex"


def test_an_unpinned_quest_is_current_on_its_resolved_map(tmp_path: Path) -> None:
    quest = _quest(tmp_path)
    state = State.load(quest.state_dir)
    state.resolved_tiers = _tiers("claude_code")

    assert current_provider(quest, state) == "claude_code"


def test_a_per_step_override_never_becomes_current(tmp_path: Path) -> None:
    # /provider's subject is the quest, not the step: a cross-vendor /model
    # override must not be read as current, or the same-name guard inverts.
    quest = _quest(tmp_path)
    state = State.load(quest.state_dir)
    state.resolved_tiers = _tiers("claude_code")
    state.resolved_steps = {"w": ProviderModel(provider="openai", model="gpt")}

    assert current_provider(quest, state) == "claude_code"


def test_with_no_state_current_is_what_the_config_would_pick(
    tmp_path: Path, floor_availability,
) -> None:
    # Pre-session there is no State at all; _seed_control configures anthropic.
    quest = _quest(tmp_path)
    floor_availability({"anthropic", "codex"})

    assert current_provider(quest, None) == "anthropic"


def test_a_pre_resolve_state_falls_back_to_the_config(
    tmp_path: Path, floor_availability,
) -> None:
    quest = _quest(tmp_path)
    state = State.load(quest.state_dir)
    assert state.resolved_tiers is None
    floor_availability({"anthropic", "codex"})

    assert current_provider(quest, state) == "anthropic"


def test_an_unpinned_quest_is_never_pending(tmp_path: Path) -> None:
    # A bare inequality against None would make every unpinned quest pending and
    # wake every backoff on its first tick.
    quest = _quest(tmp_path)
    state = State.load(quest.state_dir)
    state.resolved_tiers = _tiers("claude_code")

    assert provider_pending(quest, state) is False


def test_a_pin_the_map_does_not_name_is_pending(tmp_path: Path) -> None:
    quest = _quest(tmp_path, provider="codex")
    state = State.load(quest.state_dir)
    state.resolved_tiers = _tiers("claude_code")

    assert provider_pending(quest, state) is True


def test_a_pin_the_map_already_names_is_not_pending(tmp_path: Path) -> None:
    quest = _quest(tmp_path, provider="codex")
    state = State.load(quest.state_dir)
    state.resolved_tiers = _tiers("codex")

    assert provider_pending(quest, state) is False


def test_a_partly_repointed_map_is_still_pending(tmp_path: Path) -> None:
    # Any tier still on the old vendor means the switch has not landed.
    quest = _quest(tmp_path, provider="codex")
    state = State.load(quest.state_dir)
    state.resolved_tiers = _tiers("codex")
    state.resolved_tiers["high"] = ProviderModel(provider="claude_code", model="m")

    assert provider_pending(quest, state) is True


def test_nothing_is_pending_before_the_first_resolve(tmp_path: Path) -> None:
    quest = _quest(tmp_path, provider="codex")
    state = State.load(quest.state_dir)
    assert state.resolved_tiers is None

    assert provider_pending(quest, state) is False
