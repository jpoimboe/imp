# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import importlib.resources as ir
from dataclasses import dataclass

from markdown_it import MarkdownIt

# Content raw HTML is disabled so any untrusted `<script>`/tag in agent output or
# a tool result is escaped to visible, inert text. The table plugin is enabled so
# the summary path's GFM pipe tables render (CommonMark omits tables).
_MD = MarkdownIt("commonmark", {"html": False}).enable("table")


def _load_css() -> str:
    return (ir.files("imp") / "assets" / "github-markdown.css").read_text(
        encoding="utf-8")


def _code_lines(markdown: str) -> frozenset[int]:
    """Line numbers markdown-it itself treats as code. Asking the parser is what
    keeps the fold scan from desynchronizing with it — a tilde fence or an
    indented fence needs no rule of its own here."""
    return frozenset(
        n for tok in _MD.parse(markdown)
        if tok.map and tok.type in ("fence", "code_block")
        for n in range(tok.map[0], tok.map[1]))


@dataclass(frozen=True)
class _FoldOpen:
    line: int
    summary: str


@dataclass(frozen=True)
class _FoldClose:
    line: int


_SUMMARY_OPEN = "<summary>"
_SUMMARY_CLOSE = "</summary>"


def _fold_markers(markdown: str) -> list[_FoldOpen | _FoldClose]:
    """imp's fold markers in order: a `<details>` line outside code followed by a
    `<summary>…</summary>` line opens; a `</details>` line outside code closes the
    innermost open fold. Any other such line is ordinary text."""
    lines = markdown.split("\n")
    code = _code_lines(markdown)
    markers: list[_FoldOpen | _FoldClose] = []
    depth = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if i not in code:
            if (line == "<details>" and i + 1 < len(lines)
                    and lines[i + 1].startswith(_SUMMARY_OPEN)
                    and lines[i + 1].endswith(_SUMMARY_CLOSE)):
                summary = lines[i + 1][len(_SUMMARY_OPEN):-len(_SUMMARY_CLOSE)]
                markers.append(_FoldOpen(i, summary))
                depth += 1
                i += 2
                continue
            if line == "</details>" and depth:
                markers.append(_FoldClose(i))
                depth -= 1
        i += 1
    return markers


def _render_lines(lines: list[str]) -> str:
    text = "\n".join(lines).strip("\n")
    return _MD.render(text) if text else ""


def _md_to_fragment(markdown: str) -> str:
    """Convert imp-emitted Markdown to an inert HTML fragment.

    Content outside code is HTML-escaped and code carries verbatim text, and
    render_markdown only emits output whose fold markers match its fully escaped
    rendering, where every fold marker is imp's. Folds are recognized against
    markdown-it's own code-block map and re-emitted as real tags around their
    converted content; everything else goes through markdown-it with raw HTML
    disabled."""
    lines = markdown.split("\n")
    parts: list[str] = []
    pos = 0
    depth = 0
    for m in _fold_markers(markdown):
        parts.append(_render_lines(lines[pos:m.line]))
        if isinstance(m, _FoldOpen):
            # Part 1's _details emits the summary already HTML-escaped (safe to re-emit).
            parts.append(f"<details>\n<summary>{m.summary}</summary>\n")
            depth += 1
            pos = m.line + 2
        else:
            parts.append("</details>\n")
            depth -= 1
            pos = m.line + 1
    parts.append(_render_lines(lines[pos:]))
    parts.append("</details>\n" * depth)
    return "".join(parts)


def render_html(markdown: str) -> str:
    """Wrap the converted Markdown fragment in a single self-contained HTML page:
    the vendored github-markdown-css inlined into <style>, the body carrying the
    `.markdown-body` class it targets, and no external href/src reference."""
    fragment = _md_to_fragment(markdown)
    css = _load_css()
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<style>\n"
        f"{css}\n"
        ".markdown-body { box-sizing: border-box; min-width: 200px; "
        "max-width: 980px; margin: 0 auto; padding: 45px; }\n"
        ".markdown-body details details { margin-left: 1.5em; }\n"
        "</style>\n"
        "</head>\n"
        '<body class="markdown-body">\n'
        f"{fragment}"
        "</body>\n"
        "</html>\n"
    )
