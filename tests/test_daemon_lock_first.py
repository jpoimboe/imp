from pathlib import Path

import anyio

from imp.engine.change import Change
from imp.engine.state import ProjectLock
from imp.remote.session.daemon import serve_change_session
from imp.remote.session.discover import session_socket_path
from tests.conftest import _seed_control


def test_spawn_against_held_lock_exits_without_opening_socket(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    sock = Path(session_socket_path(ch.control_root, ch.id))

    async def _scenario() -> object:
        # Simulate a RESIDENT change holding its ProjectLock; the wake-spawned
        # daemon must return harmlessly without binding the socket.
        with ProjectLock(ch.change_dir / "lock").acquire():
            return await serve_change_session(ch, resume=True)

    result = anyio.run(_scenario)
    assert result is None              # exited harmlessly (no LockHeld escaped)
    assert not sock.exists()           # never opened/clobbered the resident socket
