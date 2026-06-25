from __future__ import annotations

from imp.agents import prompts
from imp.agents.front_door_status import (
    FRONT_DOOR_STATUS_PHRASES,
    STATUS_PHRASE_LABEL,
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


def test_render_label_without_context_leaves_var_templates_raw() -> None:
    # Regression: a vars-referencing label with no change/log still falls back
    # to the raw template text (unchanged behavior).
    raw = "{{ vars.plan_index }}"
    assert prompts.render_label(raw, change=None, log=None) == raw


def test_sentinel_label_resolves_to_a_pool_member_without_context() -> None:
    out = prompts.render_label(STATUS_PHRASE_LABEL, change=None, log=None)
    assert out in FRONT_DOOR_STATUS_PHRASES


def test_sentinel_label_resolution_is_a_fresh_draw_each_call() -> None:
    draws = {prompts.render_label(STATUS_PHRASE_LABEL, change=None, log=None)
             for _ in range(300)}
    assert len(draws) > 1
    assert draws <= set(FRONT_DOOR_STATUS_PHRASES)
    assert STATUS_PHRASE_LABEL not in draws          # never echoes the sentinel


def test_plain_label_is_unaffected_by_the_sentinel_branch() -> None:
    assert prompts.render_label("implementing", change=None, log=None) == "implementing"
