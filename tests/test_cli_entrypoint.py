from __future__ import annotations

from imp.cli.main import render_overview


def test_overview_lists_each_common_command_and_footer() -> None:
    text = render_overview(color=False)
    for name in ("new", "resume", "status", "log", "abandon", "config"):
        assert name in text
    # Getting-started pointer and the per-command detail footer.
    assert "imp init" in text
    assert "Run 'imp <command> -h' for details." in text


def test_overview_resume_line_is_accurate_and_unframed() -> None:
    text = render_overview(color=False)
    # Accuracy: resume must NOT claim it resumes "the most recent" change.
    assert "most recent" not in text.lower()
    # Content, not an argparse error: no usage:/error: framing words.
    assert "usage" not in text.lower()
    assert "error" not in text.lower()
