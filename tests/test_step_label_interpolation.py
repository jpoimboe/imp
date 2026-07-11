from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from imp.agents.prompts import render_label
from imp.engine.quest import Quest
from tests.conftest import _seed_control

PLAN_LABEL = (
    "plan {{ vars.plan_index }}"
    "{% if vars.plan_total %}/{{ vars.plan_total }}{% endif %} · writing"
)


def _change(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    return Quest.create(tmp_path, title="t", quest_id="aaa111", base_branch="main")


def _state(**vars: object) -> SimpleNamespace:
    return SimpleNamespace(vars=dict(vars))


def test_templated_label_renders_vars(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    out = render_label(PLAN_LABEL, ch, ch.event_log(),
                       _state(plan_index=2, plan_total=4))
    assert out == "plan 2/4 · writing"


def test_zero_total_drops_denominator(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    out = render_label(PLAN_LABEL, ch, ch.event_log(),
                       _state(plan_index=1, plan_total=0))
    assert out == "plan 1 · writing"


def test_non_templated_label_unchanged(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    assert render_label("implementing", ch, ch.event_log(), _state()) == "implementing"


def test_none_label_passes_through(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    assert render_label(None, ch, ch.event_log(), _state()) is None


def test_unset_var_returns_raw_label(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    raw = "writing plan {{ vars.missing_var }}"
    # StrictUndefined would raise inside render_prompt; render_label must catch
    # PromptRenderError and return the label untouched.
    assert render_label(raw, ch, ch.event_log(), _state()) == raw


def test_none_change_returns_raw_label() -> None:
    raw = "writing plan {{ vars.plan_index }}"
    assert render_label(raw, None, None, None) == raw
