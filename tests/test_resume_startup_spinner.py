# tests/test_resume_startup_spinner.py
from __future__ import annotations

import re

import pytest

from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from tests._event_fixtures import _TTY, _two_phase_events, agent_text, ev, task_lead_started

_CSI = re.compile(r"\x1b\[([0-9?]*)([A-Za-z])")


class Screen:
    """Minimal ANSI screen model for the footer's cursor subset: printable text,
    \\n (down one row; the emulator also resets col to 0, harmless because the
    footer always emits \\r after \\n), \\r (col 0), \\x1b[K (clear-to-EOL at the
    cursor), and \\x1b[<n>A (cursor up n). SGR and cursor show/hide are ignored."""

    def __init__(self) -> None:
        self.lines: list[str] = [""]
        self.row = 0
        self.col = 0

    def _ensure_row(self) -> None:
        while self.row >= len(self.lines):
            self.lines.append("")

    def feed(self, data: str) -> None:
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                match = _CSI.match(data, i)
                if match is None:
                    i += 1
                    continue
                params, cmd = match.group(1), match.group(2)
                if cmd == "A":
                    n = int(params) if params.isdigit() else 1
                    self.row = max(0, self.row - n)
                elif cmd == "K":
                    self._ensure_row()
                    self.lines[self.row] = self.lines[self.row][: self.col]
                i = match.end()
                continue
            if ch == "\n":
                self.row += 1
                self.col = 0
                self._ensure_row()
            elif ch == "\r":
                self.col = 0
            else:
                self._ensure_row()
                line = self.lines[self.row]
                if self.col > len(line):
                    line = line + " " * (self.col - len(line))
                self.lines[self.row] = line[: self.col] + ch + line[self.col + 1:]
                self.col += 1
            i += 1


def _trimmed(screen: Screen) -> list[str]:
    lines = list(screen.lines)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _build(*, committed: str, preceded_by_blank: bool,
           startup_verb: str | None) -> tuple[LiveDriver, _TTY, LiveStatus]:
    out = _TTY()
    out.write(committed)
    status = LiveStatus(out)
    status._width_fn = lambda: 80
    drv = LiveDriver(stream=out, color=False, width=80, status=status,
                     suppress_replay=True, preceded_by_blank=preceded_by_blank,
                     startup_verb=startup_verb)
    return drv, out, status


def _shown_screen(*, committed: str, preceded_by_blank: bool) -> Screen:
    drv, out, status = _build(
        committed=committed, preceded_by_blank=preceded_by_blank,
        startup_verb="resuming")
    drv.install_startup_status()
    status._frame += 1
    status._draw()                                  # one spinner tick, still no live event
    screen = Screen()
    screen.feed(out.getvalue())
    return screen


def _replaced_screen(*, committed: str, preceded_by_blank: bool,
                     startup_verb: str | None = "resuming") -> Screen:
    drv, out, status = _build(
        committed=committed, preceded_by_blank=preceded_by_blank,
        startup_verb=startup_verb)
    drv.install_startup_status()
    status._frame += 1
    status._draw()
    events = _two_phase_events()
    marked = [{**e, "_replay": True} for e in events[:3]] + events[3:]
    for e in marked[:3]:                            # replayed prefix only
        drv.on_event(e)
    drv.on_event({"type": "replay_complete"})       # boundary lifts suppression
    for e in marked[3:]:                            # live continuation onward
        drv.on_event(e)
    drv.finish()                                    # clear the transient real footer
    screen = Screen()
    screen.feed(out.getvalue())
    return screen


def _spinner_index(lines: list[str]) -> int:
    # The bare-header committed line also contains "resuming" (the static header
    # word), so take the LAST match — the spinner is always the last drawn row.
    return max(i for i, ln in enumerate(lines) if "resuming" in ln)


def _block_index(lines: list[str]) -> int:
    return next(i for i, ln in enumerate(lines) if ln == "── plan phase ───")


# ---- while-shown: exactly one blank between content and the spinner --------

def test_while_shown_splash_one_blank_below_tagline() -> None:
    lines = _shown_screen(committed="tagline\n\n", preceded_by_blank=True).lines
    i = _spinner_index(lines)
    assert lines[i - 1] == ""            # exactly one blank
    assert lines[i - 2] == "tagline"     # ...directly below the committed content (not two)


