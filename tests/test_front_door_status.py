from __future__ import annotations

from imp.agents import prompts
from imp.agents.front_door_status import (
    FRONT_DOOR_STATUS_PHRASES,
    front_door_status_phrase,
)


def test_pool_is_108_unique_in_character_phrases() -> None:
    assert len(FRONT_DOOR_STATUS_PHRASES) == 108
    assert len(set(FRONT_DOOR_STATUS_PHRASES)) == 108
    assert FRONT_DOOR_STATUS_PHRASES[0] == "scheming"
    assert FRONT_DOOR_STATUS_PHRASES[-1] == "circling problem"
    assert "consulting runes" in FRONT_DOOR_STATUS_PHRASES
    assert "ideating" not in FRONT_DOOR_STATUS_PHRASES


def test_picker_draws_only_pool_members() -> None:
    drawn = {front_door_status_phrase() for _ in range(300)}
    assert drawn                                  # non-empty
    assert drawn <= set(FRONT_DOOR_STATUS_PHRASES)


def test_label_template_renders_to_a_pool_member_without_change_context() -> None:
    # The State.step_label seed renders labels with no change/log; the global
    # must still resolve there.
    out = prompts.render_label("{{ front_door_status_phrase() }}", change=None, log=None)
    assert out in FRONT_DOOR_STATUS_PHRASES


def test_render_label_without_context_leaves_var_templates_raw() -> None:
    # Regression: a vars-referencing label with no change/log still falls back
    # to the raw template text (unchanged behavior).
    raw = "{{ vars.plan_index }}"
    assert prompts.render_label(raw, change=None, log=None) == raw
