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
