"""Static assets shipped with imp (workflows, agent definitions, scripts).

Resolved via importlib.resources so the default workflow is findable inside
both editable installs and wheel installs.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def default_workflow_dir() -> Path:
    """Return a real filesystem Path to the shipped default workflow.

    Editable installs return a real source path; wheel installs return a
    Traversable that we coerce to Path via str(). For V0 we only support
    sources that resolve to real paths (no zipapp shipping); raise if not.
    """
    resource = files("imp.assets").joinpath("workflows", "default")
    p = Path(str(resource))
    if not p.is_dir():
        raise RuntimeError(
            f"default workflow not found at {p}; was the package installed "
            f"without its assets? Try `pip install -e .` from a checkout."
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
