"""Signal handling: pause-and-exit on Ctrl+C / Ctrl+D / SIGTERM.

The executor naturally yields control between events (it iterates the runner's
generator and writes the log). Catching KeyboardInterrupt at the outer
_cmd_new/_cmd_resume frame is sufficient — Python's default SIGINT raises in
the main thread on the next bytecode boundary, interrupting whatever runner
code is in flight (including blocking I/O).

Ctrl+D arrives from prompt_toolkit as EOFError. Handled identically.

Tool-kill escalation (Task 13) happens via the process-group mechanism
installed when imp starts.
"""

from __future__ import annotations

from imp.engine.change import Change
from imp.engine.state import State

# Mid-stream ESC (cancel an LLM call in flight) is post-V0. The langchain
# cancellation token API in the installed version doesn't surface cleanly
# through deepagents' stream interface; punt to Plan 4. V0 ESC only fires
# between turns (when ChatMiddleware is awaiting user input).


def pause_and_exit(change: Change, *, reason: str) -> None:
    """Save state as paused; append workflow_paused with reason. Idempotent."""
    try:
        state = State.load(change.state_dir)
        state.status = "paused"
        state.save(change.state_dir)
    except FileNotFoundError:
        # State never reached the durable phase — nothing to update
        pass
    change.event_log().append("workflow_paused", {"reason": reason})
