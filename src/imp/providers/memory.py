"""Filesystem memory (imp/memory/): human-authored markdown standing
instructions, layered onto agents broad->narrow (global -> workflow -> step).

Pure helpers — no engine imports — so they are safe to call from config load,
validation, and the agent-invocation builder alike.
"""
from __future__ import annotations

import re
from pathlib import Path

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_html_comments(text: str) -> str:
    """Remove HTML comments (`<!-- ... -->`, including multi-line). `#` is a
    markdown heading, never a comment, and is left intact."""
    return _HTML_COMMENT_RE.sub("", text)


def load_memory_snapshot(memory_dir: Path) -> dict[str, str]:
    """Snapshot the whole `imp/memory/` tree: every `*.md` file keyed by its
    POSIX path relative to `memory_dir`, valued by its comment-stripped content.
    Absent directory -> empty snapshot. Comment-only files are kept (their value
    is blank after stripping); resolution skips blanks."""
    if not memory_dir.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(memory_dir.rglob("*.md")):
        if not path.is_file():
            continue
        rel = path.relative_to(memory_dir).as_posix()
        out[rel] = strip_html_comments(path.read_text(encoding="utf-8"))
    return out
