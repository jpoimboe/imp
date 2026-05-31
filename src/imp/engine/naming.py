"""Random three-word phrase for the default Change title (spec §3.1).

This is engine code, so `random` is fair game. The combinatorial space of three
independent words makes same-instant collisions negligible; create_worktree
still retries on the rare branch collision.
"""

from __future__ import annotations

import random
from importlib.resources import files


_WORDS: list[str] | None = None


def _words() -> list[str]:
    global _WORDS
    if _WORDS is None:
        text = files("imp.assets").joinpath("words.txt").read_text()
        _WORDS = [w.strip() for w in text.splitlines() if w.strip()]
    return _WORDS


def random_title(rng: random.Random | None = None) -> str:
    """e.g. "brave silver otter". Slug is derived from this via slug.slugify."""
    r = rng or random
    pool = _words()
    return " ".join(r.choice(pool) for _ in range(3))
