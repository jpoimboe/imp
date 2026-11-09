"""`imp init`'s confirmation screen: state, rendering, row edits, key bindings."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.assets import bundled_assets_dir
from imp.cli.init import (
    _EDIT_HINTS,
    _EDIT_PROMPTS,
    BaseDetection,
    InitScreenState,
    ScreenAccepted,
    ScreenRow,
    _apply_row_edit,
    _confirm_rows,
    _cycle_workflow,
    _default_row_prompt,
    _edit_default,
    _interactive_confirm_screen,
    _row_cells,
    _row_prompt_message,
    _screen_state_from_detection,
    _workflow_blurb,
)
from imp.cli.pythonpath_detect import ImportRoots
from imp.cli.setup_detect import DetectedSetupCommand
from imp.cli.verify_detect import DetectedTestCommand

_WORKFLOWS = ["default", "minimal"]
_LABELS = ["workflow", "branch", "tests", "worktree setup", "Python import path"]
_HEADER = "Here's how imp will set up this project:"
_HINT = "↑↓ move · space change · enter accept · esc cancel"
_WIDE = 120  # no row of any state these tests build wraps at this width
_LONG_SETUP = "uv sync --all-extras --frozen && pre-commit install --install-hooks"


def _detected_state() -> InitScreenState:
    return _screen_state_from_detection(
        workflow="default",
        base=BaseDetection("master", "from origin/HEAD"),
        tests=DetectedTestCommand("make check", "from Makefile"),
        setup=DetectedSetupCommand("uv sync --all-extras", "from uv.lock"),
        roots=ImportRoots(["src"], "from pyproject.toml"),
    )


def _long_value_state() -> InitScreenState:
    return replace(_detected_state(), worktree_setup=_LONG_SETUP, setup_provenance="you set this")


def _text(rows: list[tuple[str, str]]) -> str:
    return "".join(text for _style, text in rows)


def _squash(text: str) -> str:
    return "".join(text.split())


def test_state_from_detection_keeps_each_value_with_its_own_provenance() -> None:
    state = _detected_state()
    assert state.workflow == "default"
    assert state.branch == "master"
    assert state.branch_provenance == "from origin/HEAD"
    assert state.test_cmd_fast == "make check"
    assert state.tests_provenance == "from Makefile"
    assert state.worktree_setup == "uv sync --all-extras"
    assert state.setup_provenance == "from uv.lock"
    assert state.pythonpath == ["src"]
    assert state.roots_provenance == "from pyproject.toml"


def test_state_with_nothing_detected_leaves_tests_unset_not_opted_out() -> None:
    state = _screen_state_from_detection(
        workflow="default", base=None, tests=None, setup=None,
        roots=ImportRoots([], "not a Python project"))
    # Nothing detected is nothing decided: None for both, never an empty spelling.
    assert state.test_cmd_fast is None
    assert state.tests_provenance == "nothing detected — imp runs no tests until one is set"
    assert state.worktree_setup is None
    assert state.setup_provenance == "nothing detected — no command runs in new worktrees"
    assert state.branch == ""
    assert state.branch_provenance == "nothing detected"
    assert state.pythonpath is None
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
    assert _workflow_blurb("housekeeping") == "custom workflow"


def test_cycle_workflow_advances_wraps_and_touches_nothing_else() -> None:
    state = _detected_state()
    once = _cycle_workflow(state, _WORKFLOWS)
    twice = _cycle_workflow(once, _WORKFLOWS)
    thrice = _cycle_workflow(twice, _WORKFLOWS)
    assert [once.workflow, twice.workflow, thrice.workflow] == [
        "minimal", "default", "minimal"]
    assert thrice.branch == "master"
    assert thrice.branch_provenance == "from origin/HEAD"
    assert thrice.test_cmd_fast == "make check"
    assert thrice.tests_provenance == "from Makefile"
    assert thrice.worktree_setup == "uv sync --all-extras"
    assert thrice.pythonpath == ["src"]
    assert thrice.roots_provenance == "from pyproject.toml"


def _body(state: InitScreenState, cursor: ScreenRow) -> list[str]:
    """Each row's single line at a width where nothing wraps, caret/gutter removed."""
    return [text[2:].rstrip("\n")
            for _style, text in _confirm_rows(state, cursor, _WIDE)[2:-1]]


