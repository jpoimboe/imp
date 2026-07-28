"""Repo lint: state paths resolve through the seam, not through a literal
`<control_root>/.imp` composition."""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
SELF = Path(__file__).resolve()


def _hits(root: Path, needle: str) -> list[str]:
    return [f"{p.relative_to(root.parent)}:{n}"
            for p in sorted(root.rglob("*.py")) if p.resolve() != SELF
            for n, line in enumerate(p.read_text().splitlines(), 1)
            if needle in line]


def test_no_control_imp_reference_remains() -> None:
    assert _hits(SRC, "control_imp") == []
    assert _hits(TESTS, "control_imp") == []


# The only surviving `.imp` path literals in src/ are the imp home's own default
# and two worktree-relative paths: the per-quest home symlink and the nav-tool
# artifact carve-out that walks it.
_SRC_ALLOWED = {
    ('imp/paths.py',
     'base = os.environ.get("IMP_HOME") or str(Path.home() / ".imp")'),
    ('imp/engine/quest.py',
     'link = worktree_path / ".imp" / "quests" / quest_dir.name'),
    ('imp/agents/tools/nav.py',
     'for art in sorted((root / ".imp" / "quests").glob("*/artifacts")):'),
}


def test_src_builds_state_paths_through_the_seam() -> None:
    found = {(str(p.relative_to(SRC)), line.strip())
             for p in sorted(SRC.rglob("*.py")) if p.resolve() != SELF
             for line in p.read_text().splitlines()
             if '/ ".imp"' in line or 'Path(".imp")' in line}
    assert found == _SRC_ALLOWED
