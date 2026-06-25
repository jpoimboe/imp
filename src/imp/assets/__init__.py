"""Static assets shipped with imp (workflows, agent definitions, scripts).

Resolved via importlib.resources so the bundled workflow is findable inside
both editable installs and wheel installs.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def is_bundled_workflow(name: str) -> bool:
    """True iff imp bundles a workflow named `name` (the 'managed' predicate)."""
    resource = files("imp.assets").joinpath("workflows", name)
    return Path(str(resource)).is_dir()


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