def test_confirm_rows_header_and_hint_are_verbatim() -> None:
    rows = _confirm_rows(_detected_state(), ScreenRow.WORKFLOW, 80)
    # screen-owned: erased with the screen, so it never renders flush against splash or re-draws
    assert rows[0] == ("", "\n")
    assert rows[1] == ("bold", "Here's how imp will set up this project:\n\n")
    assert rows[-1] == (
        "ansibrightblack",
        "\n↑↓ move · space change · enter accept · esc cancel")


def test_confirm_rows_carry_label_value_and_provenance_in_order() -> None:
    body = _body(_detected_state(), ScreenRow.WORKFLOW)
    assert len(body) == 5
    assert [line.split("  ")[0] for line in body] == _LABELS
    assert body[0].endswith("full pipeline — spec, plan, implement, review, merge")
    assert "default" in body[0]
    assert body[1].endswith("from origin/HEAD") and "master" in body[1]
    assert body[2].endswith("from Makefile") and "make check" in body[2]
    assert body[3].endswith("from uv.lock") and "uv sync --all-extras" in body[3]
    assert body[4].endswith("from pyproject.toml") and "src" in body[4]


def test_confirm_rows_pad_each_column_to_its_widest_entry() -> None:
    body = _body(_detected_state(), ScreenRow.WORKFLOW)
    values = ["default", "master", "make check", "uv sync --all-extras", "src"]
    notes = ["full pipeline — spec, plan, implement, review, merge",
             "from origin/HEAD", "from Makefile", "from uv.lock", "from pyproject.toml"]
    value_col = max(len(label) for label in _LABELS) + 2
    note_col = value_col + max(len(value) for value in values) + 2
    for line, value, note in zip(body, values, notes, strict=True):
        assert line[value_col:].startswith(value)
        assert line[note_col:] == note


def test_confirm_rows_mark_the_cursor_row_with_a_render_safe_caret() -> None:
    rows = _confirm_rows(_detected_state(), ScreenRow.TESTS, _WIDE)
    styles = [style for style, _text in rows[2:-1]]
    texts = [text for _style, text in rows[2:-1]]
    assert styles == ["", "", "ansiyellow bold", "", ""]
    assert texts[2].startswith("> ")
    assert "❯" not in texts[2]
    assert all(t.startswith("  ") for i, t in enumerate(texts) if i != 2)


def test_confirm_rows_wrap_the_workflow_blurb_in_its_note_column_at_80() -> None:
    state = replace(_detected_state(), branch="main", branch_provenance="current branch")
    rows = _confirm_rows(state, ScreenRow.WORKFLOW, 80)
    assert _text(rows) == "\n" + _HEADER + "\n\n" + "".join(f"{line}\n" for line in [
        "> workflow            default               full pipeline — spec, plan,",
        " " * 44 + "implement, review, merge",
        "  branch              main                  current branch",
        "  tests               make check            from Makefile",
        "  worktree setup      uv sync --all-extras  from uv.lock",
        "  Python import path  src                   from pyproject.toml",
    ]) + "\n" + _HINT


