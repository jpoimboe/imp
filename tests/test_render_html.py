from __future__ import annotations

import importlib.resources as ir


def test_github_markdown_css_asset_is_bundled() -> None:
    css = (ir.files("imp") / "assets" / "github-markdown.css").read_text(
        encoding="utf-8")
    assert ".markdown-body" in css          # the class github-markdown-css targets
    assert "color-scheme: light" in css     # the *light* theme was vendored
    assert len(css) > 1000                  # a real stylesheet, not a stub
