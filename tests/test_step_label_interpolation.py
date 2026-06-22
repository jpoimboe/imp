from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from imp.engine.change import Change
from imp.engine.executor import _render_label
from tests.conftest import _seed_control

PLAN_LABEL = (
    "writing plan {{ vars.plan_index }}"
    "{% if vars.plan_total %}/{{ vars.plan_total }}{% endif %}"
)


def _change(tmp_path: Path) -> Change:
    _seed_control(tmp_path)
    return Change.create(tmp_path, title="t", change_id="aaa111", base_branch="main")


def _state(**vars: object) -> SimpleNamespace:
    return SimpleNamespace(vars=dict(vars))


def test_templated_label_renders_vars(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    out = _render_label(PLAN_LABEL, ch, ch.event_log(),
                        _state(plan_index=2, plan_total=4))
    assert out == "writing plan 2/4"


def test_zero_total_drops_denominator(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    out = _render_label(PLAN_LABEL, ch, ch.event_log(),
                        _state(plan_index=1, plan_total=0))
    assert out == "writing plan 1"


def test_non_templated_label_unchanged(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    assert _render_label("implementing", ch, ch.event_log(), _state()) == "implementing"


def test_none_label_passes_through(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    assert _render_label(None, ch, ch.event_log(), _state()) is None
