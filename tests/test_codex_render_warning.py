from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli import render_text as rt


def test_codex_warning_renders_as_subline() -> None:
    node = nv.TiersResolvedNode(rows=[
        {"tier": "high", "provider": "codex", "model": "gpt-5.5",
         "differ_tier": None, "differ_from": None,
         "warning": "codex runs unsandboxed; a disobedient native edit "
                    "outside the worktree is detected but not auto-reverted."},
        {"tier": "low", "provider": "anthropic", "model": "haiku",
         "differ_tier": None, "differ_from": None, "warning": None},
    ])
    out = list(rt._render(node, depth=0, full=False, color=False, width=120))
    # the codex row's main line, then a warning sub-line immediately after
    hi = next(i for i, ln in enumerate(out) if ln.lstrip().startswith("high "))
    assert "→ codex/gpt-5.5" in out[hi]
    assert "runs unsandboxed" in out[hi + 1]
    # the non-codex row emits no warning sub-line
    assert not any("runs unsandboxed" in ln for ln in out
                   if ln.lstrip().startswith("low"))
