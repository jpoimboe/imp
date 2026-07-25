"""Static assets shipped with imp (workflows, agent definitions, scripts).

Resolved via importlib.resources so the bundled workflow is findable inside
both editable installs and wheel installs.
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path


def is_bundled_workflow(name: str) -> bool:
    """True iff imp bundles a workflow named `name` (the 'managed' predicate)."""
    resource = files("imp.assets").joinpath("workflows", name)
    return Path(str(resource)).is_dir()


def engine_prompt(name: str) -> str:
    """Read one engine-owned universal prompt block from the bundled
    engine-prompts/ asset dir, dropping its leading {# … #} doc comment and the
    surrounding newlines so the value is byte-identical to the former inline
    literal."""
    text = (files("imp") / "assets" / "engine-prompts" / name).read_text(
        encoding="utf-8")
    text = re.sub(r"\A\{#.*?#\}", "", text, count=1, flags=re.DOTALL)
    return text.strip("\n")


def bundled_skills_dir() -> Path:
    """Return a real filesystem Path to the bundled global skills pool
    (src/imp/assets/skills/), mirroring bundled_workflow_dir. Raises if absent."""
    p = Path(str(files("imp.assets").joinpath("skills")))
    if not p.is_dir():
        raise RuntimeError(
            f"bundled skills pool not found at {p}; was the package installed "
            f"without its assets? Try `pip install -e .` from a checkout."
        )
    return p


def bundled_partials_dir() -> Path:
    """Return a real filesystem Path to the bundled shared-partials pool
    (src/imp/assets/partials/), mirroring bundled_skills_dir. Raises if absent."""
    p = Path(str(files("imp.assets").joinpath("partials")))
    if not p.is_dir():
        raise RuntimeError(
            f"bundled shared partials pool not found at {p}; was the package "
            f"installed without its assets? Try `pip install -e .` from a checkout."
        )
    return p


def bundled_docs_dir() -> Path:
    """Return a real filesystem Path to imp's repo-root docs/ corpus, resolved
    relative to the install (editable-from-a-clone), mirroring
    bundled_skills_dir. Read in place — never copied into a quest snapshot, so
    a threshold agent can grep it in any project. Raises if absent."""
    p = Path(str(files("imp.assets"))).parents[2] / "docs"
    if not p.is_dir():
        raise RuntimeError(
            f"imp docs corpus not found at {p}; was the package installed "
            f"without its repo checkout? Try `pip install -e .` from a checkout."
        )
    return p


def bundled_workflow_dir(name: str) -> Path:
    """Return a real filesystem Path to a bundled workflow by name.

    Editable installs return a real source path; wheel installs return a
    Traversable coerced to Path via str(). V0 supports only sources that resolve
    to real paths (no zipapp shipping); raise if the named workflow is absent.
    """
    p = Path(str(files("imp.assets").joinpath("workflows", name)))
    if not p.is_dir():
        raise RuntimeError(
            f"bundled workflow {name!r} not found at {p}; was the package "
            f"installed without its assets? Try `pip install -e .` from a checkout."
        )
    return p


def bundled_workflow_names() -> list[str]:
    """Sorted names of every bundled workflow (the 'managed' set)."""
    root = Path(str(files("imp.assets").joinpath("workflows")))
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def available_workflow_names(project_root: Path) -> list[str]:
    """Bundled names union committed imp/workflows/* names, sorted."""
    names = set(bundled_workflow_names())
    committed = project_root / "imp" / "workflows"
    if committed.is_dir():
        names |= {p.name for p in committed.iterdir() if p.is_dir()}
    return sorted(names)


INTERNAL_WORKFLOWS = frozenset({"memory"})


def selectable_workflow_names(project_root: Path) -> list[str]:
    """Available names minus imp's internal lanes — the set offered as a project
    default. `memory` exists and runs (`imp new --workflow memory --background`)
    but is never a sensible project default."""
    return [
        name
        for name in available_workflow_names(project_root)
        if name not in INTERNAL_WORKFLOWS
    ]


def resolve_workflow_dir(project_root: Path, name: str) -> Path:
    """Resolve a workflow name to a real dir: a committed imp/workflows/<name>
    (a fork, or the imp-repo dogfood symlink) wins; else the bundled workflow;
    else LoadError naming the valid set."""
    from imp.providers.config import validate_workflow_name
    if not validate_workflow_name(name):
        from imp.engine.errors import LoadError
        from imp.engine.types import SourceLoc
        raise LoadError(
            f"invalid workflow name {name!r}",
            SourceLoc(file=project_root / "imp" / "project.yaml", line=1),
        )
    committed = project_root / "imp" / "workflows" / name
    if committed.is_dir():
        return committed
    if is_bundled_workflow(name):
        return bundled_workflow_dir(name)
    from imp.engine.errors import LoadError
    from imp.engine.types import SourceLoc
    raise LoadError(
        f"unknown workflow {name!r}; available: "
        f"{', '.join(available_workflow_names(project_root))}",
        SourceLoc(file=project_root / "imp" / "project.yaml", line=1),
    )