def test_confirm_rows_wrap_a_long_value_within_its_own_column_at_80() -> None:
    # Every note stays on screen; the value column caps at 32 and its continuation
    # lines start at the value column; --install-hooks is never split at its hyphen.
    state = replace(_long_value_state(), branch="main", branch_provenance="current branch")
    rows = _confirm_rows(state, ScreenRow.WORKTREE_SETUP, 80)
    assert _text(rows) == "\n" + _HEADER + "\n\n" + "".join(f"{line}\n" for line in [
        "  workflow            default                           full pipeline — spec,",
        " " * 56 + "plan, implement, review,",
        " " * 56 + "merge",
        "  branch              main                              current branch",
        "  tests               make check                        from Makefile",
        "> worktree setup      uv sync --all-extras --frozen &&  you set this",
        " " * 22 + "pre-commit install",
        " " * 22 + "--install-hooks",
        "  Python import path  src                               from pyproject.toml",
    ]) + "\n" + _HINT


_VALUE_COL = 22  # 2-char gutter + the widest label (18) + a 2-char gap


def _read_back(rows: list[tuple[str, str]], value_w: int) -> list[tuple[str, str]]:
    """Each row's (value, note), whitespace-squashed, read off the rendered grid."""
    note_col = _VALUE_COL + value_w + 2
    read: list[tuple[str, str]] = []
    for _style, line in rows[2:-1]:
        if line[2:_VALUE_COL].strip():
            read.append(("", ""))
        value, note = read[-1]
        read[-1] = (value + _squash(line[_VALUE_COL:note_col]), note + _squash(line[note_col:]))
    return read


def _squashed_cells(state: InitScreenState) -> list[tuple[str, str]]:
    cells = _row_cells(state)
    return [(_squash(cells[row][1]), _squash(cells[row][2])) for row in ScreenRow]


@pytest.mark.parametrize(("state", "width", "value_w"), [
    (_detected_state(), 80, 20),
    (_detected_state(), 60, 18),
    (_detected_state(), 40, 8),
    (_long_value_state(), 80, 32),
    (_long_value_state(), 60, 18),
    (_long_value_state(), 40, 8),
])
def test_confirm_rows_wrap_within_the_width_and_drop_nothing(
    state: InitScreenState, width: int, value_w: int,
) -> None:
    rows = _confirm_rows(state, ScreenRow.WORKFLOW, width)
    assert all(len(line) <= width for line in _text(rows).split("\n"))
    assert _read_back(rows, value_w) == _squashed_cells(state)


@pytest.mark.parametrize("state", [_detected_state(), _long_value_state()])
def test_confirm_rows_at_a_tiny_width_still_show_every_character(
    state: InitScreenState,
) -> None:
    assert _read_back(_confirm_rows(state, ScreenRow.WORKFLOW, 10), 1) == (
        _squashed_cells(state))


def test_confirm_rows_wrap_only_once_the_columns_overflow_the_width() -> None:
    state = _detected_state()
    cells = _row_cells(state).values()
    fit = 6 + sum(max(len(cell[i]) for cell in cells) for i in range(3))
    assert len(_confirm_rows(state, ScreenRow.WORKFLOW, fit)[2:-1]) == len(ScreenRow)
    assert len(_confirm_rows(state, ScreenRow.WORKFLOW, fit - 1)[2:-1]) > len(ScreenRow)


def test_a_wrapped_cursor_row_is_highlighted_on_every_line_with_one_caret() -> None:
    rows = _confirm_rows(_long_value_state(), ScreenRow.WORKTREE_SETUP, 80)[2:-1]
    assert [style for style, _line in rows] == (
        [""] * 5 + ["ansiyellow bold"] * 3 + [""])
    assert rows[5][1].startswith("> worktree setup")
    assert all(text.startswith("  ") for _style, text in rows[6:8])


def test_header_and_hint_word_wrap_when_the_terminal_is_narrower() -> None:
    rows = _confirm_rows(_detected_state(), ScreenRow.WORKFLOW, 30)
    assert rows[1][0] == "bold" and rows[-1][0] == "ansibrightblack"
    header = rows[1][1].removesuffix("\n\n").split("\n")
    hint = rows[-1][1].removeprefix("\n").split("\n")
    for lines, original in ((header, _HEADER), (hint, _HINT)):
        assert len(lines) > 1
        assert all(len(line) <= 30 for line in lines)
        assert " ".join(lines).split() == original.split()


