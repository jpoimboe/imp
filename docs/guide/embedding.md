# Embedding imp

`imp.api` is imp's supported Python surface for driving quests from another process — a
web app, a chat bot, a CI hook. It creates quests, lists them, attaches to one, streams
its events, and answers the questions a workflow asks. Everything else under `imp.*` is
internal and may move without notice; this page is the contract.

```python
from imp.api import (
    create_quest, list_quests, attach,
    Session, QuestHandle, QuestSummary, Event, EVENT_SCHEMA_VERSION,
    ImpError, QuestNotFound, DaemonUnreachable, DaemonStartupError,
    NotAtGate, InvalidVar,
)
```

## A worked example

Create a quest, stream it, answer its first question, and stop when it completes:

```python
import asyncio
from pathlib import Path

from imp.api import QuestHandle, attach, create_quest

CONTROL_ROOT = Path("/path/to/the/checkout/imp/manages")


async def interview(handle: QuestHandle) -> None:
    async with attach(CONTROL_ROOT, handle) as session:
        async for event in session.events():
            if event.raw.get("_replay"):
                continue                       # replayed history, not news
            if event.type == "awaiting_user_input":
                print(event.payload.get("last_agent_message") or "")
                reply = await asyncio.to_thread(input, "> ")
                await session.send_input(reply)
            elif event.type == "workflow_completed":
                break
            # any other type, known or not, is simply ignored


# create_quest is synchronous and does git work: call it before the event loop.
handle = create_quest(
    CONTROL_ROOT,
    "interview me about the invention",
    workflow="invention",
    vars={"jurisdiction": "us"},
)
print(f"created {handle.id}")
asyncio.run(interview(handle))
```

The `_replay` filter is not decoration — see the replay handshake below.

## Creating a quest

```python
create_quest(control_root, prompt, *, workflow=None, base=None, title=None,
             background=False, vars=None) -> QuestHandle
```

`control_root` is the git checkout imp manages. Creation is synchronous and happens in
your own process: when the call returns the quest exists on disk and `handle.id` is
attachable that instant — no polling, no scraping the CLI.

`QuestHandle` is a frozen dataclass carrying `id`, `title` and `control_root`, and is
accepted anywhere a quest id is accepted, `attach()` included.

Preflight is `imp new`'s, not a lookalike: the checkout is checked, the base branch is
resolved, and `.imp` is gitignored — in that order, before anything is written. So
`base=None` means the configured `default-base`, and an unset `default-base` or a branch
that does not exist is a hard `ImpError` with no quest home created. A quest cannot be
un-created, so every rejection happens first.

`vars=` seeds workflow variables for this quest alone, as already-typed values
(`{"jurisdiction": "us", "strict": True}`). A name the workflow does not declare, or a
value of the wrong type, raises `InvalidVar` — again before the quest exists.

`background=True` additionally starts a detached autonomous run you never have to attach
to. Leave it false for anything interactive.

## Listing quests

```python
list_quests(control_root) -> list[QuestSummary]
```

Returns in-flight and finished quests — the live quest homes merged with the archive.
`QuestSummary` is a frozen dataclass of `id`, `title`, `status` and `step` (`None` when
the quest has not reached a step yet).

`status` is `new`, `running`, `waiting`, `paused`, `error` or `merge_blocked` while a
quest is in flight, and `merged`, `done`, `abandoned` or `shelved` once it is finished.

## Attaching

```python
attach(control_root, quest_id, *, lease=True) -> AbstractAsyncContextManager[Session]
```

`quest_id` is a `str` or a `QuestHandle`. `attach()` spawns the quest's session daemon
when none is resident, and resumes an already-started quest where it left off — so
`create_quest` then `attach` works with nothing in between.

`attach()` is built on anyio but is backend-agnostic: `asyncio.run(...)` around your own
coroutine is all it needs.

Bringing a daemon up gets a connect budget of 60 seconds by default;
`IMP_SESSION_CONNECT_TIMEOUT` overrides it. Leaving the context closes the socket,
cancels the receive task, and leaves behind no task and no file descriptor.

## The lease

