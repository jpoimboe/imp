from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imp.engine import executor as executor_mod
from imp.engine.errors import LoadError
from imp.engine.executor import _reconcile_provider, _refresh_workflow_snapshot
from imp.engine.loader import load_workflow
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.providers import BUILTIN_FLOOR
from imp.providers.config import ProviderModel
from tests.test_executor import _seed_minimal_workflow


def _tiers(provider: str) -> dict[str, ProviderModel]:
    return {t: ProviderModel(provider=provider, model=m)
            for t, m in BUILTIN_FLOOR[provider].tier_defaults.items()}


def _seed(tmp_path: Path) -> tuple[Quest, State, Any]:
    """A quest already resolved onto claude, as a stopped run left it."""
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    (project_state_dir(tmp_path) / "config.yaml").write_text(
        "providers: [claude]\n")
    _refresh_workflow_snapshot(quest)
    workflow = load_workflow(quest.workflow_dir)
    state = State.load(quest.state_dir)
    state.resolved_tiers = _tiers("claude")
    state.resolved_steps = {}
    state.save(quest.state_dir)
    return quest, state, workflow


def test_an_unpinned_quest_reconciles_to_false_and_touches_nothing(
    tmp_path: Path, floor_availability,
) -> None:
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    events: list[dict[str, Any]] = []

    assert _reconcile_provider(
        quest, state, workflow, events.append) is False
    assert {pm.provider for pm in state.resolved_tiers.values()} == {"claude"}
    assert events == []


def test_a_pending_switch_repoints_every_tier_off_the_pinned_floor(
    tmp_path: Path, floor_availability,
) -> None:
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    quest.set_provider("codex")
    events: list[dict[str, Any]] = []

    assert _reconcile_provider(
        quest, state, workflow, events.append) is True

    assert {t: (pm.provider, pm.model) for t, pm in state.resolved_tiers.items()} == {
        t: ("codex", model)
        for t, model in BUILTIN_FLOOR["codex"].tier_defaults.items()}
    # Persisted onto the live State, so the running dispatcher reads it.
    assert {pm.provider
            for pm in State.load(quest.state_dir).resolved_tiers.values()} == {"codex"}


def test_the_re_resolve_discards_a_cross_vendor_step_override(
    tmp_path: Path, floor_availability,
) -> None:
    # An override names a model on the vendor being fled — exactly what the
    # switch exists to escape — so replacement is wholesale, never a merge.
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    state.resolved_steps = {"w": ProviderModel(provider="openai", model="gpt-6.1-sol")}
    quest.set_provider("codex")

    assert _reconcile_provider(
        quest, state, workflow, lambda _e: None) is True
    assert "w" not in state.resolved_steps


def test_reconciling_twice_is_a_no_op_the_second_time(
    tmp_path: Path, floor_availability,
) -> None:
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    quest.set_provider("codex")
    assert _reconcile_provider(quest, state, workflow, lambda _e: None) is True
    events: list[dict[str, Any]] = []
    assert _reconcile_provider(quest, state, workflow, events.append) is False
    assert events == []


def test_the_confirmation_names_the_vendor_and_the_disposition(
    tmp_path: Path, floor_availability,
) -> None:
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    quest.set_provider("codex")
    events: list[dict[str, Any]] = []

    _reconcile_provider(quest, state, workflow, events.append)

    assert events == [
        {"type": "provider_marker", "payload": {"provider": None}},
        {"type": "script_output", "payload": {
            "stdout": "provider switched — codex picks up this step from the event log"}}]


def test_step_ends_selects_the_next_step_confirmation(
    tmp_path: Path, floor_availability,
) -> None:
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    quest.set_provider("codex")
    events: list[dict[str, Any]] = []

    _reconcile_provider(quest, state, workflow, events.append, step_ends=True)

    assert events[0] == {"type": "provider_marker", "payload": {"provider": None}}
    assert events[1]["payload"]["stdout"] == (
        "provider switched — codex takes the next step")


def _raise_load_error(monkeypatch) -> None:
    """Inject the LoadError arm. It cannot be reached through config: a custom
    tier name is rejected at parse time (`_REQUIRED_TIERS` at
    `src/imp/providers/config.py:43`), and for the three real tiers `_floor_layer`
    deep-merges every vendor's `tier_defaults` into the bottom layer
    (`src/imp/providers/effective.py:58-74`), so `config.tiers[t]` always contains
    the pinned vendor and `resolve_run`'s raise at
    `src/imp/providers/resolve.py:186` never fires. Injecting keeps the pinned
    copy line honest while pinning the handler, which is the behaviour under test."""
    def _boom(_workflow, _config):
        raise LoadError("tier 'deep' has no model configured for provider 'codex'")

    monkeypatch.setattr(executor_mod, "resolve_run", _boom)


def test_a_raising_re_resolve_refuses_the_switch_and_keeps_the_run(
    tmp_path: Path, floor_availability, monkeypatch,
) -> None:
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    quest.set_provider("codex")
    _raise_load_error(monkeypatch)
    events: list[dict[str, Any]] = []

    assert _reconcile_provider(
        quest, state, workflow, events.append) is False

    assert {pm.provider for pm in state.resolved_tiers.values()} == {"claude"}
    assert events == [
        {"type": "provider_marker", "payload": {"provider": None}},
        {"type": "script_output", "payload": {"stdout": (
            "couldn't switch to codex — staying on claude: "
            "tier 'deep' has no model configured for provider 'codex'")}}]


def test_a_refused_switch_clears_the_pin_so_it_cannot_wedge_a_resume(
    tmp_path: Path, floor_availability, monkeypatch,
) -> None:
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude", "codex"})
    quest.set_provider("codex")
    _raise_load_error(monkeypatch)

    _reconcile_provider(quest, state, workflow, lambda _e: None)

    assert quest.provider is None
    # Read quest.json directly: _seed_minimal_workflow writes it without a
    # layout stamp, so Quest.load would raise LayoutMismatch on this fixture.
    assert json.loads(
        (quest.quest_dir / "quest.json").read_text())["provider"] is None


def test_an_unavailable_pinned_vendor_is_refused_the_same_way(
    tmp_path: Path, floor_availability,
) -> None:
    # The engine process's environment can differ from the client's that
    # validated availability; ProviderUnavailable must not kill the run either.
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude"})           # codex not available here
    quest.set_provider("codex")
    events: list[dict[str, Any]] = []

    assert _reconcile_provider(
        quest, state, workflow, events.append) is False
    assert {pm.provider for pm in state.resolved_tiers.values()} == {"claude"}
    assert quest.provider is None
    assert events[0] == {"type": "provider_marker", "payload": {"provider": None}}
    assert events[1]["payload"]["stdout"].startswith(
        "couldn't switch to codex — staying on claude: ")