def test_while_shown_inert_question_one_blank() -> None:
    lines = _shown_screen(committed="the pending question\n\n",
                          preceded_by_blank=True).lines
    i = _spinner_index(lines)
    assert lines[i - 1] == ""
    assert lines[i - 2] == "the pending question"


def test_while_shown_bare_header_one_blank() -> None:
    lines = _shown_screen(committed="feat · demo · resuming\n",
                          preceded_by_blank=False).lines
    i = _spinner_index(lines)
    assert lines[i - 1] == ""            # the footer owns this blank
    assert lines[i - 2] == "feat · demo · resuming"


# ---- after-replacement: committed-blank paths keep exactly one blank -------

def test_after_replacement_splash_keeps_one_blank() -> None:
    lines = _replaced_screen(committed="tagline\n\n", preceded_by_blank=True).lines
    i = _block_index(lines)
    assert lines[i - 1] == ""            # one blank preserved (NOT collapsed to zero)
    assert lines[i - 2] == "tagline"


def test_after_replacement_inert_keeps_one_blank() -> None:
    lines = _replaced_screen(committed="the pending question\n\n",
                             preceded_by_blank=True).lines
    i = _block_index(lines)
    assert lines[i - 1] == ""
    assert lines[i - 2] == "the pending question"


# ---- after-replacement: bare header matches today's non-startup layout -----

def test_after_replacement_bare_header_matches_no_startup_layout() -> None:
    with_startup = _replaced_screen(
        committed="feat · demo · resuming\n", preceded_by_blank=False,
        startup_verb="resuming")
    no_startup = _replaced_screen(
        committed="feat · demo · resuming\n", preceded_by_blank=False,
        startup_verb=None)
    assert _trimmed(with_startup) == _trimmed(no_startup)


# ---- echoed reply → spinner: one blank via the real echo seam --------------

def test_echoed_reply_then_spinner_one_blank() -> None:
    # Inert-resume wiring: no committed scrollback, footer owns its leading blank.
    drv, out, _status = _build(
        committed="", preceded_by_blank=False, startup_verb="resuming")
    # Drive the reply through the real echo seam (walks via _walk), not a
    # hardcoded committed string.
    drv.echo_submitted_reply("sandbox is off, go ahead and try")
    drv.install_startup_status()
    _status._frame += 1
    _status._draw()                                  # one spinner tick, no live event
    screen = Screen()
    screen.feed(out.getvalue())
    lines = screen.lines
    i = _spinner_index(lines)
    assert lines[i - 1] == ""                                        # the footer's own blank
    assert lines[i - 2].strip() == "> sandbox is off, go ahead and try"


# ---- resume tail: one blank below the header, one above the spinner --------

_TAIL_PROSE = "Latest prose before the pause."


def _tail_screen(*, committed: str, preceded_by_blank: bool) -> list[str]:
    drv, out, status = _build(
        committed=committed, preceded_by_blank=preceded_by_blank, startup_verb="resuming")
    drv.install_startup_status()
    for e in [ev("1", "quest_created", quest_id=1, title="t"), task_lead_started("2"),
              agent_text("3", _TAIL_PROSE)]:
        drv.on_event({**e, "_replay": True})
    drv.on_event({"type": "replay_complete"})
    status._frame += 1
    status._draw()
    screen = Screen()
    screen.feed(out.getvalue())
    return _trimmed(screen)


@pytest.mark.parametrize(("committed", "preceded_by_blank", "top"), [
    pytest.param("tagline\n\n", True, "tagline", id="splash"),
    pytest.param("feat · demo · resuming\n", False, "feat · demo · resuming",
                 id="bare-header"),
])
def test_tail_sits_one_blank_below_the_header_and_above_the_spinner(
    committed: str, preceded_by_blank: bool, top: str,
) -> None:
    lines = _tail_screen(committed=committed, preceded_by_blank=preceded_by_blank)
    assert lines[:4] == [top, "", f"• {_TAIL_PROSE}", ""]
    assert lines[4] != ""                           # the footer's activity row
