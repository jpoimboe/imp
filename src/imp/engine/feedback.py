"""Format gate-failure detail into framed feedback prose for a productive retry.

Pure functions: each gate runner's failure detail → a single string that the
executor stashes in `State.retry_feedback` and a provider injects as a user
turn on the targeted step's resume (design §6).
"""

from __future__ import annotations

from typing import Any

_WRAPPER = "Your previous attempt completed but was not accepted. {detail}"

# Single source of truth for the finish-with-complete_step reminder appended by the executor.
COMPLETION_BACKSTOP = (
    "When you've addressed this, end this same turn by calling `complete_step` — a "
    "turn that ends on prose alone pauses the run for a human instead of advancing "
    "it. Only stop without `complete_step` if you genuinely need a person to weigh "
    "in."
)

# Bare-turn-end nudge; WAITING offered first so an uncertain model isn't tilted to complete early.
COMPLETION_NUDGE = (
    "Your turn ended without `complete_step`. If you are waiting on the human to "
    "reply, respond with only the word `WAITING`. If the step is done, call "
    "`complete_step` now with your structured result. Do not repeat or restate "
    "anything you have already said."
)


def apply_completion_backstop(feedback: str | None, *, has_schema: bool) -> str | None:
    """Append the backstop when feedback is truthy and has_schema, else return it unchanged."""
    if feedback and has_schema:
        return f"{feedback}\n\n{COMPLETION_BACKSTOP}"
    return feedback


_TRUNCATE_LIMIT = 4000


def _coerce(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _truncate(text: str, limit: int = _TRUNCATE_LIMIT) -> str:
    """Head+tail split: test failures put useful signal at both ends."""
    if len(text) <= limit:
        return text
    half = limit // 2
    head, tail = text[:half], text[-half:]
    dropped = text[half:-half]
    n_lines = dropped.count("\n") + 1
    return f"{head}\n[… {n_lines} lines truncated …]\n{tail}"


def format_script_feedback(
    command: str, exit_code: int, stdout: Any, stderr: Any,
) -> str:
    body = _truncate(f"{_coerce(stdout)}{_coerce(stderr)}")
    detail = f"Gate `{command}` failed (exit {exit_code}).\n{body}"
    return _WRAPPER.format(detail=detail)


def format_clean_tree_feedback(paths: str) -> str:
    body = _truncate(paths)
    detail = (
        "The worktree still has uncommitted changes after your turn:\n"
        f"{body}\n"
        "Commit all of your work before continuing — or, if a merge or rebase "
        "resolution is in progress, complete it first."
    )
    return _WRAPPER.format(detail=detail)


def summarize_transition_reason(feedback: str | None, *, limit: int = 80) -> str | None:
    """First non-empty line of `feedback`, framing stripped and truncated; None if empty."""
    if not feedback:
        return None
    text = feedback
    prefix = _WRAPPER.format(detail="")
    if text.startswith(prefix):
        text = text[len(prefix):]
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), None)
    if not first:
        return None
    if len(first) > limit:
        first = first[: limit - 1].rstrip() + "…"
    return first


def render_output_fields(output: dict[str, Any]) -> str:
    """Render every field of a step's structured output as `key: value` lines.
    No editorial, no wrapper — the engine composes `{preamble}\n{fields}` on the
    edge (§6). Schema-agnostic: the decide-matched verdict field is surfaced
    among the rest; we assume no specific field name."""
    return "\n".join(f"{key}: {_coerce(value)}" for key, value in output.items())
