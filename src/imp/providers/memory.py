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
