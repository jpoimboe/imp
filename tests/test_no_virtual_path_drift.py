"""Guard: no /project or /scratch virtual-path strings survive in the default
workflow assets OR in the FS/nav tool descriptions (akin to the cc-builtins
drift test). The path model is worktree-relative now (spec Part A)."""
from __future__ import annotations

import re
from pathlib import Path

from imp.assets import default_workflow_dir

# Match a ROOT-level virtual path `/project` or `/scratch`:
#   - trailing edge: word-boundary (not followed by an alnum/underscore/hyphen) —
#     so a bare `/project` at the end of a sentence is caught, while an unrelated
#     path like `/project-backup/` is NOT (the hyphen exclusion prevents that
#     false positive; a plain substring list would miss both distinctions).
#   - leading edge: NOT preceded by `~` or a word char — so the legitimate
#     home-relative example `~/project/...` in skills/systematic-debugging.md
#     (an unrelated string the spec says to leave) does NOT false-positive, while
#     `"/project`, `` `/project ``, ` /project`, and start-of-line `/project` do.
_FORBIDDEN = re.compile(r"(?<![~\w])/(?:project|scratch)(?![A-Za-z0-9_-])")


def test_no_virtual_paths_in_default_workflow_assets():
    root = Path(default_workflow_dir())
    offenders: list[str] = []
    for f in root.rglob("*"):
        if not f.is_file() or f.suffix not in (".md", ".yaml", ".yml"):
            continue
        for i, line in enumerate(f.read_text().splitlines(), start=1):
            if _FORBIDDEN.search(line):
                offenders.append(f"{f.relative_to(root)}:{i}: {line.strip()}")
    assert not offenders, "virtual-path drift:\n" + "\n".join(offenders)


def test_no_virtual_paths_in_tool_descriptions():
    import imp.agents.tools.fs
    import imp.agents.tools.nav  # noqa: F401
    from imp.agents.tools import get_registry

    reg = get_registry()
    offenders: list[str] = []
    for name in ("read_file", "write_file", "edit_file", "glob", "grep", "ls"):
        desc = reg[name].description
        if "/project" in desc or "/scratch" in desc:
            offenders.append(f"{name} description: {desc!r}")
    assert not offenders, "tool-description drift:\n" + "\n".join(offenders)
