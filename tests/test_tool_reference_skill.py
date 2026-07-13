from __future__ import annotations

from imp.agents.skills import load_agent_skill
from imp.assets import bundled_workflow_dir


def _skills_dir():
    return bundled_workflow_dir("imp") / "skills"


def test_tool_reference_loads_via_agent_path_without_frontmatter() -> None:
    body = load_agent_skill(_skills_dir(), "tool-reference")
    assert body.strip(), "tool-reference skill missing/empty"
    assert not body.lstrip().startswith("---"), "skill must carry no frontmatter"


def test_tool_reference_holds_relocated_reference_detail() -> None:
    body = load_agent_skill(_skills_dir(), "tool-reference")
    # One sentinel per tool whose description relocated detail here (AC 3):
    # every removed reference detail must still be present, only relocated.
    for sentinel in (
        "PNG/JPEG",              # read_file image handling
        "only the first 5 MB",   # read_file / edit_file caps
        "not valid UTF-8",       # edit_file refusal semantics
        "all-or-nothing",        # edit_file batch mechanics
        "DOTALL+MULTILINE",      # grep multiline
        "external-directory",    # grep walk cap
        "pathlib glob semantics",  # glob vs grep detail
        "most-recent-first",     # glob ordering
        "max_depth",             # symbol_outline
        "breadth-first thinning",  # symbol_outline
        "block-buffer",          # bash / bash_output buffering
        "out_path",              # bash background fields
        "err_path",
        "20-minute wait budget",  # bash_output budget/clip
        "advances the output cursors",  # bash_kill
        "comma-separated",       # imp_events type filter
        "durable working files",  # imp_events current-state nudge
    ):
        assert sentinel in body, f"tool-reference missing: {sentinel!r}"
