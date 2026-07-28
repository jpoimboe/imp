"""`imp init`'s confirmation screen: state, rendering, row edits, key bindings."""

from __future__ import annotations

from dataclasses import replace

import yaml

from imp.assets import bundled_assets_dir
from imp.cli.init import (
    BaseDetection,
    InitScreenState,
    ScreenRow,
    _confirm_rows,
    _cycle_workflow,
    _screen_state_from_detection,
    _workflow_blurb,
)
from imp.cli.pythonpath_detect import ImportRoots
from imp.cli.verify_detect import DetectedTestCommand

_WORKFLOWS = ["default", "minimal", "patent"]
_LABELS = ["workflow", "branch", "tests", "import root"]


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


def _body(state: InitScreenState, cursor: ScreenRow) -> list[str]:
    """Each row's text with the two-character caret/gutter prefix removed."""
    return [text[2:].rstrip("\n")
            for _style, text in _confirm_rows(state, cursor)[1:-1]]


def test_confirm_rows_header_and_hint_are_verbatim() -> None:
    rows = _confirm_rows(_detected_state(), ScreenRow.WORKFLOW)
    assert rows[0] == ("bold", "Here's how imp will set up this project:\n\n")
    assert rows[-1] == (
        "ansibrightblack",
        "\n↑↓ move · space change · enter accept · ? talk it through · esc cancel")


def test_confirm_rows_carry_label_value_and_provenance_in_order() -> None:
    body = _body(_detected_state(), ScreenRow.WORKFLOW)
    assert len(body) == 4
    assert [line.split("  ")[0] for line in body] == _LABELS
    assert body[0].endswith("full pipeline — spec, plan, implement, review, merge")
    assert "default" in body[0]
    assert body[1].endswith("from origin/HEAD") and "master" in body[1]
    assert body[2].endswith("from Makefile") and "make check" in body[2]
    assert body[3].endswith("from pyproject.toml") and "src" in body[3]


def test_confirm_rows_pad_each_column_to_its_widest_entry() -> None:
    body = _body(_detected_state(), ScreenRow.WORKFLOW)
    values = ["default", "master", "make check", "src"]
    notes = ["full pipeline — spec, plan, implement, review, merge",
             "from origin/HEAD", "from Makefile", "from pyproject.toml"]
    value_col = max(len(label) for label in _LABELS) + 2
    note_col = value_col + max(len(value) for value in values) + 2
    for line, value, note in zip(body, values, notes, strict=True):
        assert line[value_col:].startswith(value)
        assert line[note_col:] == note


def test_confirm_rows_mark_the_cursor_row_with_a_render_safe_caret() -> None:
    rows = _confirm_rows(_detected_state(), ScreenRow.TESTS)
    styles = [style for style, _text in rows[1:-1]]
    texts = [text for _style, text in rows[1:-1]]
    assert styles == ["", "", "ansiyellow bold", ""]
    assert texts[2].startswith("> ")
    assert "❯" not in texts[2]
    assert all(t.startswith("  ") for i, t in enumerate(texts) if i != 2)


def test_confirm_rows_show_none_when_no_command_or_roots_were_detected() -> None:
    state = _screen_state_from_detection(
        workflow="default", base=BaseDetection("main", "new repo"),
        tests=None, roots=ImportRoots([], "not a Python project"))
    body = _body(state, ScreenRow.WORKFLOW)
    assert "(none)" in body[2] and body[2].endswith(
        "nothing detected — imp will run no tests")
    assert "(none)" in body[3] and body[3].endswith("not a Python project")
    assert "new repo" in body[1]


def test_confirm_rows_comma_join_multiple_import_roots() -> None:
    body = _body(replace(_detected_state(), pythonpath=["src", "lib"]),
                 ScreenRow.WORKFLOW)
    assert "src, lib" in body[3]


def test_confirm_rows_show_the_cycled_workflow_blurb_in_step() -> None:
    cycled = _cycle_workflow(_detected_state(), _WORKFLOWS)
    body = _body(cycled, ScreenRow.WORKFLOW)
    assert "minimal" in body[0]
    assert body[0].endswith(_workflow_blurb("minimal"))