def test_confirm_rows_show_none_when_no_command_or_roots_were_detected() -> None:
    state = _screen_state_from_detection(
        workflow="default", base=BaseDetection("main", "new repo"),
        tests=None, setup=None, roots=ImportRoots([], "not a Python project"))
    body = _body(state, ScreenRow.WORKFLOW)
    assert "(none)" in body[2] and body[2].endswith(
        "nothing detected — imp runs no tests until one is set")
    assert "(none)" in body[3] and body[3].endswith(
        "nothing detected — no command runs in new worktrees")
    assert "(none)" in body[4] and body[4].endswith("not a Python project")
    assert "new repo" in body[1]


def test_confirm_rows_comma_join_multiple_import_roots() -> None:
    body = _body(replace(_detected_state(), pythonpath=["src", "lib"]),
                 ScreenRow.WORKFLOW)
    assert "src, lib" in body[4]


def test_confirm_rows_show_the_cycled_workflow_blurb_in_step() -> None:
    cycled = _cycle_workflow(_detected_state(), _WORKFLOWS)
    body = _body(cycled, ScreenRow.WORKFLOW)
    assert "minimal" in body[0]
    assert body[0].endswith(_workflow_blurb("minimal"))


def test_editing_the_branch_marks_the_row_as_user_set(tmp_path: Path) -> None:
    state = _apply_row_edit(tmp_path, _detected_state(), ScreenRow.BRANCH, "  trunk  ")
    assert state.branch == "trunk"
    assert state.branch_provenance == "you set this"
    assert _body(state, ScreenRow.BRANCH)[1].endswith("you set this")


def test_emptying_the_branch_still_reads_as_user_set(tmp_path: Path) -> None:
    # An empty branch blocks accept, so it is never a "none needed" answer.
    state = _apply_row_edit(tmp_path, _detected_state(), ScreenRow.BRANCH, "   ")
    assert state.branch == ""
    assert state.branch_provenance == "you set this"


def test_editing_tests_to_a_command_replaces_the_detected_one(tmp_path: Path) -> None:
    state = _apply_row_edit(tmp_path, _detected_state(), ScreenRow.TESTS, "just check")
    assert state.test_cmd_fast == "just check"
    assert _body(state, ScreenRow.TESTS)[2].endswith("you set this")


def test_editing_tests_to_empty_records_needs_none(tmp_path: Path) -> None:
    state = _apply_row_edit(tmp_path, _detected_state(), ScreenRow.TESTS, "   ")
    # False is the user's answer "this project needs none"; None would mean the row
    # was never decided and would have imp raise it again.
    assert state.test_cmd_fast is False
    assert state.tests_provenance == "you left it empty"
    body = _body(state, ScreenRow.TESTS)[2]
    assert "(none needed)" in body and body.endswith("you left it empty")


def test_editing_import_roots_drops_entries_that_do_not_validate(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "lib").mkdir()
    answer = f"src , {tmp_path / 'src'}, ../escape, missing, lib"
    state = _apply_row_edit(tmp_path, _detected_state(), ScreenRow.IMPORT_ROOT, answer)
    assert state.pythonpath == ["src", "lib"]
    assert state.roots_provenance == "you set this"
    assert "src, lib" in _body(state, ScreenRow.IMPORT_ROOT)[4]


def test_editing_import_roots_to_empty_records_needs_none(tmp_path: Path) -> None:
    state = _apply_row_edit(tmp_path, _detected_state(), ScreenRow.IMPORT_ROOT, "  ")
    assert state.pythonpath is False
    assert state.roots_provenance == "you left it empty"
    body = _body(state, ScreenRow.IMPORT_ROOT)[4]
    assert "(none needed)" in body and body.endswith("you left it empty")


