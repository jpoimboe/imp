"""The shared in-process creation helper: imp new's preflight, then a deferred draft."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.cli.main import BaseResolutionError
from imp.cli.var_flags import VarFlagError
from imp.create import create_draft_quest
from imp.engine.errors import LoadError
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _git, _seed_control, _seed_control_with_vars


def _meta(control: Path, quest_id: str) -> dict[str, object]:
    path = project_state_dir(control) / "quests" / quest_id / "quest.json"
    return dict(json.loads(path.read_text()))


def test_creates_a_deferred_draft_carrying_the_prompt_as_its_seed_opening(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)

    quest = create_draft_quest(tmp_path, "interview me", title="Interview")

    meta = _meta(tmp_path, quest.id)
    assert meta["seed_opening"] == "interview me"
    assert meta["title"] == "Interview"
    assert meta["materialized"] is False
    state = State.load(project_state_dir(tmp_path) / "quests" / quest.id / "state")
    assert (state.status, state.step) == ("new", None)   # deferred: seeded at first run


def test_resolves_the_configured_default_base_when_none_is_given(tmp_path: Path) -> None:
    _seed_control(tmp_path)   # imp/config.yaml carries `default-base: main`

    quest = create_draft_quest(tmp_path, "go")

    assert _meta(tmp_path, quest.id)["base_branch"] == "main"


def test_an_unset_default_base_raises_and_creates_no_quest(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")

    with pytest.raises(BaseResolutionError):
        create_draft_quest(tmp_path, "go")

    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_a_nonexistent_base_branch_raises_and_creates_no_quest(tmp_path: Path) -> None:
    _seed_control(tmp_path)

    with pytest.raises(BaseResolutionError):
        create_draft_quest(tmp_path, "go", base="no-such-branch")

    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_gitignores_imp_the_way_imp_new_does(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    (tmp_path / ".gitignore").write_text("")   # drop the seeded `.imp/` rule
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "drop the ignore rule")
    assert _git(tmp_path, "check-ignore", "-q", ".imp/").returncode != 0

    create_draft_quest(tmp_path, "go")

    assert _git(tmp_path, "check-ignore", "-q", ".imp/").returncode == 0


def test_typed_vars_land_on_the_draft(tmp_path: Path) -> None:
    _seed_control_with_vars(tmp_path)

    quest = create_draft_quest(tmp_path, "go", vars={"retries": 5, "strict": True})

    assert _meta(tmp_path, quest.id)["vars"] == {"retries": 5, "strict": True}


def test_a_rejected_var_raises_before_any_quest_exists(tmp_path: Path) -> None:
    _seed_control_with_vars(tmp_path)

    with pytest.raises(VarFlagError):
        create_draft_quest(tmp_path, "go", vars={"retries": "many"})

    assert not (project_state_dir(tmp_path) / "quests").exists()


@pytest.mark.parametrize("name", ["", "   ", "a/b", "a\\b", ".hidden"])
def test_an_invalid_workflow_name_raises_before_any_quest_exists(
    tmp_path: Path, name: str,
) -> None:
    # `imp new` shape-checks the name at cli/main.py:1243 before creating
    # anything; without the same check here a slashed name writes a draft that
    # only explodes at first resume.
    _seed_control(tmp_path)

    with pytest.raises(LoadError):
        create_draft_quest(tmp_path, "go", workflow=name)

    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_a_valid_workflow_name_still_loads_no_workflow_without_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The shape check is pure, so Decision 7's skew tolerance survives it.
    _seed_control(tmp_path)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("the workflow must not be loaded")

    monkeypatch.setattr("imp.engine.loader.load_workflow", _boom)

    quest = create_draft_quest(tmp_path, "go", workflow="imp")

    assert _meta(tmp_path, quest.id)["workflow"] == "imp"


def test_two_creations_mint_distinct_ids(tmp_path: Path) -> None:
    _seed_control(tmp_path)

    first = create_draft_quest(tmp_path, "one")
    second = create_draft_quest(tmp_path, "two")

    assert first.id != second.id
    assert _meta(tmp_path, second.id)["seed_opening"] == "two"
