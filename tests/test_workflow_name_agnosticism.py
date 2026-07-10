"""Guard: no distinctive workflow identifier is hardcoded in engine/cli/agents/providers."""
from __future__ import annotations

import ast
from pathlib import Path

import yaml

import imp
from imp.assets import bundled_workflow_dir

_PACKAGES = ("engine", "cli", "agents", "providers")


def _src_imp_root() -> Path:
    return Path(imp.__file__).resolve().parent


def _distinctive_workflow_names() -> set[str]:
    """Hyphen-containing phase/step/agent identifiers from the imp workflow."""
    wf_dir = bundled_workflow_dir("imp")
    doc = yaml.safe_load((wf_dir / "workflow.yaml").read_text(encoding="utf-8"))
    names: set[str] = set()
    for phase in doc.get("phases", []):
        names.add(phase["name"])
        for step in phase.get("steps", []):
            names.add(step["name"])
    for md in sorted((wf_dir / "agents").glob("*.md")):
        parts = md.read_text(encoding="utf-8").split("---", 2)
        if len(parts) < 3:
            continue
        meta = yaml.safe_load(parts[1]) or {}
        name = meta.get("name")
        if isinstance(name, str):
            names.add(name)
    return {n for n in names if isinstance(n, str) and "-" in n}


def _hardcoded_names(source: str, blocklist: set[str]) -> set[str]:
    """Blocklist identifiers that appear as an exact string literal in `source`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value in blocklist):
            found.add(node.value)
    return found


def test_no_distinctive_workflow_names_hardcoded_in_packages() -> None:
    blocklist = _distinctive_workflow_names()
    root = _src_imp_root()
    offenders: list[str] = []
    for pkg in _PACKAGES:
        for py in sorted((root / pkg).rglob("*.py")):
            for name in sorted(_hardcoded_names(py.read_text(encoding="utf-8"), blocklist)):
                offenders.append(f"{py.relative_to(root)}: {name!r}")
    assert not offenders, (
        "workflow identifiers hardcoded as string literals in "
        "engine/cli/agents/providers — move them to workflow.yaml/prompts:\n"
        + "\n".join(offenders))


def test_guard_blocklist_and_scanner_are_live() -> None:
    blocklist = _distinctive_workflow_names()
    # Distinctive workflow identifiers really are in the blocklist.
    assert {"plan-reviewer", "gap-check"} <= blocklist
    # The scanner flags a planted literal (proves the guard is not a tautology).
    assert _hardcoded_names('STEPS = {"plan-reviewer": 1}\n', blocklist) == {"plan-reviewer"}
    # A hyphenated non-workflow string is not flagged.
    assert _hardcoded_names('x = "not-a-step"\n', blocklist) == set()
