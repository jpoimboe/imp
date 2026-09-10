# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Engine-level reincarnation helpers (foundation primitives; no executor imports)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal


def session_has_resume_state(session_dir: Path) -> bool:
    from imp.agents.transcript import Transcript
    from imp.engine.resume_state import read_native_resume_state

    native = read_native_resume_state(session_dir)
    return native is not None or Transcript(session_dir / "transcript.jsonl").exists_and_nonempty()


def stash_session(session_dir: Path) -> Path | None:
    """Move a resumable session aside to its first free ``<name>.attempt-N`` sibling."""
    if not session_has_resume_state(session_dir):
        return None
    n = 1
    while True:
        dest = session_dir.parent / f"{session_dir.name}.attempt-{n}"
        if not dest.exists():
            break
        n += 1
    session_dir.rename(dest)
    return dest


_SEED_OPENINGS = {
    "context": (
        "You are a reincarnated session: the previous attempt approached its "
        "context limit and was restarted fresh to keep working without a silent "
        "compaction. Pick up where it left off using the recovery sources below."),
    "provider_switch": (
        "You are a reincarnated session: the previous attempt ran on a different "
        "provider and was switched at the user's request, not because anything "
        "went wrong. Pick up where it left off using the recovery sources below."),
}


def reincarnation_seed(
    *, handoff_text: str | None, quest_id: str, step_name: str,
    cause: Literal["context", "provider_switch"] = "context",
) -> str:
    """Recovery text for a reincarnated (reborn) session, appended last to its
    first user message.

    Names only sources the reborn session can actually reach: the handoff note
    inlined verbatim, and `imp events` queries the daemon resolves outside the
    sandbox — never a path under <quest_dir>/steps/, which every agent's policy
    masks. `handoff_text is None` means no note (the reactive Tier B path, or a
    proactive handoff that wrote none); an empty note cannot occur, since
    write_handoff_note refuses empty or whitespace-only text.
    """
    step_query = (
        f"`imp events {quest_id} --step {step_name} --limit N` returns the last "
        "N events of this step — everything the previous attempt did, one "
        "compact line each, in order (`--json` gives an event's exact payload)")
    lines = [_SEED_OPENINGS[cause]]
    if handoff_text is not None:
        lines.append(
            "- Primary: the previous attempt's handoff note — its goal, "
            "decisions, key findings, and exactly what remains — reproduced "
            "verbatim at the end of this message.")
        lines.append(
            f"- Deeper detail: {step_query}. Reach for it when the handoff note "
            "does not carry enough.")
    else:
        lines.append(
            f"- Primary: {step_query}. It left no handoff note, so this is the "
            "record of what it did.")
    lines.append(
        f"- Orientation only: `imp events {quest_id} --limit N` widens the same "
        "log to the whole quest — what earlier steps did and what was said (a "
        "bare call prints the quest's index). Use it for quest-level bearings, "
        "not for current state.")
    if handoff_text is not None:
        lines.append(
            "\n--- HANDOFF NOTE FROM THE PREVIOUS ATTEMPT (verbatim, to end of "
            f"message) ---\n\n{handoff_text}")
    return "\n".join(lines)


def provider_path_for(session_dir: Path) -> Path:
    """The sidecar naming the vendor that wrote this session."""
    return session_dir / "provider"


def write_session_owner(session_dir: Path, provider: str) -> None:
    provider_path_for(session_dir).write_text(provider)


def read_session_owner(session_dir: Path) -> str | None:
    """The recorded owner, or None when the session is unlabelled."""
    path = provider_path_for(session_dir)
    if not path.exists():
        return None
    return path.read_text().strip() or None


def handoff_path_for(session_dir: Path) -> Path:
    """The on-disk handoff-note path for a session, alongside its directory."""
    return session_dir.parent / f"{session_dir.name}.handoff.md"


def handoff_instruction() -> str:
    """The user-turn text that elicits a handoff when a session approaches its
    context limit (Tier A proactive handoff). The agent emits the handoff as its
    final reply text (plain text, no tool calls) and stops; the provider persists
    that text and the engine reincarnates the session from it."""
    return (
        "You are approaching the context limit. Stop new work now. Your final "
        "reply must be a compact handoff note — plain text, no tool calls — "
        "capturing: the current goal and progress, decisions made, key findings "
        "as `file:line` citations, and exactly what remains to be done. After "
        "you have written your reply, stop — do not start anything new.")


def write_handoff_note(handoff_path: Path, text: str) -> bool:
    """Write `text` to `handoff_path` unless empty/whitespace; return whether it wrote."""
    if not text.strip():
        return False
    handoff_path.write_text(text)
    return True
