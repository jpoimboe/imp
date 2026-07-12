"""Filesystem memory (imp/memory/): human-authored markdown standing
instructions, layered onto agents broad->narrow (global -> workflow -> step).

Pure helpers — no engine imports — so they are safe to call from config load,
validation, and the agent-invocation builder alike.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
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


def _applicable_keys(workflow: str | None, step: str | None) -> list[str]:
    """The snapshot keys that apply, broad->narrow. `all.md` always applies;
    the workflow and step layers apply only when identity is known (a subagent
    passes workflow=None and receives only global memory)."""
    keys = ["all.md"]
    if workflow is not None:
        keys.append(f"{workflow}/all.md")
        if step is not None:
            keys.append(f"{workflow}/{step}.md")
    return keys


def resolve_memory(
    snapshot: dict[str, str], *, workflow: str | None, step: str | None
) -> str:
    """Concatenate the applicable memory layers broad->narrow, joined by blank
    lines. Missing or blank layers contribute nothing; an all-blank result is
    the empty string (the caller emits no `## Project memory` section)."""
    parts: list[str] = []
    for key in _applicable_keys(workflow, step):
        content = snapshot.get(key, "").strip()
        if content:
            parts.append(content)
    return "\n\n".join(parts)


def validate_memory_tree(
    relpaths: Iterable[str],
    *,
    valid_workflows: Collection[str],
    workflow_steps: Mapping[str, Collection[str]],
) -> list[str]:
    """Check each memory relpath against the known workflows and steps. Returns
    a list of loud error messages, one per offending path (empty list == valid).

    `relpaths` are POSIX paths relative to `imp/memory/` (the snapshot keys).
    `valid_workflows` is every known workflow name; `workflow_steps` maps a
    workflow name to its valid step names (only workflows actually referenced by
    a subdir need be present). Content is irrelevant here — this validates the
    tree's SHAPE, so a comment-only file is judged only by its path.
    """
    errors: list[str] = []
    known = ", ".join(sorted(valid_workflows)) or "(none)"
    for rel in sorted(relpaths):
        parts = rel.split("/")
        if len(parts) == 1:
            if parts[0] != "all.md":
                errors.append(
                    f"memory/{rel}: only 'all.md' is allowed at the memory/ top "
                    f"level; nest a step file under its workflow, e.g. "
                    f"memory/<workflow>/{parts[0]}"
                )
        elif len(parts) == 2:
            workflow, filename = parts
            if workflow not in valid_workflows:
                errors.append(
                    f"memory/{rel}: '{workflow}' is not a known workflow; "
                    f"known workflows: {known}"
                )
                continue
            if filename == "all.md":
                continue
            step = filename[: -len(".md")]
            steps = workflow_steps.get(workflow, ())
            if step not in steps:
                valid_steps = ", ".join(sorted(steps)) or "(none)"
                errors.append(
                    f"memory/{rel}: '{step}' is not a step of workflow "
                    f"'{workflow}'; valid steps: {valid_steps}"
                )
        else:
            errors.append(
                f"memory/{rel}: memory files nest at most two levels "
                f"(memory/all.md, memory/<workflow>/all.md, "
                f"memory/<workflow>/<step>.md)"
            )
    return errors
