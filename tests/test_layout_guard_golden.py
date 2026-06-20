from __future__ import annotations

import json
import os
from pathlib import Path

from imp.engine.change import LAYOUT_VERSION, Change
from tests.conftest import _seed_control

_GOLDEN = Path(__file__).parent / "fixtures" / "layout_guard_golden.txt"


def _observe_layout(change_dir: Path) -> str:
    """Sorted on-disk paths + key-sets of change.json and state.json; values excluded."""
    files = sorted(
        str(p.relative_to(change_dir)).replace(os.sep, "/")
        for p in change_dir.rglob("*") if p.is_file()
    )
    change_keys = sorted(json.loads((change_dir / "change.json").read_text()))
    state_keys = sorted(json.loads((change_dir / "state" / "state.json").read_text()))
    lines = ["[paths]", *files,
             "[change.json]", *change_keys,
             "[state.json]", *state_keys]
    return "\n".join(lines) + "\n"


def test_observed_layout_matches_golden_and_version_is_paired(tmp_path: Path) -> None:
    header, _, body = _GOLDEN.read_text().partition("\n")
    golden_version = int(header.split("LAYOUT_VERSION:")[1].strip())

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="golden", base_branch="main")
    observed = _observe_layout(ch.change_dir)

    # structural drift fails until golden body and LAYOUT_VERSION constant are updated together
    assert observed == body
    assert golden_version == LAYOUT_VERSION
