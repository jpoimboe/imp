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


def playbooks_dir() -> Path:
    """Real filesystem Path to the shipped playbook library; safe in editable and wheel installs."""
    resource = files("imp.assets").joinpath("playbooks")
    p = Path(str(resource))
    if not p.is_dir():
        raise RuntimeError(
            f"shipped playbooks not found at {p}; was the package installed "
            f"without its assets? Try `pip install -e .` from a checkout."
        )
    return p


def shipped_playbook_paths() -> list[Path]:
    return sorted(playbooks_dir().glob("*.md"))
