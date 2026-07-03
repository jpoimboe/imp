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


def _fence_marker(line: str) -> str | None:
    """The backtick fence string when `line` is a code-fence delimiter, else None.
    Part 1 emits only symmetric all-backtick fences (>=3, length one greater than
    any backtick run inside), so a delimiter is a stripped line of only backticks."""
    s = line.strip()
    if len(s) >= 3 and set(s) == {"`"}:
        return s
    return None


def _md_to_fragment(markdown: str) -> str:
    """Convert imp-emitted Markdown to an inert HTML fragment.

    imp's own `<details>`/`<summary>` fold markers are the ONLY raw HTML in the
    stream: every Part 1 content path either HTML-escapes `<`/`>` or wraps text in
    a code fence, so a bare `<details>`/`</details>` line OUTSIDE a code fence is
    always imp's. Such folds are recognized structurally and re-emitted as real
    tags wrapping the separately-converted inner fragment; everything else goes
    through markdown-it with raw HTML disabled."""
    lines = markdown.split("\n")
    parts: list[str] = []
    buf: list[str] = []
    fence: str | None = None
    i = 0

    def flush() -> None:
        text = "\n".join(buf).strip("\n")
        buf.clear()
        if text:
            parts.append(_MD.render(text))

    while i < len(lines):
        line = lines[i]
        if fence is None and line == "<details>":
            flush()
            # Part 1's _details always emits `<summary>...</summary>` on the next
            # line, with the summary text already HTML-escaped (safe to re-emit).
            summary = lines[i + 1][len("<summary>"):-len("</summary>")]
            inner: list[str] = []
            inner_fence: str | None = None
            j = i + 2
            while j < len(lines):
                lj = lines[j]
                if inner_fence is None and lj == "</details>":
                    break
                marker = _fence_marker(lj)
                if inner_fence is None and marker:
                    inner_fence = marker
                elif inner_fence is not None and marker and len(marker) >= len(inner_fence):
                    inner_fence = None
                inner.append(lj)
                j += 1
            inner_html = _md_to_fragment("\n".join(inner).strip("\n"))
            parts.append(
                f"<details>\n<summary>{summary}</summary>\n{inner_html}</details>\n")
            i = j + 1
            continue
        marker = _fence_marker(line)
        if fence is None and marker:
            fence = marker
        elif fence is not None and marker and len(marker) >= len(fence):
            fence = None
        buf.append(line)
        i += 1
    flush()
    return "".join(parts)
