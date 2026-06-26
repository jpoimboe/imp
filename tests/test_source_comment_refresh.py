from pathlib import Path

import imp.agents.runner as runner_mod
import imp.agents.tools.context as context_mod


def test_context_source_has_no_wait_for_user_references():
    """context.py's refreshed comments hold no wait_for_user reference."""
    src = Path(context_mod.__file__).read_text()
    assert "wait_for_user" not in src


def test_runner_source_has_no_wait_for_user_references():
    """runner.py's refreshed comments hold no wait_for_user reference."""
    src = Path(runner_mod.__file__).read_text()
    assert "wait_for_user" not in src
