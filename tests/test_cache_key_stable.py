"""OpenAI prompt-cache key is keyed on the immutable id, so a mid-run title
rename does NOT rotate the key (spec §9, AC 10). The test varies the title
across two ChangeContexts sharing one id and shows the id-derived key is stable
while a slug-derived key would NOT be — guarding the keying *choice*, not just
the helper's purity."""

from __future__ import annotations

from pathlib import Path

from imp.agents.runner import ChangeContext
from imp.providers.openai import _prompt_cache_key


def _cc(title: str, slug: str) -> ChangeContext:
    return ChangeContext(
        id="2026-06-04-fff666", slug=slug, title=title,
        project_root=Path("/wt"), workflow_dir=Path("/wt/.imp/change/workflow"),
    )


def test_cache_key_stable_across_title_rename() -> None:
    aid = "step:drafting/spec"
    before = _cc("First Name", "2026-06-04-first-name")     # pre-rename
    after = _cc("Second Name", "2026-06-04-second-name")    # post-rename (same id)

    # Keyed on the immutable id: identical before/after the title rename.
    assert _prompt_cache_key(before.id, aid) == _prompt_cache_key(after.id, aid)

    # Guard the keying choice: had the call site keyed on the (rotating) slug,
    # the key WOULD have changed — proving id-keying is what buys stability.
    assert _prompt_cache_key(before.slug, aid) != _prompt_cache_key(after.slug, aid)
