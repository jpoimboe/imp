"""OpenAI prompt-cache key is keyed on the immutable id, so a mid-run title
rename does NOT rotate the key (spec §9, AC 10). The test varies the title
across two ChangeContexts sharing one id and shows the id-derived key is stable
while a slug-derived key would NOT be — guarding the keying *choice*, not just
the helper's purity."""

from __future__ import annotations

from pathlib import Path

from imp.agents.runner import ChangeContext
from imp.providers.openai import _prompt_cache_key


def _cc(title: str) -> ChangeContext:
    return ChangeContext(
        id="fff666", title=title,
        project_root=Path("/wt"), workflow_dir=Path("/wt/.imp/change/workflow"),
    )


def test_cache_key_stable_across_title_rename() -> None:
    aid = "step:drafting/spec"
    before = _cc("First Name")     # pre-rename
    after = _cc("Second Name")    # post-rename (same id)

    # Keyed on the immutable id: identical before/after the title rename.
    assert _prompt_cache_key(before.id, aid) == _prompt_cache_key(after.id, aid)

    # Guard the keying choice: a rotating title-derived string WOULD change the key,
    # so keying on it (rather than the immutable id) would break stability.
    assert _prompt_cache_key("first-name-fff666", aid) != _prompt_cache_key(
        "second-name-fff666", aid
    )
