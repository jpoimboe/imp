# tests/test_edit_file_closest_match.py
"""edit_file's not-found diagnostic: the closest-region scan (anchoring, the
0.6 floor, tie-breaking, the caps that bound its cost), the rendered
want:/file: message, and the cause named in _short_repr's docstring."""
from __future__ import annotations

import difflib
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import imp.agents.tools.fs as fs
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.fs import _apply_edit, _closest_region, _short_repr


@pytest.fixture
def scored_windows(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []
    class _Recording(difflib.SequenceMatcher):
        _active = False
        def set_seq2(self, b: str) -> None:
            if b:  # real seq2 set (not the empty-string __init__ call)
                self._active = True
            super().set_seq2(b)
        def set_seq1(self, a: str) -> None:
            if self._active:
                seen.append(len(a))
            super().set_seq1(a)
    monkeypatch.setattr(fs, "difflib", SimpleNamespace(SequenceMatcher=_Recording))
    return seen


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl",
    )
    ctx.coordinator = None
    return ctx


def _edit(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["edit_file"].async_invoke, ctx, args)


def _not_found_message(body: str, old: str, path: str = "x.py",
                       label: str = "") -> str:
    # Guard the fixture itself: _apply_edit raises on plain substring absence,
    # so an `old` that IS contained never reaches the not-found branch and
    # would make the assertions below vacuous.
    assert body.count(old) == 0, "fixture is not a miss: old is contained"
    with pytest.raises(ValueError) as exc:
        _apply_edit(body, old, "NEW", path, False, label=label)
    return str(exc.value)


# The spec's motivating real-world case: `old` differs from the file line only
# in interior whitespace, so it is genuinely absent (not a substring).
RV_BODY = "do_run() {\n\tif ((rc == 124)); then\n\t\tretry\n\tfi\n}\n"
RV_OLD = "if (( rc == 124 )); then"


def test_interior_whitespace_drift_is_located() -> None:
    assert RV_BODY.count(RV_OLD) == 0  # a genuine miss, not containment
    assert _closest_region(RV_BODY, RV_OLD) == (1, ["\tif ((rc == 124)); then"])


def test_over_indented_old_is_located() -> None:
    body = "def run():\n    if rc == 124:\n        retry()\n"
    old = "        if rc == 124:"
    assert body.count(old) == 0
    assert _closest_region(body, old) == (1, ["    if rc == 124:"])


def test_multiline_content_drift_over_200_chars_is_located() -> None:
    # difflib's DEFAULT autojunk heuristic discards any character appearing in
    # more than 1% of a 200+-character sequence, which drags this window's
    # ratio to 0.514 — under the floor — and yields NO diagnostic for exactly
    # the content-drift case the feature exists for. autojunk=False scores it
    # 0.857. This test fails if the heuristic is left on.
    body = "\n".join(f"    value_{i} = compute({i})" for i in range(300)) + "\n"
    old = "\n".join(f"    value_{i} = compute({i})  # drift"
                    for i in range(40, 80))
    assert len(old) > 200
    region = _closest_region(body, old)
    assert region is not None
    start, window = region
    assert start == 40
    assert window[0] == "    value_40 = compute(40)"
    assert len(window) == 40


def test_score_exactly_at_floor_is_reported() -> None:
    # ratio() == 0.6 exactly: the boundary is inclusive.
    assert _closest_region("ANCHO\nabcd\n", "ANCHO\nwxyz") == (0, ["ANCHO", "abcd"])


def test_below_floor_region_is_declined() -> None:
    # Same anchor line, but the second line is long enough to drop the window
    # to 0.462 — under the floor, so no region at all.
    assert _closest_region("ANCHO\nabcdefghij\n", "ANCHO\nwxyz") is None


def test_absent_string_anchors_nothing() -> None:
    assert _closest_region("alpha\nbeta\n", "ZZZ") is None


def test_tie_reports_the_earlier_region() -> None:
    # Both windows score identically; the earlier start wins.
    body = "alpha\nbeta\nMID\nalpha\nbeta\n"
    assert _closest_region(body, "alpha\nBETA") == (0, ["alpha", "beta"])


