from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import jinja2
import yaml

from imp.assets import shipped_playbook_paths

FINDINGS_ONLY_DIRECTIVE = (
    "\n\n---\n\n"
    "**Findings-only mode:** Stop after producing and committing a findings "
    "document that records what you found. Do NOT implement any fixes in this "
    "change."
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

_ENV = jinja2.Environment(
    undefined=jinja2.StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
)


@dataclass(frozen=True)
class Playbook:
    name: str
    description: str
    body: str


def _project_dir(control: Path) -> Path:
    return control / "imp" / "playbooks"


def _playbook_paths(control: Path) -> list[Path]:
    project = _project_dir(control)
    if project.is_dir():
        local = sorted(project.glob("*.md"))
        if local:
            return local
    return shipped_playbook_paths()


def _load(path: Path) -> Playbook:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return Playbook(name=path.stem, description="", body=text)
    fm = yaml.safe_load(m.group(1)) or {}
    description = fm.get("description", "") if isinstance(fm, dict) else ""
    return Playbook(name=path.stem, description=str(description), body=m.group(2))


def list_playbooks(control: Path) -> list[Playbook]:
    return [_load(p) for p in _playbook_paths(control)]


def find_playbook(control: Path, name: str) -> Playbook | None:
    for pb in list_playbooks(control):
        if pb.name == name:
            return pb
    return None


def render_playbook(playbook: Playbook, args: str) -> str:
    return _ENV.from_string(playbook.body).render(args=args)
