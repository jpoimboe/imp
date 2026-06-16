from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from tests.conftest import _seed_control

_CHILD = textwrap.dedent(
    """
    import anyio, argparse, sys
    from pathlib import Path
    import imp.cli.main as m
    from imp.engine.change import Change
    from imp.engine.executor import RunResult
    from imp.engine.state import State

    ch = Change.create(Path("."), title="demo", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)

    async def fake_run_change(change, **kw):
        on_event = kw["on_event"]
        on_event({"id": "1", "agent_id": None, "type": "change_created",
                  "payload": {"slug": change.slug, "title": change.title}})
        on_event({"id": "2", "agent_id": None, "type": "step_started",
                  "payload": {"step": "s", "kind": "agent", "phase": "design",
                              "model": "x", "provider": "anthropic"}})
        return RunResult.COMPLETED

    m.run_change = fake_run_change
    args = argparse.Namespace(token="feat", from_target=None, force=False, tui=True)
    sys.exit(anyio.run(m._async_cmd_resume, args))
    """
)


def test_subprocess_resume_tui_on_non_tty_warns_and_streams(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("IMP_")}
    env.setdefault("ANTHROPIC_API_KEY", "sk-test")  # _preflight_imp_init checks this
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    # stdout is a PIPE -> isatty() False -> the non-tty --tui fallback path.
    assert "interactive terminal" in proc.stderr      # warning surfaced
    assert "design" in proc.stdout                     # streamed via LiveDriver, not the TUI
