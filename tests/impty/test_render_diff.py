from __future__ import annotations

from impty.driver import _diff_body


def _rows(n: int) -> list[str]:
    return [f"r{i}" for i in range(n)]


def test_a_single_replaced_row_is_labelled_by_its_index() -> None:
    prev = _rows(10)
    cur = list(prev)
    cur[7] = "changed"
    assert _diff_body(prev, cur) == "[row 7]\nchanged"


def test_a_replaced_run_is_labelled_with_an_inclusive_range() -> None:
    prev = _rows(24)
    cur = list(prev)
    cur[20:23] = ["x20", "x21", "x22"]
    assert _diff_body(prev, cur) == "[rows 20-22]\nx20\nx21\nx22"


def test_a_replace_consuming_more_rows_than_it_emits_names_the_count() -> None:
    prev = _rows(10)
    cur = [*prev[:7], "merged", *prev[10:]]
    assert _diff_body(prev, cur) == "[row 7 replacing 3 rows]\nmerged"


def test_a_replace_emitting_more_rows_than_it_consumes_names_the_count() -> None:
    prev = _rows(24)
    cur = [*prev[:20], "a", "b", "c", *prev[21:]]
    assert _diff_body(prev, cur) == "[rows 20-22 replacing 1 row]\na\nb\nc"


def test_one_inserted_row_is_marked_inserted() -> None:
    prev = _rows(10)
    cur = [*prev[:7], "new", *prev[7:]]
    assert _diff_body(prev, cur) == "[row 7 inserted]\nnew"


def test_an_inserted_run_is_marked_inserted() -> None:
    prev = _rows(24)
    cur = [*prev[:21], "n1", "n2", "n3", *prev[21:]]
    assert _diff_body(prev, cur) == "[rows 21-23 inserted]\nn1\nn2\nn3"


def test_one_deleted_row_emits_no_rows() -> None:
    prev = _rows(24)
    cur = prev[:13] + prev[14:]
    assert _diff_body(prev, cur) == "[1 row deleted at row 13]"


def test_deleted_rows_are_counted_and_emit_no_rows() -> None:
    prev = _rows(24)
    assert _diff_body(prev, prev[3:]) == "[3 rows deleted at row 0]"


def test_identical_rows_are_the_no_text_change_marker() -> None:
    prev = _rows(24)
    assert _diff_body(prev, list(prev)) == "[no text change]"


def test_a_three_line_scroll_names_three_rows_not_twenty_three() -> None:
    # The whole point of a sequence diff: an index-wise diff reports 23 of 24
    # rows changed here, which costs more than sending the whole screen.
    prev = _rows(24)
    cur = [*prev[3:], "new1", "new2", "new3"]
    assert _diff_body(prev, cur) == (
        "[3 rows deleted at row 0]\n"
        "[rows 21-23 inserted]\nnew1\nnew2\nnew3"
    )
