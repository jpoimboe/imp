"""The shared current-and-roster derivation behind `p` and both `/provider`
worlds (Scope 3; AC 18a)."""
from __future__ import annotations

from pathlib import Path

import imp.cli.provider_command as provider_command
from imp.cli.provider_command import (
    ROSTER_FOOT,
    current_provider_for,
    picker_choices,
    quest_provider_groups,
    render_picker,
    render_provider_roster,
)
from imp.providers import ProviderGroups
from imp.providers.config import ProviderModel
from tests.test_provider_reconcile import _seed, _tiers

_ROOT = Path(__file__).resolve().parents[1]

_GROUPS = ProviderGroups(configured=("claude",), also_installed=("codex",),
                         needs_setup=("anthropic", "openai"))

_PICKER = """\
switch provider — the rest of this quest runs on your pick
takes effect right away — this step is waiting to retry

configured
  1  claude   current
also installed
  2  codex

press a number, or esc to cancel"""

_ROSTER = """\
Current: claude (this quest)

Configured:
  claude
Also installed:
  codex

Switch with /provider <name> — the rest of this quest runs on it."""


def test_the_picker_block_is_verbatim() -> None:
    # Line 2 of the head must stay true for every backoff category the gate
    # admits — auth, network, rate_limit and stalled — so it names none of them.
    assert "\n".join(render_picker("claude", _GROUPS)) == _PICKER


def test_the_roster_block_is_verbatim() -> None:
    assert "\n".join(render_provider_roster("claude", _GROUPS)) == _ROSTER


def test_only_the_current_vendor_is_tagged() -> None:
    lines = render_picker("codex", _GROUPS)
    assert lines[3:7] == ["configured", "  1  claude",
                          "also installed", "  2  codex   current"]


def test_the_roster_keeps_config_order_and_prints_no_empty_header() -> None:
    lines = render_provider_roster(
        None, ProviderGroups(("codex", "claude"), (), ()))
    assert lines == ["Configured:", "  codex", "  claude", "", ROSTER_FOOT]


def test_the_roster_drops_the_current_line_and_an_empty_configured_group() -> None:
    # A configured order whose vendors are all unavailable resolves to nothing,
    # while the pin-reachable roster is still non-empty.
    lines = render_provider_roster(
        None, ProviderGroups((), ("codex",), ("anthropic", "openai", "claude")))
    assert lines == ["Also installed:", "  codex", "", ROSTER_FOOT]


def test_the_current_vendors_slot_carries_no_name() -> None:
    assert picker_choices("claude", _GROUPS) == [None, "codex"]
    assert picker_choices("codex", _GROUPS) == ["claude", None]


def test_digits_follow_the_displayed_order_across_groups() -> None:
    groups = ProviderGroups(("codex",), ("anthropic", "claude"), ())
    rows = [line for line in render_picker(None, groups) if line.startswith("  ")]
    assert rows == ["  1  codex", "  2  anthropic", "  3  claude"]
    assert picker_choices(None, groups) == ["codex", "anthropic", "claude"]


def test_the_quests_groups_come_from_its_effective_config(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _state, _wf = _seed(tmp_path)        # configures providers: [claude]
    floor_availability({"claude", "codex"})
    assert quest_provider_groups(quest) == _GROUPS


def test_the_flat_roster_helper_is_gone() -> None:
    assert not hasattr(provider_command, "selectable_available")


def test_no_arming_surface_reads_availability_on_its_own() -> None:
    for path in ("src/imp/cli/provider_command.py", "src/imp/cli/session_run.py",
                 "src/imp/cli/slash.py"):
        assert "provider_is_available" not in (_ROOT / path).read_text(), path


def test_current_is_the_pin_when_pinned(tmp_path: Path, floor_availability) -> None:
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    quest.set_provider("codex")
    assert current_provider_for(quest) == "codex"


def test_current_falls_back_to_the_resolved_map_not_a_step_override(
    tmp_path: Path, floor_availability,
) -> None:
    # AC 8e: a cross-vendor /model override on the current step must not be what
    # the guard compares, or a real switch reads as redundant and vice versa.
    quest, state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex", "openai"})
    state.resolved_tiers = _tiers("claude")
    state.resolved_steps = {"w": ProviderModel(provider="openai",
                                               model="gpt-6.1-terra")}
    state.step = "w"
    state.save(quest.state_dir)
    assert current_provider_for(quest) == "claude"


def test_current_reads_the_config_when_there_is_no_state(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    (quest.state_dir / "state.json").unlink()
    assert current_provider_for(quest) == "claude"


def test_current_reflects_pin_written_by_another_process(
    tmp_path: Path, floor_availability,
) -> None:
    # Guards the arm→reconcile window: the daemon writes the pin OOP while the
    # client still holds the original in-memory Quest.
    quest, state, _wf = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    # state already resolved to claude — ensures the reload, not the
    # fallback, is what surfaces "codex".
    state.resolved_tiers = _tiers("claude")
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
