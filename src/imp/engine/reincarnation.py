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
