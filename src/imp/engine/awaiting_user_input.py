"""AwaitingUserInput: the no-human pause signal for a bare-turn-end yield.

Raised by an input channel that has no human to take the yield — a subagent, a
headless top-level run whose --message reply is exhausted, or the daemon's
hibernation gate. It propagates out of the provider run() (re-raised past the
generic tool except, mirroring SubagentSuspended) and is caught in
drive_invocation as a clean, resumable pause carrying the agent's last text.
"""

from __future__ import annotations


class AwaitingUserInput(Exception):
    """Pause a run at a bare-turn-end yield when no human is attached; `prompt`
    carries the agent's last text as the resume prompt.

    `interrupt` is True only when the pause came from the cooperative-interrupt
    steer (Ctrl-C mid-turn) rather than a genuine bare-turn-end yield; the
    executor shadows it into State.interrupt_pending so a no-message resume
    continues the interrupted step instead of soliciting."""

    def __init__(self, prompt: str = "", *, interrupt: bool = False) -> None:
        super().__init__("awaiting user input")
        self.prompt = prompt
        self.interrupt = interrupt
