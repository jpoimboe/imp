# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import importlib.resources as ir

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


def _md_to_fragment(markdown: str) -> str:
    """Convert imp-emitted Markdown to an inert HTML fragment.

    imp's own `<details>`/`<summary>` fold markers are the ONLY raw HTML in the
    stream: every Part 1 content path either HTML-escapes `<`/`>` or wraps text in
    a code fence, so a bare `<details>`/`</details>` line OUTSIDE a code fence is
    always imp's. Such folds are recognized structurally against markdown-it's own
    code-block map and re-emitted as real tags wrapping the separately-converted
    inner fragment; everything else goes through markdown-it with raw HTML disabled."""
    lines = markdown.split("\n")
    code = _code_lines(markdown)
    parts: list[str] = []
    buf: list[str] = []
    i = 0

    def flush() -> None:
        text = "\n".join(buf).strip("\n")
        buf.clear()
        if text:
            parts.append(_MD.render(text))

    while i < len(lines):
        line = lines[i]
        if i not in code and line == "<details>":
            flush()
            # Part 1's _details always emits `<summary>...</summary>` on the next
            # line, with the summary text already HTML-escaped (safe to re-emit).
            summary = lines[i + 1][len("<summary>"):-len("</summary>")]
            inner: list[str] = []
            depth = 0
            j = i + 2
            while j < len(lines):
                lj = lines[j]
                if j not in code:
                    if lj == "</details>":
                        if depth == 0:
                            break
                        depth -= 1
                    elif lj == "<details>":
                        depth += 1
                inner.append(lj)
                j += 1
            inner_html = _md_to_fragment("\n".join(inner).strip("\n"))
            parts.append(
                f"<details>\n<summary>{summary}</summary>\n{inner_html}</details>\n")
            i = j + 1
            continue
        buf.append(line)
        i += 1
    flush()
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
