"""Engine-level reincarnation helpers (foundation primitives; no executor imports)."""
from __future__ import annotations

from pathlib import Path


def stash_transcript(transcript_dir: Path) -> Path | None:
    """Move transcript_dir aside to a sibling ``<name>.attempt-N``; return the
    stash path, or None when there is nothing to stash."""
    tx = transcript_dir / "transcript.jsonl"
    if not tx.exists() or tx.stat().st_size == 0:
        return None
    n = 1
    while True:
        dest = transcript_dir.parent / f"{transcript_dir.name}.attempt-{n}"
        if not dest.exists():
            break
        n += 1
    transcript_dir.rename(dest)
    return dest


def reincarnation_seed(
    *, stashed_dir: Path | None, handoff_path: Path | None
) -> str:
    """Opening user message for a reincarnated (reborn) session.

    The prior session approached its context limit and is restarting fresh.
    Point it at its recovery sources in priority order: the agent-authored
    handoff note (primary; present only for the proactive Tier A path), the
    stashed prior transcript for deeper detail, and `query_events` for
    change-level orientation. Built to serve both the no-handoff (reactive,
    Tier B) and handoff (proactive, Tier A) shapes.
    """
    lines = [
        "You are a reincarnated session: the previous attempt approached its "
        "context limit and was restarted fresh to keep working without a silent "
        "compaction. Pick up where it left off using the recovery sources below.",
    ]
    if handoff_path is not None:
        lines.append(
            f"- Primary: read the handoff note at `{handoff_path}` — a compact "
            "summary of the goal, decisions, key findings, and exactly what "
            "remains.")
    if stashed_dir is not None:
        # The "summary above" only exists on the Tier A (handoff) path; on the
        # reactive Tier B path the transcript IS the only prior detail, so word
        # the pointer accordingly rather than referring to a missing summary.
        tail = (" (read_file it if you need more than the summary above carries)."
                if handoff_path is not None
                else " — read_file it for the prior session's full detail.")
        lines.append(
            f"- Primary detail source: the prior transcript is stashed at "
            f"`{stashed_dir}/transcript.jsonl`{tail}")
    lines.append(
        "- Orientation only: `query_events limit=N` returns the last N entries of "
        "the Quest's event log — the provenance record of what was said and what "
        "prior steps did (a bare call is bounded to the last 100). Use it for "
        "quest-level bearings, not for current state and not as the primary "
        "source of detail.")
    return "\n".join(lines)


def handoff_path_for(transcript_dir: Path) -> Path:
    """The on-disk handoff-note path for a session — a SIBLING of its transcript
    dir (`<name>.handoff.md`), so it survives the fresh-entry wipe that removes
    the transcript dir itself. One file per session generation; a later
    reincarnation of the same step overwrites it (the prior content is preserved
    in the stashed transcript)."""
    return transcript_dir.parent / f"{transcript_dir.name}.handoff.md"


def handoff_instruction(handoff_path: Path) -> str:
    """The user-turn text that elicits an agent-authored handoff when a session
    approaches its context limit (Tier A proactive handoff). The agent writes the
    note to `handoff_path` with write_file and stops; the engine then reincarnates
    the session seeded from it."""
    return (
        "You are approaching the context limit. Stop new work now. Write a "
        f"compact handoff to `{handoff_path}` capturing: the current goal and "
        "progress, decisions made, key findings as `file:line` citations, and "
        "exactly what remains to be done. Use write_file. After you have written "
        "it, stop — do not start anything new.")