def test_file_shorter_than_old_is_one_whole_file_candidate() -> None:
    # split("\n") gives a trailing "" for the final newline; the window is
    # simply everything there is.
    assert _closest_region("alpha\nbeta\n", "alpha\nbeta\ngamma\ndelta") == (
        0, ["alpha", "beta", ""])


def test_single_line_interior_drift_uses_the_prefix_fallback() -> None:
    # No whitespace-stripped line is shared with the file, so the exact-key
    # pass finds nothing; the 12-character prefix key still anchors it.
    body = "class C:\n        self.name = name.strip()\n        return None\n"
    old = "        self.name = nameX.strip(True)"
    assert body.count(old) == 0
    assert _closest_region(body, old) == (
        1, ["        self.name = name.strip()"])


def test_over_common_anchor_key_is_skipped() -> None:
    # 65 copies of the same line: every anchor key exceeds the 64-hit cap, so
    # the key locates nothing and the scan declines.
    body = "\n".join(["    value = compute(x)"] * 65) + "\n"
    assert _closest_region(body, "    value = compute(y)") is None


def test_at_the_anchor_hit_cap_still_anchors() -> None:
    # 64 copies is exactly at the cap, which is inclusive.
    body = "\n".join(["    value = compute(x)"] * 64) + "\n"
    assert _closest_region(body, "    value = compute(y)") == (
        0, ["    value = compute(x)"])


def test_very_wide_window_is_skipped_unscored(scored_windows: list[int]) -> None:
    # Each candidate window is megabytes wide (long_line * width). Such a
    # window is skipped before scoring: ratio() = 2*M/T with M <= len(old),
    # so an over-wide window cannot reach the floor however well it matches.
    long_line = "x" * 15000
    body = "\n".join(f"    anchor_{i} = compute({i})" if i % 8 == 0 else long_line
                     for i in range(320)) + "\n"
    old = "    anchor_0 = computeX(0)" + "\n" * 287
    assert body.count(old) == 0
    assert _closest_region(body, old) is None
    assert scored_windows == []  # width guard skipped every candidate before set_seq1


def test_old_over_the_char_cap_is_skipped_even_when_it_would_anchor() -> None:
    # Both halves are drawn from the file itself, so both anchor perfectly;
    # only the character cap separates them. The `under` half is kept small
    # (699 chars, ~0.01 s) — a cap check needs no expensive fixture.
    body = "\n".join(f"    value_{i} = compute({i})" for i in range(300)) + "\n"
    under = "\n".join(f"    value_{i} = compute({i})  # drift" for i in range(20))
    over = "\n".join(f"    value_{i} = compute({i})  # drift" for i in range(60))
    assert len(under) < 2000 < len(over)
    assert _closest_region(body, under) == (0, body.split("\n")[0:20])
    assert _closest_region(body, over) is None


def test_message_appends_the_diff_after_todays_prefix() -> None:
    # Reproduces the spec's worked example byte-for-byte.
    assert _not_found_message(RV_BODY, RV_OLD, path="rv.sh") == (
        "'if (( rc == 124 )); then' not found in 'rv.sh'"
        " — closest match at line 2:\n"
        "    want: 'if (( rc == 124 )); then'\n"
        "    file: '\\tif ((rc == 124)); then'"
    )


def test_multiline_old_reports_the_window_start_line() -> None:
    body = "alpha\nbeta\ngamma\ndelta\nepsilon\n"
    assert _not_found_message(body, "beta\nGAMMA\ndelta") == (
        "'beta\\nGAMMA\\ndelta' not found in 'x.py' — closest match at line 2:\n"
        "    want: 'GAMMA'\n"
        "    file: 'gamma'"
    )


def test_identical_lines_inside_the_window_are_omitted() -> None:
    body = "def f():\n    a = 1\n    b = 2\n    c = 3\n    return a\n"
    old = "def F():\n    a = 1\n    b = 2\n    c = 3\n    return A\n"
    msg = _not_found_message(body, old)
    assert msg.endswith(
        " — closest match at line 1:\n"
        "    want: 'def F():'\n"
        "    file: 'def f():'\n"
        "    want: '    return A'\n"
        "    file: '    return a'"
    )
    # Scoped to the diff: the message's `old` repr prefix names them too.
    tail = msg.split("closest match")[1]
    assert "a = 1" not in tail
    assert "b = 2" not in tail
    assert "c = 3" not in tail


