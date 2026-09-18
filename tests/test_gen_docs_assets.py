from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import gen_docs_assets as gen

_EVENTS = gen.demo_events()


def test_excerpt_carries_the_run_s_beats_in_order() -> None:
    text = gen.excerpt_text(_EVENTS)

    beats = [
        "$ imp log samo71",
        "> add a --json flag to imp list",
        "  … 279 events elided …",
        "• Settled, then. To read it back in one piece before I set it up:",
        "> ok",
        "  … 2,397 events elided …",
        "• I walked all twelve acceptance criteria clause by clause against the committed",
        "  … 459 events elided …",
        "  Open items. None. All three recorded followups were resolved during the run",
        "Merged samo71 into master.",
    ]
    positions = [text.index(beat) for beat in beats]

    assert positions == sorted(positions)


def test_excerpt_selection_excludes_the_two_width_independent_wide_rows() -> None:
    lines = gen.excerpt_text(_EVENTS).splitlines()

    assert max(len(line) for line in lines) <= 80
    # Both 81-column rows are plan 2's review steps, inside the elided stretch.
    assert not [line for line in lines if "reviewing plan · complete" in line]


def test_transcript_is_the_whole_run_and_keeps_its_two_wide_rows() -> None:
    body = (gen.transcript_text(_EVENTS)
            .split("```console\n")[1].removesuffix("```\n").splitlines())

    assert body[0] == "$ imp log samo71"
    assert body[1] == "> add a --json flag to imp list"
    assert body[-1] == "Merged samo71 into master."
    assert len(body) == 1553          # the command line plus 1552 rendered lines
    # A linked docs file, not a README block: the 80-column rule does not apply.
    assert len([line for line in body if len(line) > 80]) == 2


def test_ledger_cut_elides_the_middle_and_ends_on_the_run_s_real_total() -> None:
    lines = gen.ledger_text(_EVENTS).splitlines()
    body = lines[2:-1]                      # inside the fence, past the command line

    assert lines[1] == "$ imp log samo71 --summary"
    assert len(body) <= 45                  # the full render is 85 rows
    assert body[0] == "default  ·  samo71  ·  merged"
    assert "  … 33 rows elided …" in body
    # The total is the WHOLE run's, not the surviving rows': 2h 6m / 28.0M / $42.20.
    assert body[-1] == (
        "  total                                 2h 6m   28.0M  531.1k  [$42.20]")
    assert max(len(line) for line in body) <= 80


def test_ledger_cut_keeps_the_first_cycle_s_coder_reviewer_pairs() -> None:
    body = gen.ledger_text(_EVENTS).splitlines()

    assert sum(1 for line in body if "↳ coder (writing code)" in line) == 3
    assert sum(1 for line in body if "↳ code-reviewer (reviewing code)" in line) == 3
    # The hero frame's `↳ coder · 47s` is this row's own timing (AC 7b).
    assert ("    ↳ coder (writing code)                47s  149.6k    2.9k   [$0.19]"
            in body)


def test_width_guard_fails_the_build_on_an_over_wide_block() -> None:
    with pytest.raises(SystemExit) as excinfo:
        gen.check_width("probe.md", "fits\n" + "x" * 81 + "\n")

    assert excinfo.value.code
    assert "81" in str(excinfo.value)


def test_generated_assets_are_plain_terminal_text() -> None:
    blob = "".join(gen.text_assets(_EVENTS).values())

    assert "\x1b" not in blob
    assert "<details>" not in blob
    assert "quota_reading" not in blob
    assert "token_refresh" not in blob
    assert not [line for line in blob.splitlines()
                if re.fullmatch(r"\s*\|.*\|\s*", line)]


def test_no_elapsed_reading_exceeds_the_run_s_work_time() -> None:
    blob = "".join(gen.text_assets(_EVENTS).values())

    readings = [(int(h), int(m)) for h, m in re.findall(r"\b(\d+)h (\d+)m\b", blob)]

    assert readings                                  # the total row reads 2h 6m
    assert all(hours * 60 + minutes <= 126 for hours, minutes in readings)


def test_write_text_assets_replaces_the_three_named_assets_in_place(tmp_path: Path) -> None:
    first = {p.name: p.read_bytes() for p in gen.write_text_assets(_EVENTS, tmp_path)}
    second = {p.name: p.read_bytes() for p in gen.write_text_assets(_EVENTS, tmp_path)}

    assert sorted(first) == ["samo71-excerpt.md", "samo71-ledger.md", "samo71.md"]
    assert first == second