def test_editing_import_roots_to_only_commas_records_needs_none(tmp_path: Path) -> None:
    state = _apply_row_edit(tmp_path, _detected_state(), ScreenRow.IMPORT_ROOT, " , ,")
    assert state.pythonpath is False
    assert state.roots_provenance == "you left it empty"
    body = _body(state, ScreenRow.IMPORT_ROOT)[4]
    assert "(none needed)" in body and body.endswith("you left it empty")


def test_editing_import_roots_all_invalid_leaves_undecided_with_note(tmp_path: Path) -> None:
    # "srcc" doesn't exist: a typo must not permanently opt out of import roots.
    state = _apply_row_edit(tmp_path, _detected_state(), ScreenRow.IMPORT_ROOT, "srcc")
    assert state.pythonpath is None
    assert state.roots_provenance == "no such directory in this repo — nothing recorded"
    body = _body(state, ScreenRow.IMPORT_ROOT)[4]
    assert "(none)" in body
    assert "no such directory in this repo" in body


def test_edit_prompts_prefill_the_current_row_value() -> None:
    state = _detected_state()
    assert _edit_default(state, ScreenRow.BRANCH) == "master"
    assert _edit_default(state, ScreenRow.TESTS) == "make check"
    assert _edit_default(state, ScreenRow.WORKTREE_SETUP) == "uv sync --all-extras"
    assert _edit_default(
        replace(state, worktree_setup=None), ScreenRow.WORKTREE_SETUP) == ""
    assert _edit_default(
        replace(state, worktree_setup=False), ScreenRow.WORKTREE_SETUP) == ""
    assert _edit_default(state, ScreenRow.IMPORT_ROOT) == "src"
    assert _edit_default(
        replace(state, pythonpath=["src", "lib"]), ScreenRow.IMPORT_ROOT) == "src, lib"
    assert _edit_default(
        replace(state, test_cmd_fast=None), ScreenRow.TESTS) == ""
    assert _edit_default(
        replace(state, test_cmd_fast=False), ScreenRow.TESTS) == ""
    assert _edit_default(
        replace(state, pythonpath=None), ScreenRow.IMPORT_ROOT) == ""
    assert _edit_default(
        replace(state, pythonpath=False), ScreenRow.IMPORT_ROOT) == ""


def test_edit_prompt_labels_are_verbatim() -> None:
    assert _EDIT_PROMPTS[ScreenRow.BRANCH] == "Integration branch: "
    assert _EDIT_PROMPTS[ScreenRow.TESTS] == "Fast test command: "
    assert _EDIT_PROMPTS[ScreenRow.WORKTREE_SETUP] == "Worktree setup command: "
    assert _EDIT_PROMPTS[ScreenRow.IMPORT_ROOT] == "Python import paths (comma-separated): "
    assert ScreenRow.WORKFLOW not in _EDIT_PROMPTS


def test_edit_prompt_hints_are_verbatim() -> None:
    assert _EDIT_HINTS[ScreenRow.BRANCH] == (
        "The branch each quest forks from and merges back into.")
    assert _EDIT_HINTS[ScreenRow.TESTS] == (
        "Lint, typecheck and fast tests, run after each change. Leave empty for none.")
    assert _EDIT_HINTS[ScreenRow.WORKTREE_SETUP] == (
        "Installs dependencies in each new worktree, e.g. uv sync. Leave empty for none.")
    assert _EDIT_HINTS[ScreenRow.IMPORT_ROOT] == (
        "Put on PYTHONPATH so tests import the worktree's code. Leave empty for none.")
    assert ScreenRow.WORKFLOW not in _EDIT_HINTS


def test_row_prompt_message_is_blank_line_dim_hint_then_plain_label() -> None:
    assert _row_prompt_message(ScreenRow.BRANCH) == [
        ("", "\n"),
        ("ansibrightblack", "The branch each quest forks from and merges back into."),
        ("", "\n"),
        ("", "Integration branch: "),
    ]


