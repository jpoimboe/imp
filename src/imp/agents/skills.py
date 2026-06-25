"""Skill resolution shared by the load_skill tool (agent path, spec §3) and the
interactive slash dispatcher (human path, spec §4).

Skills live in two audience-scoped subdirectories of <workflow_dir>/skills/:
  agent/  — disciplines loaded by subagents (load_agent_skill)
  user/   — skills surfaced to the human (load_user_skill / list_user_skills)

Agent path: resolves skills/agent/<name>.md, falling back to skills/user/<name>.md.
Human path: reads skills/user/ only; agent disciplines are never reachable here.
Each subdir applies the same name sanitization (reject empty / dot / traversal /
separator names). A missing subdir reads as not-found/empty, never a bare OSError.
"""

from __future__ import annotations

from pathlib import Path


class SkillError(Exception):
    """Base for every skill-resolution failure, so callers can catch the whole
    family with one `except SkillError` (the subclasses keep their own identity
    for callers that need to distinguish a bad name from a missing file)."""


class SkillNameError(SkillError, ValueError):
    """The requested skill name is malformed (empty, dot, traversal, separator)."""


class SkillNotFound(SkillError):
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


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """If `text` opens with a `---\n … \n---\n` block, return (description, body)
    with the block removed; otherwise (None, text). Only `description:` is read;
    every other field is ignored. The body is what reaches the conversation, so
    the metadata block must never survive in it."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", len("---\n"))
    if end == -1:
        return None, text  # unterminated block: treat the whole file as body
    block = text[len("---\n"):end]
    body = text[end + len("\n---\n"):]
    description: str | None = None
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("description:"):
            description = stripped[len("description:"):].strip()
            break
    return description, body


def load_agent_skill(skills_dir: Path, name: str) -> str:
    """Agent path: resolve <skills_dir>/agent/<name>.md, falling back to
    <skills_dir>/user/<name>.md. Strip any leading frontmatter from the body.
    Raises SkillNameError for a bad name, SkillNotFound if neither file exists."""
    for audience in ("agent", "user"):
        path = _safe_skill_path(skills_dir / audience, name)
        if path.is_file():
            return _split_frontmatter(path.read_text())[1]
    raise SkillNotFound(f"no such skill {name!r}")


def load_user_skill(skills_dir: Path, name: str) -> str:
    """Human path: resolve <skills_dir>/user/<name>.md ONLY (agent disciplines
    are never reachable here). Strip any leading frontmatter from the body."""
    path = _safe_skill_path(skills_dir / "user", name)
    if not path.is_file():
        raise SkillNotFound(f"no such skill {name!r}")
    return _split_frontmatter(path.read_text())[1]


def list_user_skills(skills_dir: Path) -> list[tuple[str, str | None]]:
    """Sorted (stem, description) pairs for <skills_dir>/user/*.md. description is
    None when the file carries no frontmatter description. Empty list if user/ is
    absent (a Change created before user skills existed)."""
    user = skills_dir / "user"
    if not user.is_dir():
        return []
    out: list[tuple[str, str | None]] = []
    for p in sorted(user.glob("*.md")):
        if p.is_file():
            description = _split_frontmatter(p.read_text())[0]
            out.append((p.stem, description))
    return out

