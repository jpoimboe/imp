"""commit_memory tool (loku06 spec Component 1).

The only sanctioned path for a top-level agent to land a commit on the shared
main checkout: writes and commits one imp/memory/ file under RepoGitLock, scoped
by an explicit single-file pathspec so unrelated staged/dirty state is excluded.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from imp.agents.tools import imp_tool
from imp.engine.worktree import RepoGitLock, _git, _git_ok


@imp_tool(
    name="commit_memory",
    description=(
        "Write and commit a single imp/memory/ file to the shared main "
        "checkout under imp's repo lock. `path` is POSIX and relative to "
        "imp/memory/ (e.g. 'all.md', 'imp/threshold.md'); `content` is the "
        "full final file text; `message` is the 'memory: <summary>' commit "
        "message you compose. Validates the target scope and refuses any path "
        "outside imp/memory/, writing nothing on rejection; the error text "
        "lists the valid workflow/step names so you can correct and retry."
    ),
)
def commit_memory(ctx: Any, path: str, content: str, message: str) -> str:
    from imp.engine.loader import load_workflow
    from imp.providers.config import parse_default_workflow, parse_project_facts
    from imp.providers.effective import memory_validation_scopes
    from imp.providers.memory import validate_memory_tree

    if ctx.quest is None:
        raise RuntimeError(
            "commit_memory is only available to top-level quest steps"
        )
    quest = ctx.quest
    control_root = quest.control_root

    pp = PurePosixPath(path)
    if (
        not path
        or not pp.parts
        or pp.is_absolute()
        or ".." in pp.parts
        or ".imp" in pp.parts
    ):
        return (
            f"commit_memory refuses {path!r}: path must be a relative location "
            "inside imp/memory/ (no absolute paths, no '..', never .imp/)"
        )

    with RepoGitLock(control_root).acquire():
        project_facts = parse_project_facts(control_root / "imp" / "project.yaml")
        # Use the frozen snapshot to avoid diza15 (live tree may have advanced past this loader).
        wf_name = quest.workflow or parse_default_workflow(
            control_root / "imp" / "project.yaml"
        )
        wf = load_workflow(
            quest.workflow_dir, project_keys=frozenset(project_facts)
        )
        preloaded_steps = {wf_name: frozenset(s.name for s in wf.flat_steps())}
        valid, workflow_steps = memory_validation_scopes(
            control_root, [path], project_facts, preloaded_steps=preloaded_steps
        )
        errors = validate_memory_tree(
            [path], valid_workflows=valid, workflow_steps=workflow_steps
        )
        if errors:
            return "\n".join(errors)

        relpath = f"imp/memory/{path}"
        target = control_root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git_ok(control_root, "add", "--", relpath)
        if _git(control_root, "diff", "--cached", "--quiet", "--", relpath).returncode == 0:
            branch = _git_ok(
                control_root, "rev-parse", "--abbrev-ref", "HEAD"
            ).strip()
            return f"{relpath} already up to date on {branch}; nothing to commit"
        _git_ok(control_root, "commit", "-m", message, "--", relpath)
        branch = _git_ok(
            control_root, "rev-parse", "--abbrev-ref", "HEAD"
        ).strip()
    return f"committed {relpath} on {branch}"
