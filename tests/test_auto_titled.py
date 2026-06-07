"""auto_titled flag (spec §12): persisted in change.json, reloaded by
Change.load, and exposed to agent prompts as {{ change.auto_titled }}."""

from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.engine.change import Change
from imp.engine.state import JsonlEventLog
from tests.conftest import _seed_control


def test_auto_titled_defaults_false_and_round_trips(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Real Title", slug="real-title",
                       base_branch="main", auto_titled=True)
    assert ch.auto_titled is True
    reloaded = Change.load(tmp_path, "real-title")
    assert reloaded.auto_titled is True


def test_auto_titled_absent_in_json_loads_false(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Plain", slug="plain", base_branch="main")
    assert ch.auto_titled is False
    reloaded = Change.load(tmp_path, "plain")
    assert reloaded.auto_titled is False


def test_change_namespace_exposes_auto_titled_to_prompts(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Prov", slug="prov", base_branch="main",
                       auto_titled=True)
    log = JsonlEventLog(ch.events_path)
    ctx = build_context(change_slug=ch.slug, change_title=ch.title,
                        change_auto_titled=ch.auto_titled, event_log=log)
    rendered = render_prompt(
        "{% if change.auto_titled %}RENAME-ME{% else %}KEEP{% endif %}", ctx
    )
    assert rendered == "RENAME-ME"
