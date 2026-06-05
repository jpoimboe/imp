"""Load/save fixtures as data (directory-per-fixture). The diff is stored in a
separate `diff.patch` file (it can be large and is awkward inside JSON); the rest
of the fixture is `fixture.json`. Fixtures do not import the harness or grader."""

from __future__ import annotations

import json
from pathlib import Path

from imp.evals.models import Fixture


def save_fixture(fixture: Fixture, root: Path) -> Path:
    """Write `<root>/<id>/{fixture.json, diff.patch}`. Returns the fixture dir."""
    fixture_dir = root / fixture.id
    fixture_dir.mkdir(parents=True, exist_ok=True)
    d = fixture.to_dict()
    diff = d.pop("diff")
    (fixture_dir / "fixture.json").write_text(json.dumps(d, indent=2) + "\n")
    (fixture_dir / "diff.patch").write_text(diff)
    return fixture_dir


def load_fixture(fixture_dir: Path) -> Fixture:
    d = json.loads((fixture_dir / "fixture.json").read_text())
    d["diff"] = (fixture_dir / "diff.patch").read_text()
    return Fixture.from_dict(d)


def load_fixtures(root: Path) -> list[Fixture]:
    """Load every fixture directory under `root` (a dir with a fixture.json)."""
    out: list[Fixture] = []
    for child in sorted(root.iterdir()):
        if (child / "fixture.json").exists():
            out.append(load_fixture(child))
    return out


def check_cross_task_stratum(fixtures: list[Fixture]) -> list[str]:
    """Lint (M2): any finding with cross_task=True implies its fixture is
    stratum=="cross-cutting". Implication only — a cross-cutting fixture need NOT
    carry a cross_task finding, so a cross-cutting fixture without one is fine.
    Returns a list of violation messages (empty == clean)."""
    violations: list[str] = []
    for fx in fixtures:
        if any(ef.cross_task for ef in fx.expected_findings) and fx.meta.stratum != "cross-cutting":
            violations.append(
                f"{fx.id}: has a cross_task finding but stratum={fx.meta.stratum!r} "
                f"(must be 'cross-cutting')"
            )
    return violations
