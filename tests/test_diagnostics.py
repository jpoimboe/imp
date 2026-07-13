from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from pathlib import Path

from imp.diagnostics import install_stack_dumper


def _wait_for(path: Path, needle: str, tries: int = 300) -> str:
    for _ in range(tries):
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if needle in text:
                return text
        time.sleep(0.02)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_dump_contains_header_thread_and_asyncio_sections(tmp_path: Path) -> None:
    dump = tmp_path / "stack-dump.txt"
    dumper = install_stack_dumper(dump)
    try:
        async def _run() -> None:
            pending = asyncio.ensure_future(asyncio.Event().wait())
            os.kill(os.getpid(), signal.SIGUSR1)
            for _ in range(300):
                if dump.exists() and "-- asyncio tasks --" in dump.read_text():
                    break
                await asyncio.sleep(0.02)
            pending.cancel()

        asyncio.run(_run())
        text = dump.read_text(encoding="utf-8")
    finally:
        dumper.uninstall()

    assert re.search(r"==== imp stack dump · .+ · pid \d+ ====", text)
    assert "-- asyncio tasks --" in text
    # faulthandler's all-thread dump names threads and file frames.
    assert "Thread" in text or 'File "' in text
    # The self-pipe diagnostics thread actually enumerated a pending task.
    asyncio_section = text.split("-- asyncio tasks --", 1)[1]
    assert "Task " in asyncio_section


def test_no_running_loop_notes_absence(tmp_path: Path) -> None:
    dump = tmp_path / "stack-dump.txt"
    dumper = install_stack_dumper(dump)
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        text = _wait_for(dump, "(no running event loop)")
    finally:
        dumper.uninstall()

    assert re.search(r"==== imp stack dump · .+ · pid \d+ ====", text)
    assert "(no running event loop)" in text


def test_repeated_signals_append_repeated_sections(tmp_path: Path) -> None:
    dump = tmp_path / "stack-dump.txt"
    dumper = install_stack_dumper(dump)
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        _wait_for(dump, "-- asyncio tasks --")
        os.kill(os.getpid(), signal.SIGUSR1)
        for _ in range(300):
            if dump.exists() and dump.read_text().count("-- asyncio tasks --") >= 2:
                break
            time.sleep(0.02)
        text = dump.read_text(encoding="utf-8")
    finally:
        dumper.uninstall()

    assert text.count("==== imp stack dump") == 2
    assert text.count("-- asyncio tasks --") == 2


def test_asyncio_section_produced_while_loop_is_wedged(tmp_path: Path) -> None:
    import threading

    dump = tmp_path / "stack-dump.txt"
    dumper = install_stack_dumper(dump)

    async def _named_pending() -> None:
        await asyncio.Event().wait()

    async def _main() -> None:
        marker = asyncio.ensure_future(_named_pending())
        # A blocking sync call inside the coroutine wedges the loop: it does not
        # await, so the loop cannot turn while the signal is handled.
        time.sleep(1.5)
        marker.cancel()

    def _fire() -> None:
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGUSR1)

    firer = threading.Thread(target=_fire, name="sigusr1-firer")
    try:
        firer.start()
        asyncio.run(_main())
        text = _wait_for(dump, "-- asyncio tasks --")
    finally:
        firer.join(timeout=2)
        dumper.uninstall()

    assert "-- asyncio tasks --" in text
    # The wedged loop never turned, yet the diagnostics thread still enumerated
    # the pending task and captured its stack.
    assert "_named_pending" in text
