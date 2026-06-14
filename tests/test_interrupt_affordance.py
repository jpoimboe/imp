from __future__ import annotations

import io

import anyio

import imp.cli.main as m


class _TTY(io.StringIO):
    def isatty(self) -> bool:        # force the isatty()-gated affordance to print
        return True


class _Coord:
    def has_queued_interject(self) -> bool:
        return False


async def _run(at_completion: bool) -> None:
    # "go" resumes immediately when not at completion; "/continue" finishes the
    # at-completion path so the loop terminates without exhausting the iterator.
    replies = iter(["go", "/continue"])

    async def _reply() -> str:
        return next(replies)

    await m._collect_resume_input(_reply, _Coord(), at_completion=at_completion)


def test_interrupt_pause_prints_affordance(monkeypatch) -> None:
    buf = _TTY()
    monkeypatch.setattr(m.sys, "stdout", buf)
    anyio.run(_run, False)
    out = buf.getvalue().lower()
    assert "paused" in out
    assert "ctrl-d" in out
    assert "redirect" in out


def test_completion_boundary_omits_interrupt_affordance(monkeypatch) -> None:
    buf = _TTY()
    monkeypatch.setattr(m.sys, "stdout", buf)
    anyio.run(_run, True)
    out = buf.getvalue().lower()
    assert "redirect" not in out   # the interrupt affordance is not shown at completion


def test_interrupt_affordance_suppressed_when_not_tty(monkeypatch) -> None:
    buf = io.StringIO()  # plain StringIO — isatty() returns False
    monkeypatch.setattr(m.sys, "stdout", buf)
    anyio.run(_run, False)
    out = buf.getvalue().lower()
    assert "paused" not in out  # affordance is tty-only