def test_more_than_six_differing_lines_are_capped_with_a_tail() -> None:
    body = "\n".join(f"line {i} of the file" for i in range(12)) + "\n"
    old = "\n".join(
        (f"line {i} of the file" if i in (2, 3) else f"line {i} of the FILE")
        for i in range(2, 12))
    msg = _not_found_message(body, old)
    assert msg.endswith(
        " — closest match at line 3:\n"
        "    want: 'line 4 of the FILE'\n"
        "    file: 'line 4 of the file'\n"
        "    want: 'line 5 of the FILE'\n"
        "    file: 'line 5 of the file'\n"
        "    want: 'line 6 of the FILE'\n"
        "    file: 'line 6 of the file'\n"
        "    want: 'line 7 of the FILE'\n"
        "    file: 'line 7 of the file'\n"
        "    want: 'line 8 of the FILE'\n"
        "    file: 'line 8 of the file'\n"
        "    want: 'line 9 of the FILE'\n"
        "    file: 'line 9 of the file'\n"
        "    … 2 more lines differ"
    )
    assert "line 10" not in msg.split("closest match")[1]


def test_trailing_whitespace_difference_is_visible() -> None:
    # Raw text would print two identical-looking lines; _short_repr's escaping
    # is what makes the difference legible.
    msg = _not_found_message("alpha\nbeta \ngamma\n", "alpha\nbeta\ngamma")
    assert msg.endswith(
        " — closest match at line 1:\n"
        "    want: 'beta'\n"
        "    file: 'beta '"
    )


def test_tab_versus_spaces_difference_is_visible() -> None:
    msg = _not_found_message("if x:\n\treturn 1\n", "if x:\n    return 1")
    assert msg.endswith(
        " — closest match at line 1:\n"
        "    want: '    return 1'\n"
        "    file: '\\treturn 1'"
    )


def test_displayed_lines_obey_the_short_repr_cap() -> None:
    body = "head\n" + "x" * 260 + "\ntail\n"
    old = "head\ny" + "x" * 259 + "\ntail"
    msg = _not_found_message(body, old)
    assert " — closest match at line 1:" in msg
    assert msg.count("[260 chars total]") == 2
    assert "x" * 201 not in msg.split("closest match")[1]


def test_missing_line_on_one_side_renders_as_empty_string() -> None:
    msg = _not_found_message("alpha\nbeta\n", "alpha\nbeta\ngamma\ndelta")
    assert msg.endswith(
        " — closest match at line 1:\n"
        "    want: 'gamma'\n"
        "    file: ''\n"
        "    want: 'delta'\n"
        "    file: ''"
    )


def test_no_near_miss_keeps_todays_message_byte_for_byte() -> None:
    assert _not_found_message("content\n", "nope\nx") == (
        "'nope\\nx' not found in 'x.py'")


def test_batch_label_still_prefixes_the_diagnostic() -> None:
    assert _not_found_message(RV_BODY, RV_OLD, path="rv.sh",
                              label="edit #2: ") == (
        "edit #2: 'if (( rc == 124 )); then' not found in 'rv.sh'"
        " — closest match at line 2:\n"
        "    want: 'if (( rc == 124 )); then'\n"
        "    file: '\\tif ((rc == 124)); then'"
    )


def test_batch_failure_with_diagnostic_writes_nothing(tmp_path: Path) -> None:
    # edit #1 applies to the in-memory body, edit #2 genuinely misses; the
    # batch stays all-or-nothing, so the file on disk is untouched.
    p = tmp_path / "rv.sh"
    p.write_text(RV_BODY)
    before = p.read_bytes()
    with pytest.raises(ValueError) as exc:
        _edit(_ctx(tmp_path), path="rv.sh", edits=[
            {"old": "do_run", "new": "DO_RUN"},
            {"old": RV_OLD, "new": "Z"},
        ])
    msg = str(exc.value)
    assert msg.startswith("edit #2: ")
    assert " — closest match at line 2:" in msg
    assert p.read_bytes() == before


