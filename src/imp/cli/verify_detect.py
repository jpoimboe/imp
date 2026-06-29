"""Language-agnostic detection of a project's fast test command (suggestions only)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

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


def detect_test_command(project_root: Path) -> str | None:
    """Return a suggested fast test command for `project_root`, or None when
    nothing is detected. First probe that yields a non-empty command wins."""
    makefile = project_root / "Makefile"
    if makefile.is_file():
        cmd = _from_makefile(makefile.read_text(errors="ignore"))
        if cmd:
            return cmd
    pkg = project_root / "package.json"
    if pkg.is_file():
        cmd = _from_package_json(pkg.read_text(errors="ignore"))
        if cmd:
            return cmd
    if (project_root / "Cargo.toml").is_file():
        return "cargo clippy && cargo test"
    if (project_root / "go.mod").is_file():
        return "go vet ./... && go test ./..."
    justfile = project_root / "justfile"
    if justfile.is_file():
        cmd = _from_justfile(justfile.read_text(errors="ignore"))
        if cmd:
            return cmd
    return None


def resolve_test_cmd_choice(
    suggested: str,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> str:
    output_fn(
        f"Detected `{suggested}` — use it as your fast test command? [Y / edit / skip]"
    )
    ans = input_fn("").strip().lower()
    if ans in ("e", "edit"):
        entered = input_fn("fast test command: ").strip()
        return entered or suggested
    if ans in ("s", "skip", "n", "no"):
        return ""
    return suggested


def format_test_cmd_fast_yaml(command: str) -> str:
    return f"test_cmd_fast: {json.dumps(command)}\n"
