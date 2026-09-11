"""The shared current-and-roster derivation behind `p` and both `/provider`
worlds (Scope 3; AC 18a)."""
from __future__ import annotations

from pathlib import Path

from imp.cli.provider_command import (
    current_provider_for,
    picker_choices,
    render_picker,
    render_provider_roster,
    selectable_available,
)
from imp.providers.config import ProviderModel
from tests.test_provider_reconcile import _seed, _tiers

_PICKER = """\
switch provider — the rest of this quest runs on your pick
takes effect right away — this step is waiting to retry

  1  claude_code   current
  2  codex

press a number, or esc to cancel"""

_ROSTER = """\
Current: claude_code   (this quest)

Available:
  claude_code
  codex

Switch with  /provider <name> — the rest of this quest runs on it."""


# AC 8
def test_the_picker_block_is_verbatim() -> None:
    # Line 2 of the head must stay true for every backoff category the gate
    # admits — auth, network, rate_limit and stalled — so it names none of them.
    assert "\n".join(
        render_picker("claude_code", ["claude_code", "codex"])) == _PICKER


def test_the_roster_block_is_verbatim() -> None:
    assert "\n".join(
        render_provider_roster("claude_code", ["claude_code", "codex"])) == _ROSTER


def test_only_the_current_vendor_is_tagged() -> None:
    lines = render_picker("codex", ["claude_code", "codex"])
    assert lines[3:5] == ["  1  claude_code", "  2  codex   current"]


def test_the_roster_drops_the_current_line_when_nothing_resolves_yet() -> None:
    # A configured order whose vendors are all unavailable resolves to nothing,
    # while the pin-reachable roster is still non-empty.
    assert render_provider_roster(None, ["codex"])[0] == "Available:"


def test_the_current_vendors_slot_carries_no_name() -> None:
    assert picker_choices("claude_code", ["claude_code", "codex"]) == [
        None, "codex"]


def test_the_roster_is_the_selectable_set_filtered_by_env_check(
    floor_availability,
) -> None:
    floor_availability({"claude_code", "anthropic"})
    # scripted has no tier table, so it is never selectable however available.
    assert selectable_available() == ["anthropic", "claude_code"]
    floor_availability({"codex"})
    assert selectable_available() == ["codex"]


def test_current_is_the_pin_when_pinned(tmp_path: Path, floor_availability) -> None:
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude_code", "codex"})
    quest.set_provider("codex")
    assert current_provider_for(quest) == "codex"


def test_current_falls_back_to_the_resolved_map_not_a_step_override(
    tmp_path: Path, floor_availability,
) -> None:
    # AC 8e: a cross-vendor /model override on the current step must not be what
    # the guard compares, or a real switch reads as redundant and vice versa.
    quest, state, _wf = _seed(tmp_path)
    floor_availability({"claude_code", "codex", "openai"})
    state.resolved_tiers = _tiers("claude_code")
    state.resolved_steps = {"w": ProviderModel(provider="openai",
                                               model="gpt-5.6-terra")}
    state.step = "w"
    state.save(quest.state_dir)
    assert current_provider_for(quest) == "claude_code"


def test_current_reads_the_config_when_there_is_no_state(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude_code", "codex"})
    (quest.state_dir / "state.json").unlink()
    assert current_provider_for(quest) == "claude_code"


def test_current_reflects_pin_written_by_another_process(
    tmp_path: Path, floor_availability,
) -> None:
    # Guards the arm→reconcile window: the daemon writes the pin OOP while the
    # client still holds the original in-memory Quest.
    quest, state, _wf = _seed(tmp_path)
    floor_availability({"claude_code", "codex"})
    # state already resolved to claude_code — ensures the reload, not the
    # fallback, is what surfaces "codex".
    state.resolved_tiers = _tiers("claude_code")
    state.save(quest.state_dir)
    # Simulate out-of-process write by patching quest.json directly (as the
    # daemon's set_provider callback would do from another process).
    import json
    meta_path = quest.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["provider"] = "codex"
    meta_path.write_text(json.dumps(meta))
    # The first instance's in-memory pin is still None (pre-arm).
    assert quest.provider is None
    assert current_provider_for(quest) == "codex"
