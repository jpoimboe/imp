from __future__ import annotations

import pytest

from imp.cli.main import _decide_resume_action


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_decide_resume_bare_enter_empty_queue_reprompts() -> None:
    assert _decide_resume_action(line="", queued=False, slash=None) == "reprompt"


def test_decide_resume_bare_enter_with_queue_resumes() -> None:
    assert _decide_resume_action(line="", queued=True, slash=None) == "resume"


def test_decide_resume_continue_resumes() -> None:
    assert _decide_resume_action(line="/continue", queued=False, slash="continue") == "resume"


def test_decide_resume_typed_line_resumes() -> None:
    assert _decide_resume_action(line="do more", queued=False, slash=None) == "resume"
