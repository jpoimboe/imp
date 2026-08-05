"""imp.api.create_quest: synchronous creation in the caller's own process."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from imp.api import ImpError, InvalidVar, QuestHandle, create_quest
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _seed_control, _seed_control_with_vars


def _meta(control: Path, quest_id: str) -> dict[str, object]:
    path = project_state_dir(control) / "quests" / quest_id / "quest.json"
    return dict(json.loads(path.read_text()))


def test_returns_a_handle_naming_a_draft_that_exists_on_disk(tmp_path: Path) -> None:
    _seed_control(tmp_path)

    handle = create_quest(tmp_path, "interview me", title="Interview")

    assert isinstance(handle, QuestHandle)
    assert handle.title == "Interview"
    assert handle.control_root == tmp_path
    meta = _meta(tmp_path, handle.id)
    assert meta["id"] == handle.id
    assert meta["seed_opening"] == "interview me"
    state = State.load(project_state_dir(tmp_path) / "quests" / handle.id / "state")
    assert (state.status, state.step) == ("new", None)


def test_the_handle_is_frozen(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    handle = create_quest(tmp_path, "go")
    with pytest.raises(FrozenInstanceError):
        handle.id = "other"   # type: ignore[misc]


def test_base_none_resolves_the_configured_default_base(tmp_path: Path) -> None:
    _seed_control(tmp_path)

    handle = create_quest(tmp_path, "go")

    assert _meta(tmp_path, handle.id)["base_branch"] == "main"


def test_an_unset_default_base_raises_imp_error_and_creates_no_quest(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")

    with pytest.raises(ImpError) as excinfo:
        create_quest(tmp_path, "go")

    assert "default-base" in str(excinfo.value)
    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_a_nonexistent_base_branch_raises_imp_error_and_creates_no_quest(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)

    with pytest.raises(ImpError):
        create_quest(tmp_path, "go", base="no-such-branch")

    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_typed_vars_are_persisted_as_supplied(tmp_path: Path) -> None:
    _seed_control_with_vars(tmp_path)

    handle = create_quest(tmp_path, "go", vars={"retries": 5, "jurisdiction": "us"})

    assert _meta(tmp_path, handle.id)["vars"] == {"retries": 5, "jurisdiction": "us"}


def test_an_undeclared_var_raises_invalid_var_and_creates_no_quest(
    tmp_path: Path,
) -> None:
    _seed_control_with_vars(tmp_path)

    with pytest.raises(InvalidVar) as excinfo:
        create_quest(tmp_path, "go", vars={"nope": 1})

    assert "not declared by the workflow" in str(excinfo.value)
    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_a_mistyped_var_raises_invalid_var_and_creates_no_quest(tmp_path: Path) -> None:
    _seed_control_with_vars(tmp_path)

    with pytest.raises(InvalidVar):
        create_quest(tmp_path, "go", vars={"retries": "many"})

    assert not (project_state_dir(tmp_path) / "quests").exists()


def test_invalid_var_is_catchable_as_imp_error(tmp_path: Path) -> None:
    _seed_control_with_vars(tmp_path)

    with pytest.raises(ImpError):
        create_quest(tmp_path, "go", vars={"nope": 1})


def test_no_vars_loads_no_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 17's guarantee on the API path: skew must not veto a var-less create.
    _seed_control(tmp_path)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("the workflow must not be loaded")

    monkeypatch.setattr("imp.engine.loader.load_workflow", _boom)

    handle = create_quest(tmp_path, "go")

    assert _meta(tmp_path, handle.id)["vars"] == {}
