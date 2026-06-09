"""Line semantics shared by read_file and grep (tool-ergonomics spec).

`\\n`-ONLY splitting with `cat -n` counting rules, so the two tools' line
numbers always agree (cross-tool correlation is the whole point of numbering):

  - split on "\\n" only — NEVER str.splitlines(), whose extra break characters
    (\\v, \\f, \\x85, ...) would number lines differently from every
    \\n-counting consumer (editors, compilers, cat -n, the agent);
  - the trailing empty element a terminal \\n produces is NOT a line
    ("a\\nb\\n" has 2 lines, not 3);
  - a final unterminated line IS a line ("a\\nb" has 2 lines — wc -l says 1
    there, since it counts newline bytes);
  - an empty file has 0 lines;
  - a stray \\r stays as line content (CRLF files: regexes see the \\r —
    GNU grep / ripgrep parity; use \\r?$ to anchor on CRLF files).
"""

from __future__ import annotations

from pathlib import Path

from imp.agents.tools.limits import FILE_READ_CAP_BYTES, MATCH_LINE_CAP_BYTES


def split_lines(text: str, *, cap_truncated: bool) -> list[str]:
    """Split `text` per the counting rules above.

    With `cap_truncated` (the FILE is larger than the read cap) the trailing
    element is a partial fragment the cap cut mid-line — drop it: matching or
    rendering it would fabricate a line ("foo$" false-positives on a line the
    cap happened to cut at "...foo"). UNLESS the raw text ends in "\\n": then
    the trailing element is the phantom "" the split produced, and dropping
    one more would delete a complete, real final readable line.
    """
    if not text:
        return []
    parts = text.split("\n")
    if text.endswith("\n"):
        parts.pop()        # phantom "" — not a line
    elif cap_truncated:
        parts.pop()        # partial fragment cut by the read cap — not readable
    return parts


def file_is_cap_truncated(read_truncated: bool, path: Path) -> bool:
    """Cap-truncation is a property of the FILE, not of which limit fired.

    read_*_capped returns truncated=True for a file of EXACTLY cap_bytes even
    though nothing was dropped, so disambiguate with st_size sampled AFTER the
    read (a file that grows past the cap DURING the read — an appending log —
    must classify as truncated; a before-read sample would fabricate a final
    line and claim a false total).

    A file that VANISHED between read and stat: with read_truncated=False the
    read genuinely saw EOF — complete. With True the read did NOT see EOF —
    classify as cap-truncated (read_file uses readable-window vocabulary;
    edit_file refuses). Only an exactly-at-cap file that vanished mid-call is
    described over-conservatively — the right side to err on.
    """
    if not read_truncated:
        return False
    try:
        return path.stat().st_size > FILE_READ_CAP_BYTES
    except OSError:
        return True


def truncate_line(line: str) -> str:
    """Per-line content cap shared by both tools' rendering.

    MATCH_LINE_CAP_BYTES is bytes-NAMED but applied as a CHARACTER slice —
    today's grep behavior, kept deliberately (the spec calls it characters
    rather than claiming a byte budget it doesn't enforce)."""
    if len(line) > MATCH_LINE_CAP_BYTES:
        return line[:MATCH_LINE_CAP_BYTES] + "…[truncated]"
    return line
