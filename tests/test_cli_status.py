from pathlib import Path

from imp.cli.main import _status_rows
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_status_lists_inflight_worktrees(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", change_id="alpha", base_branch="main")
    Change.create(tmp_path, title="B", change_id="beta", base_branch="main")
    rows = _status_rows(tmp_path)
    slugs = {r[0] for r in rows}
    assert slugs == {"alpha", "beta"}
