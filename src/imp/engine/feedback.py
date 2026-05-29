"""Format gate-failure detail into framed feedback prose for a productive retry.

Pure functions: each gate runner's failure detail → a single string that the
executor stashes in `State.retry_feedback` and a provider injects as a user
turn on the targeted step's resume (design §6).
"""

from __future__ import annotations

from typing import Any

_WRAPPER = (
    "Your previous attempt completed but was not accepted. {detail} "
    "Address this, then call `complete_step` again."
)

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


def format_agent_feedback(reviewer: str, output: dict[str, Any]) -> str:
    """Schema-agnostic: render every field of the reviewer's structured
    output as `key: value` lines. The decide-matched verdict field (whatever
    it is named) is surfaced among them — we make no assumption about a
    specific field name."""
    lines = [f"The {reviewer} reviewed your work and did not accept it."]
    for key, value in output.items():
        lines.append(f"{key}: {_coerce(value)}")
    return _WRAPPER.format(detail="\n".join(lines))


def format_human_feedback(label: str) -> str:
    return _WRAPPER.format(detail=f"A reviewer sent this back: {label!r}.")
