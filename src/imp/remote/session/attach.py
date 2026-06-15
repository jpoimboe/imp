from __future__ import annotations

import os
import subprocess
import sys

from imp.engine.change import Change
from imp.engine.env import overlay_engine_env


def spawn_session_daemon(change: Change) -> None:
    argv = [
        sys.executable, "-m", "imp.remote.session.daemon",
        "--control-root", str(change.control_root),
        "--change-id", change.id,
    ]
    env = overlay_engine_env(os.environ, {})
    env.pop("IMP_REMOTE_CONTROL_SOCK", None)
    subprocess.Popen(
        argv,
        env=env,
        cwd=str(change.control_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