def test_the_default_row_prompt_renders_the_hint_above_the_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prompt_toolkit as pt

    captured: dict[str, object] = {}

    def spy(message: object, **kwargs: object) -> str:
        captured["message"] = message
        captured.update(kwargs)
        return "just check"

    monkeypatch.setattr(pt, "prompt", spy)
    assert _default_row_prompt(ScreenRow.TESTS, "make check") == "just check"
    assert captured["default"] == "make check"
    message = captured["message"]
    assert isinstance(message, list)  # FormattedText is a list subclass
    assert list(message) == [
        ("", "\n"),
        ("ansibrightblack",
         "Lint, typecheck and fast tests, run after each change. Leave empty for none."),
        ("", "\n"),
        ("", "Fast test command: "),
    ]


def _never_prompts(row: ScreenRow, default: str) -> str:
    raise AssertionError(f"the screen must not prompt here: {row!r}")


def test_space_on_the_workflow_row_cycles_without_leaving_the_screen(
    tmp_path: Path,
) -> None:
    with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=DummyOutput()):
        pipe.send_text(" ")     # cycle default -> minimal, in place
        pipe.send_text("\r")    # accept
        outcome = _interactive_confirm_screen(
            tmp_path, _detected_state(), workflows=_WORKFLOWS,
            prompt_fn=_never_prompts)
    assert isinstance(outcome, ScreenAccepted)
    assert outcome.state.workflow == "minimal"
    assert outcome.state.branch == "master"


def test_space_on_an_editable_row_prompts_prefilled_then_re_renders(
    tmp_path: Path,
) -> None:
    asked: list[tuple[ScreenRow, str]] = []

    def _fake_prompt(row: ScreenRow, default: str) -> str:
        asked.append((row, default))
        return "trunk"

    with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[B")   # down to branch
        pipe.send_text(" ")        # edit it
        pipe.send_text("\r")       # accept the re-rendered screen
        outcome = _interactive_confirm_screen(
            tmp_path, _detected_state(), workflows=_WORKFLOWS,
            prompt_fn=_fake_prompt)
    assert asked == [(ScreenRow.BRANCH, "master")]
    assert isinstance(outcome, ScreenAccepted)
    assert outcome.state.branch == "trunk"
    assert outcome.state.branch_provenance == "you set this"


def test_up_wraps_from_the_first_row_to_import_root(tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    asked: list[tuple[ScreenRow, str]] = []

    def _fake_prompt(row: ScreenRow, default: str) -> str:
        asked.append((row, default))
        return "lib"

    with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[A")   # up from workflow wraps to import root
        pipe.send_text(" ")
        pipe.send_text("\r")
        outcome = _interactive_confirm_screen(
            tmp_path, _detected_state(), workflows=_WORKFLOWS,
            prompt_fn=_fake_prompt)
    assert asked == [(ScreenRow.IMPORT_ROOT, "src")]
    assert isinstance(outcome, ScreenAccepted)
    assert outcome.state.pythonpath == ["lib"]


def test_enter_is_refused_while_the_branch_is_empty(tmp_path: Path) -> None:
    state = _screen_state_from_detection(
        workflow="default", base=None, tests=None, setup=None,
        roots=ImportRoots([], "not a Python project"))
    with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=DummyOutput()):
        pipe.send_text("\r")      # refused: the screen stays open
        pipe.send_text("\x1b")    # esc cancels it instead
        with pytest.raises(KeyboardInterrupt):
            _interactive_confirm_screen(
                tmp_path, state, workflows=_WORKFLOWS, prompt_fn=_never_prompts)


def test_question_mark_does_nothing_on_the_screen(tmp_path: Path) -> None:
    with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=DummyOutput()):
        pipe.send_text(" ")    # cycle the workflow first
        pipe.send_text("?")    # inert: the screen stays open …
        pipe.send_text("\r")   # … and enter is what accepts
        outcome = _interactive_confirm_screen(
            tmp_path, _detected_state(), workflows=_WORKFLOWS,
            prompt_fn=_never_prompts)
    assert isinstance(outcome, ScreenAccepted)
    assert outcome.state.workflow == "minimal"


def test_ctrl_c_cancels_the_screen(tmp_path: Path) -> None:
    with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=DummyOutput()):
        pipe.send_text("\x03")
        with pytest.raises(KeyboardInterrupt):
            _interactive_confirm_screen(
                tmp_path, _detected_state(), workflows=_WORKFLOWS,
                prompt_fn=_never_prompts)


