from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


def _run(pytester: pytest.Pytester, *, passes: bool) -> pytest.TestReport:
    quest_dir = pytester.path / "quest"
    (quest_dir / "steps" / "a" / "w").mkdir(parents=True)
    (quest_dir / "events.jsonl").write_text('{"type": "EVENT_TAIL_MARKER"}\n')
    (quest_dir / "steps" / "a" / "w" / "stderr.log").write_text("STDERR_TAIL_MARKER\n")
    pytester.makeconftest(
        "from tests.conftest import pytest_runtest_makereport, register_quest_evidence\n")
    pytester.makepyfile(f"""
        from pathlib import Path

        def test_registered(register_quest_evidence):
            register_quest_evidence(Path({str(quest_dir)!r}))
            assert {passes!r}
    """)
    reprec = pytester.inline_run()
    [call] = [r for r in reprec.getreports("pytest_runtest_logreport") if r.when == "call"]
    return call


def test_failing_registered_test_carries_quest_evidence(pytester: pytest.Pytester) -> None:
    call = _run(pytester, passes=False)
    assert call.failed
    evidence = dict(call.sections)["imp quest evidence"]
    assert "EVENT_TAIL_MARKER" in evidence
    assert "steps/a/w/stderr.log tail:\nSTDERR_TAIL_MARKER" in evidence


def test_passing_registered_test_adds_no_evidence(pytester: pytest.Pytester) -> None:
    call = _run(pytester, passes=True)
    assert call.passed
    assert "imp quest evidence" not in dict(call.sections)
