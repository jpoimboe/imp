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
