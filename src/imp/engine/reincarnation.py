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
            f"- Deeper detail: the prior transcript is stashed at "
            f"`{stashed_dir}/transcript.jsonl`{tail}")
    lines.append(
        "- Orientation: `query_events` returns the change's event log "
        "(change-level steps and notices, not this step's own tool calls).")
    return "\n".join(lines)
