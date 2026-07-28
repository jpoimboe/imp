"""`imp init`'s confirmation screen: state, rendering, row edits, key bindings."""

from __future__ import annotations

import yaml

from imp.assets import bundled_assets_dir
from imp.cli.init import (
    BaseDetection,
    InitScreenState,
    _cycle_workflow,
    _screen_state_from_detection,
    _workflow_blurb,
)
from imp.cli.pythonpath_detect import ImportRoots
from imp.cli.verify_detect import DetectedTestCommand

_WORKFLOWS = ["default", "minimal", "patent"]


def _detected_state() -> InitScreenState:
    return _screen_state_from_detection(
        workflow="default",
        base=BaseDetection("master", "from origin/HEAD"),
        tests=DetectedTestCommand("make check", "from Makefile"),
        roots=ImportRoots(["src"], "from pyproject.toml"),
    )


def test_state_from_detection_keeps_each_value_with_its_own_provenance() -> None:
    state = _detected_state()
    assert state.workflow == "default"
    assert state.branch == "master"
    assert state.branch_provenance == "from origin/HEAD"
    assert state.test_cmd_fast == "make check"
    assert state.tests_provenance == "from Makefile"
    assert state.pythonpath == ["src"]
    assert state.roots_provenance == "from pyproject.toml"


def test_state_with_nothing_detected_leaves_tests_unset_not_opted_out() -> None:
    state = _screen_state_from_detection(
        workflow="default", base=None, tests=None,
        roots=ImportRoots([], "not a Python project"))
    # None omits the test_cmd_fast block; "" is the user's explicit opt-out.
    assert state.test_cmd_fast is None
    assert state.tests_provenance == "nothing detected — imp will run no tests"
    assert state.branch == ""
    assert state.branch_provenance == "nothing detected"
    assert state.pythonpath == []
    assert state.roots_provenance == "not a Python project"


def test_workflow_blurb_never_echoes_the_workflow_description_pipeline() -> None:
    for name in _WORKFLOWS:
        described = yaml.safe_load(
            (bundled_assets_dir() / "workflows" / name / "workflow.yaml").read_text())
        first_line = str(described["description"]).strip().splitlines()[0]
        blurb = _workflow_blurb(name)
        assert blurb != first_line
        assert "->" not in blurb


def test_workflow_blurb_is_the_hand_written_copy_per_workflow() -> None:
    assert _workflow_blurb("default") == (
        "full pipeline — spec, plan, implement, review, merge")
    assert _workflow_blurb("minimal") == "scope, build on a branch, merge it yourself"
    assert _workflow_blurb("patent") == (
        "turns an inventor interview into a patent-spec draft")
    assert _workflow_blurb("housekeeping") == "custom workflow"


def test_cycle_workflow_advances_wraps_and_touches_nothing_else() -> None:
    state = _detected_state()
    once = _cycle_workflow(state, _WORKFLOWS)
    twice = _cycle_workflow(once, _WORKFLOWS)
    thrice = _cycle_workflow(twice, _WORKFLOWS)
    assert [once.workflow, twice.workflow, thrice.workflow] == [
        "minimal", "patent", "default"]
    assert thrice.branch == "master"
    assert thrice.branch_provenance == "from origin/HEAD"
    assert thrice.test_cmd_fast == "make check"
    assert thrice.tests_provenance == "from Makefile"
    assert thrice.pythonpath == ["src"]
    assert thrice.roots_provenance == "from pyproject.toml"
