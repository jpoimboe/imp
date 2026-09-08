# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""imp's driver for the `codex exec --json` subprocess.

Owns argv/env construction, the spawn, the JSONL read loop, the concurrent stderr
drain, and the group-reaping teardown. The child is its own session leader, so a
terminal Ctrl-C (a process-group SIGINT) reaches imp alone, and teardown can kill
the child's whole group rather than orphaning its grandchildren.
"""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import shutil
import signal
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Literal

from imp.agents.timeouts import PROCESS_KILL_GRACE_S
from imp.agents.tools.context import kill_process_group
from imp.providers.codex_events import (
    ThreadEvent,
    ThreadStartedEvent,
    parse_thread_event_line,
)
from imp.providers.errors import ConfigError
from imp.providers.stderr_sink import StderrSink, current_stderr_sink

# 64 MiB ceiling for one JSONL line: asyncio's 64 KiB default makes
# StreamReader.readline() raise on a larger codex event line.
CODEX_STREAM_LIMIT = 64 * 1024 * 1024
# Full stream reaches the sink; this only bounds what an error message carries into the event log.
STDERR_ERROR_CHARS = 4096
INTERNAL_ORIGINATOR_ENV = "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"
# Server-visible: what OpenAI's backend sees. Not this quest's experiment.
CODEX_ORIGINATOR = "codex_sdk_py"

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
ApprovalMode = Literal["never", "on-request", "on-failure", "untrusted"]
ModelReasoningEffort = Literal["minimal", "low", "medium", "high"]


class CodexExecError(Exception):
    """`codex exec` exited non-zero; carries the child's stderr. Deliberately not a
    ProviderError: the runner's broad `except Exception` classifies it
    unknown-category and attaches the stderr tail."""


@dataclass(frozen=True)
class CodexOptions:
    env: dict[str, str]


@dataclass(frozen=True)
class ThreadOptions:
    model: str
    sandbox_mode: SandboxMode
    approval_policy: ApprovalMode
    network_access_enabled: bool
    model_reasoning_effort: ModelReasoningEffort | None = None


def build_command_args(options: ThreadOptions, thread_id: str | None) -> list[str]:
    """The argv after the executable. Takes no prompt: it travels on stdin, so it
    cannot leak into a process listing."""
    args = ["exec", "--json", "--model", options.model, "--sandbox", options.sandbox_mode]
    if options.model_reasoning_effort is not None:
        args += ["--config", f'model_reasoning_effort="{options.model_reasoning_effort}"']
    network = "true" if options.network_access_enabled else "false"
    args += [
        "--config", f"sandbox_workspace_write.network_access={network}",
        "--config", f'approval_policy="{options.approval_policy}"',
    ]
    if thread_id is not None:
        args += ["resume", thread_id]
    return args


async def _drain_stderr(stream: asyncio.StreamReader, sink: StderrSink | None) -> str:
    """Read stderr to EOF, teeing into the active sink. Must run concurrently with
    the stdout loop: a full stderr pipe blocks the child, which stops stdout."""
    # Incremental so a multi-byte char split across two reads decodes correctly
    # instead of becoming U+FFFD on the boundary.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    tail = ""
    truncated = False
    while True:
        chunk = await stream.read(4096)
        text = decoder.decode(chunk, final=not chunk)
        if text:
            if sink is not None:
                sink.feed(text)
            tail += text
            if len(tail) > STDERR_ERROR_CHARS:
                tail = tail[-STDERR_ERROR_CHARS:]
                truncated = True
        if not chunk:
            return f"[stderr truncated]\n{tail}" if truncated else tail


async def _run_exec(
    executable: str, args: list[str], env: dict[str, str], prompt: str,
) -> AsyncGenerator[str, None]:
    proc = await asyncio.create_subprocess_exec(
        executable, *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=CODEX_STREAM_LIMIT,
        start_new_session=True,
    )
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    assert stdin is not None and stdout is not None and stderr is not None
    stderr_task = asyncio.create_task(_drain_stderr(stderr, current_stderr_sink.get()))
    try:
        stdin.write(prompt.encode("utf-8"))
        await stdin.drain()
        stdin.close()  # codex reads stdin to EOF: without the close every turn hangs

        while True:
            line = await stdout.readline()
            if not line:
                break
            yield line.decode("utf-8").rstrip("\n")

        returncode = await proc.wait()
        stderr_text = await stderr_task
        if returncode != 0:
            raise CodexExecError(f"codex exec exited with code {returncode}: {stderr_text}")
    finally:
        stderr_task.cancel()
        if proc.returncode is None:
            await kill_process_group(proc, signal.SIGKILL)
            # Bounded: Process.wait() returns only once the child's pipes
            # disconnect, so a descendant that escaped the group would otherwise
            # hang teardown forever. The group already took SIGKILL.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=PROCESS_KILL_GRACE_S)


@dataclass(frozen=True)
class StreamedTurn:
    events: AsyncGenerator[ThreadEvent, None]


class Thread:
    def __init__(
        self, executable: str, env: dict[str, str], options: ThreadOptions,
        thread_id: str | None,
    ) -> None:
        self._executable = executable
        self._env = env
        self._options = options
        self._id = thread_id

    async def run_streamed(self, prompt: str) -> StreamedTurn:
        return StreamedTurn(events=self._stream(prompt))

    async def _stream(self, prompt: str) -> AsyncGenerator[ThreadEvent, None]:
        args = build_command_args(self._options, self._id)
        async with contextlib.aclosing(
                _run_exec(self._executable, args, self._env, prompt)) as lines:
            async for line in lines:
                event = parse_thread_event_line(line)
                if isinstance(event, ThreadStartedEvent):
                    self._id = event.thread_id
                yield event


class Codex:
    def __init__(self, options: CodexOptions) -> None:
        executable = shutil.which("codex")
        if executable is None:
            raise ConfigError("codex CLI not found on PATH")
        self._executable = executable
        env = dict(options.env)
        env.setdefault(INTERNAL_ORIGINATOR_ENV, CODEX_ORIGINATOR)
        self._env = env

    def start_thread(self, options: ThreadOptions) -> Thread:
        return Thread(self._executable, self._env, options, thread_id=None)

    def resume_thread(self, thread_id: str, options: ThreadOptions) -> Thread:
        return Thread(self._executable, self._env, options, thread_id=thread_id)