def test_the_row_prompt_pre_fills_the_current_value() -> None:
    with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=DummyOutput()):
        pipe.send_text("\r")   # accept the pre-filled value untouched
        assert _default_row_prompt(ScreenRow.BRANCH, "master") == "master"


def test_ctrl_d_at_a_row_prompt_cancels_the_screen() -> None:
    with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=DummyOutput()):
        pipe.send_text("\x7f" * len("master"))   # clear the pre-filled value
        pipe.send_text("\x04")                   # ctrl-d on an empty buffer is EOF
        with pytest.raises(KeyboardInterrupt):
            _default_row_prompt(ScreenRow.BRANCH, "master")


def test_the_screen_erases_itself_so_re_renders_do_not_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prompt_toolkit as pt

    real_application = pt.Application
    captured: dict[str, object] = {}

    def spy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_application(*args, **kwargs)

    monkeypatch.setattr(pt, "Application", spy)
    with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=DummyOutput()):
        pipe.send_text("\r")
        _interactive_confirm_screen(
            tmp_path, _detected_state(), workflows=_WORKFLOWS,
            prompt_fn=_never_prompts)
    assert captured.get("erase_when_done") is True


def test_down_wraps_from_the_last_row_back_to_the_first_landing_on_workflow(
    tmp_path: Path,
) -> None:
    with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[A")   # up from workflow wraps to import root
        pipe.send_text("\x1b[B")   # down from import root wraps back to workflow
        pipe.send_text(" ")        # space on workflow row cycles default -> minimal, no prompt
        pipe.send_text("\r")       # accept
        outcome = _interactive_confirm_screen(
            tmp_path, _detected_state(), workflows=_WORKFLOWS,
            prompt_fn=_never_prompts)
    assert isinstance(outcome, ScreenAccepted)
    assert outcome.state.workflow == "minimal"
    assert outcome.state.pythonpath == ["src"]


def test_confirm_rows_show_none_when_no_branch_resolved() -> None:
    state = _screen_state_from_detection(
        workflow="default", base=None, tests=None, setup=None,
        roots=ImportRoots([], "not a Python project"))
    body = _body(state, ScreenRow.WORKFLOW)
    assert "(none)" in body[1] and body[1].endswith("nothing detected")


def test_editing_worktree_setup_to_a_command_replaces_the_detected_one(
    tmp_path: Path,
) -> None:
    state = _apply_row_edit(
        tmp_path, _detected_state(), ScreenRow.WORKTREE_SETUP, "  npm ci  ")
    assert state.worktree_setup == "npm ci"
    assert _body(state, ScreenRow.WORKTREE_SETUP)[3].endswith("you set this")


def test_editing_worktree_setup_to_empty_records_needs_none(tmp_path: Path) -> None:
    state = _apply_row_edit(
        tmp_path, _detected_state(), ScreenRow.WORKTREE_SETUP, "   ")
    # False is the user's answer "this project needs none"; None would mean never decided.
    assert state.worktree_setup is False
    assert state.setup_provenance == "you left it empty"
    body = _body(state, ScreenRow.WORKTREE_SETUP)[3]
    assert "(none needed)" in body and body.endswith("you left it empty")


def test_the_worktree_setup_row_sits_between_tests_and_import_root() -> None:
    assert list(ScreenRow) == [
        ScreenRow.WORKFLOW, ScreenRow.BRANCH, ScreenRow.TESTS,
        ScreenRow.WORKTREE_SETUP, ScreenRow.IMPORT_ROOT]



