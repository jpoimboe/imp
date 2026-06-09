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


# Lines in src that legitimately mention `/project`/`/scratch` — all NEGATIONS
# documenting the namespace's ABSENCE (Part A). Matched by stripped content so a
# line move doesn't break the allowlist; a NEW occurrence (not in this set) trips
# the guard and must be consciously added here (the whole point of the guard).
_SRC_ALLOWLIST = {
    "no virtual `/project`/`/scratch` namespace and no in-memory scratch store; system",
    "# `head`/`sed`. The path model is worktree-relative (Part A): no `/project`.",
    "host; `fs_permissions` patterns are worktree-relative (no `/project` namespace).",
}


def test_no_virtual_paths_in_python_source():
    """M2: the asset/description guards above miss `/project` drift in Python
    comments/docstrings (and any prompt string built in code). Scan src/imp/**/*.py
    too, allowlisting only the known negations that document the namespace's removal."""
    src_root = Path(__file__).resolve().parents[1] / "src" / "imp"
    offenders: list[str] = []
    for f in sorted(src_root.rglob("*.py")):
        for i, line in enumerate(f.read_text().splitlines(), start=1):
            if _FORBIDDEN.search(line) and line.strip() not in _SRC_ALLOWLIST:
                offenders.append(f"{f.relative_to(src_root)}:{i}: {line.strip()}")
    assert not offenders, (
        "virtual-path drift in Python source (add to _SRC_ALLOWLIST only if it is "
        "a deliberate negation documenting the namespace's absence):\n"
        + "\n".join(offenders)
    )


def test_git_escape_behavioral_test_is_not_skipped():
    """The git escape-vector test (M1/F-9) is the most security-load-bearing
    behavioral check. Fail loudly if it ever ships as a pytest.skip scaffold."""
    src = Path(__file__).parent.joinpath("test_sandbox_behavioral.py").read_text()
    i = src.index("def test_git_commit_works_but_escape_vectors_blocked")
    # window from the def to the next top-level def (or EOF)
    rest = src[i + 1:]
    j = rest.find("\ndef ")
    body = rest[: j if j != -1 else len(rest)]
    assert "pytest.skip(" not in body, (
        "the git escape-vector behavioral test must be implemented, not skipped"
    )
