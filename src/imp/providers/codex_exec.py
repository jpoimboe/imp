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
from collections.abc import AsyncGenerator, Awaitable
from dataclasses import dataclass
from typing import Literal, TypeVar

import anyio

from imp.agents.timeouts import PROCESS_KILL_GRACE_S
from imp.agents.tools.context import kill_process_group, kill_process_tree
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
    model_reasoning_effort: ModelReasoningEffort | None = None


def build_command_args(options: ThreadOptions, thread_id: str | None) -> list[str]:
    """The argv after the executable. Takes no prompt: it travels on stdin, so it
    cannot leak into a process listing."""
    args = ["exec", "--json", "--model", options.model, "--sandbox", options.sandbox_mode]
    if options.model_reasoning_effort is not None:
        args += ["--config", f'model_reasoning_effort="{options.model_reasoning_effort}"']
    args += ["--config", f'approval_policy="{options.approval_policy}"']
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


class CodexProcessOwner:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._cancelled = False
        self._hard_stop = asyncio.Event()
        self._acknowledgement = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def acknowledged(self) -> bool:
        return self._acknowledgement.is_set()

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        return self._process

    def cancel(self) -> None:
        self._cancelled = True

    async def wait_for_hard_stop(self) -> None:
        await self._hard_stop.wait()

    def acknowledge(self) -> None:
        self._acknowledgement.set()

    async def wait_for_acknowledgement(self) -> None:
        await self._acknowledgement.wait()

    def publish(self, process: asyncio.subprocess.Process) -> bool:
        assert self._process is None
        self._process = process
        return self._cancelled

    def clear(self, process: asyncio.subprocess.Process) -> None:
        if self._process is process:
            self._process = None

    async def force_kill(self) -> None:
        self.cancel()
        process = self._process
        if process is None:
            self._hard_stop.set()
            return
        if process.returncode is not None:
            self.clear(process)
            self._hard_stop.set()
            return
        try:
            with contextlib.suppress(ProcessLookupError, OSError):
                await kill_process_tree(process, signal.SIGKILL)
        finally:
            self._hard_stop.set()


_T = TypeVar("_T")


async def _finish_task_after_cancellation(task: asyncio.Task[_T]) -> _T:
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)
    return task.result()


async def _wait_for_hard_stop(
    awaitable: Awaitable[_T], hard_stop_task: asyncio.Task[None],
) -> tuple[bool, _T | None]:
    operation_task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait(
            (operation_task, hard_stop_task), return_when=asyncio.FIRST_COMPLETED)
        if hard_stop_task in done:
            return True, None
        return False, await operation_task
    finally:
        with anyio.CancelScope(shield=True):
            if not operation_task.done():
                operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)


async def _run_exec(
    executable: str, args: list[str], env: dict[str, str], prompt: str,
    process_owner: CodexProcessOwner,
) -> AsyncGenerator[str, None]:
    if process_owner.cancelled:
        return

    proc: asyncio.subprocess.Process | None = None
    hard_stop_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[str] | None = None
    try:
        spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
            executable, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=CODEX_STREAM_LIMIT,
            start_new_session=True,
        ))
        try:
            proc = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                proc = await _finish_task_after_cancellation(spawn_task)
                process_owner.publish(proc)
            raise
        cancelled = process_owner.publish(proc)
        stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
        assert stdin is not None and stdout is not None and stderr is not None
        hard_stop_task = asyncio.create_task(process_owner.wait_for_hard_stop())
        stderr_task = asyncio.create_task(_drain_stderr(stderr, current_stderr_sink.get()))

        if cancelled:
            await process_owner.force_kill()
            return

        # A child that exits before draining the prompt (a codex too old for --json
        # rejects the flag and exits) breaks the pipe; its exit code and stderr are
        # the real diagnosis, so fall through to the read loop instead of raising here.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.write(prompt.encode("utf-8"))
            hard_stopped, _ = await _wait_for_hard_stop(stdin.drain(), hard_stop_task)
            if hard_stopped:
                return
        stdin.close()  # codex reads stdin to EOF: without the close every turn hangs

        while True:
            hard_stopped, line = await _wait_for_hard_stop(
                stdout.readline(), hard_stop_task)
            if hard_stopped:
                return
            if not line:
                break
            yield line.decode("utf-8").rstrip("\n")

        hard_stopped, returncode = await _wait_for_hard_stop(proc.wait(), hard_stop_task)
        if hard_stopped:
            return
        assert returncode is not None
        process_owner.clear(proc)

        hard_stopped, stderr_text = await _wait_for_hard_stop(stderr_task, hard_stop_task)
        if hard_stopped:
            return
        assert stderr_text is not None
        if returncode != 0:
            raise CodexExecError(f"codex exec exited with code {returncode}: {stderr_text}")
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            if proc is not None:
                kill_task = asyncio.create_task(process_owner.force_kill())
                await _finish_task_after_cancellation(kill_task)
        raise
    finally:
        with anyio.CancelScope(shield=True):
            tasks = [task for task in (stderr_task, hard_stop_task) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if proc is not None:
                if proc.returncode is None:
                    await kill_process_group(proc, signal.SIGKILL)
                    # Bounded: Process.wait() returns only once the child's pipes
                    # disconnect, so a descendant that escaped the group would otherwise
                    # hang teardown forever. The group already took SIGKILL.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=PROCESS_KILL_GRACE_S)
                process_owner.clear(proc)


@dataclass(frozen=True)
class StreamedTurn:
    events: AsyncGenerator[ThreadEvent, None]


class Thread:
    def __init__(
        self, executable: str, env: dict[str, str], options: ThreadOptions,
        thread_id: str | None, process_owner: CodexProcessOwner,
    ) -> None:
        self._executable = executable
        self._env = env
        self._options = options
        self._id = thread_id
        self._process_owner = process_owner

    async def run_streamed(self, prompt: str) -> StreamedTurn:
        return StreamedTurn(events=self._stream(prompt))

    async def _stream(self, prompt: str) -> AsyncGenerator[ThreadEvent, None]:
        args = build_command_args(self._options, self._id)
        async with contextlib.aclosing(
                _run_exec(
                    self._executable, args, self._env, prompt,
                    self._process_owner)) as lines:
            async for line in lines:
                event = parse_thread_event_line(line)
                if isinstance(event, ThreadStartedEvent):
                    self._id = event.thread_id
                yield event


class Codex:
    def __init__(
        self, options: CodexOptions, *,
        process_owner: CodexProcessOwner | None = None,
    ) -> None:
        executable = shutil.which("codex")
        if executable is None:
            raise ConfigError("codex CLI not found on PATH")
        self._executable = executable
        env = dict(options.env)
        env.setdefault(INTERNAL_ORIGINATOR_ENV, CODEX_ORIGINATOR)
        self._env = env
        self._process_owner = process_owner or CodexProcessOwner()

    def start_thread(self, options: ThreadOptions) -> Thread:
        return Thread(
            self._executable, self._env, options, thread_id=None,
            process_owner=self._process_owner)

    def resume_thread(self, thread_id: str, options: ThreadOptions) -> Thread:
        return Thread(
            self._executable, self._env, options, thread_id=thread_id,
            process_owner=self._process_owner)
