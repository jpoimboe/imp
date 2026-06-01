"""Skill resolution shared by the load_skill tool (agent path, spec §3) and the
interactive slash dispatcher (human path, spec §4).

A skill is a flat markdown file at <workflow_dir>/skills/<name>.md. The name is
untrusted input: reject empty / dot / traversal / separator names, confine the
resolved real path to skills/, and fail closed. A missing skills/ dir reads as
not-found, never a bare OSError (a Change created before skills landed).
"""

from __future__ import annotations

from pathlib import Path


class SkillNameError(ValueError):
    """The requested skill name is malformed (empty, dot, traversal, separator)."""


class SkillNotFound(Exception):
    """No skill file exists for an otherwise-valid name (or skills/ is absent)."""


def _safe_skill_path(skills_dir: Path, name: str) -> Path:
    """Resolve <skills_dir>/<name>.md, rejecting any name that could escape
    skills_dir. Raises SkillNameError for a malformed name."""
    if not name.strip() or name.strip() in (".", ".."):
        raise SkillNameError(f"invalid skill name {name!r}")
    if name.startswith("/") or "/" in name or "\\" in name:
        raise SkillNameError(f"invalid skill name {name!r}")
    skills_root = skills_dir.resolve()
    candidate = (skills_root / f"{name}.md").resolve()
    if not candidate.is_relative_to(skills_root):
        raise SkillNameError(f"invalid skill name {name!r}")
    return candidate


def load_skill_body(skills_dir: Path, name: str) -> str:
    """Return the markdown body of skill <name>. Raises SkillNameError for a bad
    name, SkillNotFound if the file (or skills_dir) is missing."""
    path = _safe_skill_path(skills_dir, name)
    if not path.is_file():
        raise SkillNotFound(f"no such skill {name!r}")
    return path.read_text()


def list_skill_names(skills_dir: Path) -> list[str]:
    """Sorted stems of <skills_dir>/*.md. Empty list if the dir is absent."""
    if not skills_dir.is_dir():
        return []
    return sorted(p.stem for p in skills_dir.glob("*.md") if p.is_file())