`lease=True` is the default and it is load-bearing. A freshly spawned daemon waits about
30 seconds for its first leased client to arrive; if none does — or if, once one has, the
last lease later drops — the run pauses and the daemon exits. The grace is a startup one
only, so a client that disconnects mid-gate ends the run; attaching again resumes it. A
leased client holds an open gate as long as it likes — which is exactly what a human
thinking for a minute in a browser needs.

Pass `lease=False` only for a passive observer that will never answer a question and
must not keep the run alive.

## `Session`

- `events()` — an async iterator of `Event`. It ends when the daemon's stream ends or
  when the context exits; a clean close is an end, not an error.
- `send_input(text)` — answer the open gate.
- `interrupt(reason=None)` — interrupt the running agent, exactly what Ctrl-C does in
  the terminal.
- `aclose()` — close the connection early; leaving the context does this for you.

Gate tracking is client-side, because the daemon acknowledges nothing it receives: the
gate opens on a live `awaiting_user_input` and closes on `user_input_resolved`.
`send_input` outside that window raises `NotAtGate` locally without touching the socket,
rather than letting your text be dropped in silence.

## The event contract

`Event` carries `type`, `payload` and `raw` — the whole envelope (`id`, `parent_id`,
`agent_id`, `timestamp`, `type`, `payload`, plus `_replay` on replayed rows).

`EVENT_SCHEMA_VERSION` is `1`. It is a module constant rather than a field on the
stream, so a consumer checks it once at import time.

These types are documented as relied-upon:

| type | means |
|---|---|
| `step_started` | a workflow step began |
| `step_completed` | a workflow step finished |
| `agent_message` | a model turn landed |
| `tool_call` | an agent invoked a tool |
| `tool_result` | that tool returned |
| `agent_result` | an agent finished with its structured result |
| `awaiting_user_input` | a gate is open; `payload` carries `steer_hint` and `last_agent_message` |
| `user_input_resolved` | the gate closed and your answer was accepted |
| `workflow_paused` | the run stopped and can be resumed |
| `workflow_completed` | the run reached its end |
| `replay_complete` | the replay prefix ended; everything after it is live |

An event type you do not recognize must be ignored, never treated as an error. imp grows
new event types, and a consumer that raises on an unfamiliar one breaks on an upgrade.

## The replay handshake

On attach the daemon first replays the quest's event log, marking every replayed row
`"_replay": true` in `raw`; then it emits exactly one `replay_complete` boundary, which
is never persisted; then it streams live events.

Key on the live copy of `awaiting_user_input`. A gate typically opens about 0.6s into a
run while an attach takes about 0.65s, and the daemon re-sends the pending prompt to any
client that attaches while a gate is open — so the same gate legitimately appears once
inside the replay prefix and again live. Keying on the first sighting of the gate is a
latent bug: filter on `event.raw.get("_replay")`.

## Errors

Every one of these is an `ImpError`, so a caller can catch coarsely or finely.

| exception | raised when |
|---|---|
| `ImpError` | the base; a preflight failure (unresolvable base, unusable checkout) raises it directly |
| `QuestNotFound` | `attach()` names a quest that is not live — no such quest, or one already finished and archived |
| `DaemonUnreachable` | the session daemon could not be reached or brought up |
| `DaemonStartupError` | a subclass of `DaemonUnreachable`: the daemon started and died before it bound its socket |
| `NotAtGate` | `send_input` with no gate open |
| `InvalidVar` | a `vars=` entry the workflow does not declare, or one of the wrong type |

## Concurrency

One quest, one daemon process. This API drives daemons over a socket and never runs
imp's engine in your process, so the engine's process-global `os.chdir` into the control
root — the constraint that forbids two runs in one engine process — is untouched and
does not reach you. Attaching to several quests at once from a single application is
fine: each is its own daemon behind its own socket.

Several clients may attach to one quest at the same time — your application and a
terminal both — and each sees the same stream. A gate is one at a time per quest;
whichever client answers first resolves it.

## Security

A quest's socket lives under the system temp directory — `/tmp/imp-<uid>/<quest-id>.sock`
by default, relocated with the rest of the temp directory when `TMPDIR` is set — in a
directory imp creates and keeps at mode `0700`, and a daemon refuses to use that
directory if another account owns it. Filesystem permissions are the entire boundary:
imp binds no network port and the socket carries no authentication. Anyone who can read
that directory can drive the quest, so keep the mode as it is and keep it off shared
storage.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
