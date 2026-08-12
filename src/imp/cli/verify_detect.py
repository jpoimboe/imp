"""Language-agnostic detection of a project's fast test command (suggestions only)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal, NamedTuple


class DetectedTestCommand(NamedTuple):
    """A detected fast test command with the label the confirmation screen shows
    for the build file it was read from. NOT named `TestCommand`: pytest tries to
    collect any imported `Test*` class, and this project errors on warnings."""
    command: str
    provenance: str


# Make/just target or recipe header: `name:` at the start of a line.
_TARGET_RE = re.compile(r"^([A-Za-z0-9_.\-]+):", re.MULTILINE)


def _targets(text: str) -> set[str]:
    return set(_TARGET_RE.findall(text))


def _from_makefile(text: str) -> str:
    t = _targets(text)
    if "check" in t:
        return "make check"
    if "ci" in t:
        return "make ci"
    present = [name for name in ("lint", "typecheck", "test") if name in t]
    return " && ".join(f"make {name}" for name in present)


def _from_package_json(text: str) -> str:
    try:
        scripts = json.loads(text).get("scripts", {})
    except (json.JSONDecodeError, AttributeError):
        scripts = {}
    if "check" in scripts:
        return "npm run check"
    if "ci" in scripts:
        return "npm run ci"
    if "test" in scripts:
        return "npm test"
    return ""


def _from_justfile(text: str) -> str:
    t = _targets(text)
    if "check" in t:
        return "just check"
    if "test" in t:
        return "just test"
    return ""


def detect_test_command(project_root: Path) -> DetectedTestCommand | None:
    """Return a suggested fast test command for `project_root` and where it came
    from, or None when nothing is detected. First probe that yields a non-empty
    command wins."""
    makefile = project_root / "Makefile"
    if makefile.is_file():
        cmd = _from_makefile(makefile.read_text(errors="ignore"))
        if cmd:
            return DetectedTestCommand(cmd, "from Makefile")
    pkg = project_root / "package.json"
    if pkg.is_file():
        cmd = _from_package_json(pkg.read_text(errors="ignore"))
        if cmd:
            return DetectedTestCommand(cmd, "from package.json")
    if (project_root / "Cargo.toml").is_file():
        return DetectedTestCommand("cargo clippy && cargo test", "from Cargo.toml")
    if (project_root / "go.mod").is_file():
        return DetectedTestCommand("go vet ./... && go test ./...", "from go.mod")
    justfile = project_root / "justfile"
    if justfile.is_file():
        cmd = _from_justfile(justfile.read_text(errors="ignore"))
        if cmd:
            return DetectedTestCommand(cmd, "from justfile")
    return None


def format_test_cmd_fast_yaml(command: str | Literal[False]) -> str:
    """`json.dumps` spells both states YAML accepts: a quoted command, or `false`."""
    return f"test_cmd_fast: {json.dumps(command)}\n"
