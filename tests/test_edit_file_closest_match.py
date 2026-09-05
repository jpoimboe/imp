# tests/test_edit_file_closest_match.py
"""Closest-region scan behind edit_file's not-found diagnostic: anchoring,
the 0.6 floor, tie-breaking, and the caps that bound its cost."""
from __future__ import annotations

import time

from imp.agents.tools.fs import _closest_region

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


def test_very_wide_window_is_skipped_unscored() -> None:
    # len(old) bounds the scan, but not the window's WIDTH IN CHARACTERS: a
    # file of a few very long lines makes each candidate window megabytes
    # wide, and quick_ratio() is linear in the window. Such a window can never
    # reach the floor (ratio() = 2*M/T with M <= len(old)), so it is skipped
    # before scoring. Unguarded this fixture measured 0.40 s; guarded, 0.01 s.
    long_line = "x" * 15000
    body = "\n".join(f"    anchor_{i} = compute({i})" if i % 8 == 0 else long_line
                     for i in range(320)) + "\n"
    old = "    anchor_0 = computeX(0)" + "\n" * 287
    assert body.count(old) == 0
    started = time.perf_counter()
    assert _closest_region(body, old) is None
    assert time.perf_counter() - started < 5.0


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
