"""Exception types raised by tools to signal different failure modes."""

from __future__ import annotations


class FatalToolError(Exception):
    """A tool raises this when continuing the step is pointless — the workflow
    is misconfigured or a runtime invariant has been violated.

    The runner catches FatalToolError separately from generic ``Exception``:
    instead of returning a ``tool_result`` with ``is_error=True`` (which lets
    the agent see the error and try to work around it), the runner emits an
    ``error`` event with ``kind="fatal_tool_error"`` and the executor turns
    that into an immediate ``Pause`` — no retries, no wasted LLM calls.

    Use for:
    - subagent ``task`` spawn requested but no spawn fn installed.
    - Any other condition where the agent has no way to recover and the
      problem is in the runtime configuration, not the agent's reasoning.

    Do NOT use for normal tool failures (bash exit nonzero, file not found,
    JSON parse error). Those are recoverable — surface them to the agent as
    ``tool_result`` so it can decide what to do.
    """
