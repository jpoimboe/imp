from __future__ import annotations

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