def test_edit_file_gains_no_new_parameter() -> None:
    schema = get_registry()["edit_file"].input_schema
    assert set(schema["properties"]) == {
        "path", "old", "new", "replace_all", "edits"}


def test_large_file_with_large_old_raises_promptly() -> None:
    # Both dimensions pinned: 20k lines AND a 40-line old. The rejected
    # exhaustive slide takes tens of seconds here; the anchored scan measured
    # 0.31 s. The 5 s budget cannot false-fire under a saturated CI.
    body = "".join(f"    value_{i} = compute({i})\n" for i in range(20000))
    old = "\n".join(f"    value_{i} = compute({i})  # drift"
                    for i in range(9000, 9040))
    started = time.perf_counter()
    msg = _not_found_message(body, old, path="big.py")
    assert time.perf_counter() - started < 5.0
    assert " — closest match at line 9001:" in msg
    assert "    file: '    value_9000 = compute(9000)'" in msg


def test_worst_case_in_cap_old_stays_bounded() -> None:
    # THE ceiling: an `old` at the character cap, drifting on every line, so
    # every line must be compared and quick_ratio() prunes nothing. This is
    # the most expensive input the caps admit — measured 0.57 s. A 4000-char
    # `old` (the cap before it was calibrated against autojunk=False) measured
    # 4.6 s on this same fixture shape, which is why the cap is 2000.
    body = "".join(f"    value_{i} = compute({i})\n" for i in range(20000))
    old = "\n".join(f"    value_{i} = compute({i})  # drift"
                    for i in range(9000, 9050))
    assert 1900 < len(old) <= 2000  # at the cap, not merely near it
    started = time.perf_counter()
    msg = _not_found_message(body, old, path="big.py")
    assert time.perf_counter() - started < 5.0
    assert " — closest match at line 9001:" in msg


def test_many_near_identical_regions_stay_bounded(scored_windows: list[int]) -> None:
    # An `old` whose character multiset matches 40 file regions, so
    # quick_ratio() prunes nothing between candidates. With len(old)==2000,
    # keep=max(1,min(32,2_500_000//2000**2))==1, so the budget holds this to a
    # single scored candidate; the count is what pins the budget guard.
    block = [f"    value_{i} = compute(alpha, beta, gamma, delta)  # step {i}"
             for i in range(160)]
    filler = [f"    unique_line_{j} = do_something({j})" for j in range(20000)]
    lines: list[str] = []
    cursor = 0
    per = (20000 - 40 * len(block)) // 40
    for _ in range(40):
        lines.extend(block)
        lines.extend(filler[cursor:cursor + per])
        cursor += per
    body = "\n".join(lines[:20000]) + "\n"
    old = "\n".join(reversed(block[:60]))[:2000]
    started = time.perf_counter()
    _not_found_message(body, old, path="big.py")
    assert time.perf_counter() - started < 5.0
    assert len(scored_windows) == 1  # exactly one candidate scored


def test_old_over_the_char_cap_degrades_to_todays_message() -> None:
    body = "".join(f"    value_{i} = compute({i})\n" for i in range(20000))
    old = "z" * 2001
    started = time.perf_counter()
    msg = _not_found_message(body, old, path="big.py")
    assert time.perf_counter() - started < 5.0
    assert msg == f"{'z' * 200!r}… [2001 chars total] not found in 'big.py'"


def test_all_anchors_over_the_hit_cap_degrades_to_todays_message() -> None:
    body = "\n".join(["        self.name = name.strip()"] * 100000) + "\n"
    started = time.perf_counter()
    msg = _not_found_message(body, "        self.name = nameX.strip()",
                             path="big.py")
    assert time.perf_counter() - started < 5.0
    assert msg == (
        "'        self.name = nameX.strip()' not found in 'big.py'")


def test_short_repr_docstring_names_content_drift_not_whitespace() -> None:
    # The archive refutes the old claim (1 of 12 sampled misses were
    # whitespace); the docstring must not keep asserting it.
    doc = _short_repr.__doc__
    assert doc is not None
    assert "content drift" in doc
    assert "whitespace" not in doc
