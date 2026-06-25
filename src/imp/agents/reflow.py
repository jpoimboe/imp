"""Block-aware soft-wrap reflow for assembled system prompts.

Source markdown is hard-wrapped at ~80 columns for readable diffs, but those
soft-wrap newlines carry no meaning for the model. This collapses each prose
paragraph and each wrapped list-item continuation onto one line while leaving
structure exactly as authored: fenced code, pipe tables, ATX headings,
horizontal rules, blockquotes, blank-line runs, and list-marker boundaries.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_ATX_RE = re.compile(r"^\s*#{1,6}\s")
_HRULE_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_LIST_MARKER_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s")


def reflow_markdown(text: str) -> str:
    if not text:
        return ""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            i = _emit_fence(lines, i, fence.group(1), out)
            continue
        if line.strip() == "" or _is_standalone(line):
            out.append(line)
            i += 1
            continue
        marker = _LIST_MARKER_RE.match(line)
        if marker:
            i = _emit_list_item(lines, i, len(marker.group(1)), out)
            continue
        i = _emit_paragraph(lines, i, out)
    return "\n".join(out)


def _is_standalone(line: str) -> bool:
    stripped = line.lstrip()
    return bool(
        _ATX_RE.match(line)
        or _HRULE_RE.match(line)
        or stripped.startswith("|")
        or stripped.startswith(">")
    )


def _is_block_start(line: str) -> bool:
    return bool(
        line.strip() == ""
        or _FENCE_RE.match(line)
        or _is_standalone(line)
        or _LIST_MARKER_RE.match(line)
    )


def _suppresses_join(line: str) -> bool:
    # Markdown hard line break: a trailing backslash or two-or-more trailing spaces.
    return line.endswith("\\") or len(line) - len(line.rstrip(" ")) >= 2


def _emit_fence(lines: list[str], i: int, fence: str, out: list[str]) -> int:
    out.append(lines[i])
    i += 1
    n = len(lines)
    char = re.escape(fence[0])
    close = re.compile(rf"^\s*{char}{{{len(fence)},}}\s*$")
    while i < n:
        out.append(lines[i])
        closing = close.match(lines[i])
        i += 1
        if closing:
            break
    return i


def _emit_paragraph(lines: list[str], i: int, out: list[str]) -> int:
    # Suppression keys on the accumulated line's tail (which mirrors the last
    # joined source line's trailing whitespace, because continuations are
    # appended with lstrip only). A hard-break tail is preserved on emit so a
    # second pass re-derives the same line — universal idempotence.
    joined = lines[i]
    i += 1
    n = len(lines)
    while i < n and not _is_block_start(lines[i]) and not _suppresses_join(joined):
        joined = joined.rstrip() + " " + lines[i].lstrip()
        i += 1
    out.append(joined if _suppresses_join(joined) else joined.rstrip())
    return i


def _emit_list_item(lines: list[str], i: int, marker_col: int,
                    out: list[str]) -> int:
    joined = lines[i]
    i += 1
    n = len(lines)
    while i < n:
        nxt = lines[i]
        if _is_block_start(nxt) or _suppresses_join(joined):
            break
        if len(nxt) - len(nxt.lstrip()) <= marker_col:
            break
        joined = joined.rstrip() + " " + nxt.lstrip()
        i += 1
    out.append(joined if _suppresses_join(joined) else joined.rstrip())
    return i
