"""In-process streamable-HTTP MCP server for the codex provider (design
"Components → codex_mcp.py", "Concurrency / threading model — VALIDATED").

The server runs as an asyncio task on the runner's own event loop — never via
`mcp.run()` or `uvicorn.run()`. Each imp Tool is exposed through a thin async
wrapper that:
  - intercepts `complete_step` to capture args in `RunState` (the runner reads
    them at the next turn boundary, never via the call-stack return);
  - dispatches all other tools through `Tool.async_invoke(ctx, args)` against
    the live `ToolContext`;
  - checks the run-wide dispatch cap *before* doing work — the cap RMW is a
    single-coroutine read-modify-write with NO `await` between read and
    decision (invariant; preserves no-TOCTOU on the cap under interleaving).

`RunState` is shared with the runner's consume loop; both run on the same
event loop so locks are not required at `await` boundaries.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from dataclasses import dataclass, field
from typing import Any, Literal

from imp.agents.parallel import max_parallel_tasks
from imp.agents.runner import ContentPart, ImagePart, TextPart
from imp.agents.tools import Tool
from imp.agents.tools import bg as _bg
from imp.agents.tools.context import ToolContext, suspend_inactivity_watchdog
from imp.agents.tools.errors import FatalToolError

# Bypass posture for the codex provider. This is the SAME value the ThreadOptions
# builder feeds to approvalPolicy (codex_runner._build_thread_options); the task
# readOnlyHint below is DERIVED from it so a future approval mode that flips this
# constant drops the hint automatically (design §2 honesty note). codex_runner
# imports this name to keep the two sites in lockstep.
CODEX_APPROVAL_POLICY: Literal["never"] = "never"


def _task_annotations() -> Any | None:
    """ToolAnnotations(readOnlyHint=True) for the task tool ONLY, and only when
    the bypass posture is on AND the width knob permits parallelism. readOnlyHint
    is not literally true (a subagent may edit files); its only operative effect
    in our bypass-approval wiring is unlocking the CLI's MCP parallel scheduling.
    Width gate: IMP_MAX_PARALLEL_TASKS=1 drops the hint, restoring serial dispatch
    end-to-end (Decision 5 kill switch)."""
    from mcp.types import ToolAnnotations
    bypass = CODEX_APPROVAL_POLICY == "never"
    if bypass and max_parallel_tasks() > 1:
        return ToolAnnotations(readOnlyHint=True)
    return None


def _readonly_annotations() -> Any | None:
    """ToolAnnotations(readOnlyHint=True) for a read_only tool. Honest hint, so
    NO posture gate (width-only). IMP_MAX_PARALLEL_TASKS=1 drops it."""
    from mcp.types import ToolAnnotations
    if max_parallel_tasks() > 1:
        return ToolAnnotations(readOnlyHint=True)
    return None


@dataclass
class RunState:
    """Shared state between the MCP tool wrappers and the runner's consume loop.

    `complete_step_args` is set inside the wrapper *before the response is
    serialized*, so the runner sees the captured args by the time the matching
    `turn.completed` event arrives (design "complete_step capture").

    `dispatch_count` and `cap` are the whole-`run()` budget (NOT per-turn). One
    MCP server spans all in-turn nudges/resumes (design "Cap scope").

    `abort_requested` is set whenever a cap trip fires; the runner observes it
    and routes the abort through `terminate_running_processes(ctx)` so
    governed `bash` children don't leak (N2).
    """
    cap: int
    dispatch_count: int = 0
    complete_step_called: bool = False
    complete_step_args: dict[str, Any] = field(default_factory=dict)
    abort_requested: bool = False
    runtime_errors: list[str] = field(default_factory=list)
    fatal_tool_error: tuple[str, str] | None = None
    agent_id: str = "step:unnamed"
    killed_tool_uses: list[dict[str, Any]] = field(default_factory=list)
    # Runner's event loop, recorded at runner entry. start_server raises on a
    # foreign loop (same-loop topology pin). None disables the check.
    runner_loop: Any = None


def _parts_to_mcp_content(parts: list[ContentPart]) -> list[Any]:
    # Real mcp.types objects (not dicts): FastMCP's _convert_to_content passes a
    # ContentBlock straight through but JSON-stringifies a plain dict, so an
    # ImagePart must become an ImageContent to reach the model as an image.
    from mcp.types import ImageContent, TextContent
    blocks: list[Any] = []
    for p in parts:
        if isinstance(p, TextPart):
            blocks.append(TextContent(type="text", text=p.text))
        else:
            blocks.append(ImageContent(type="image", data=p.data, mimeType=p.media_type))
    return blocks


async def dispatch_tool(
    *, tool: Tool, args: dict[str, Any], ctx: ToolContext, state: RunState,
) -> str | list[Any]:
    """Invoke `tool` against `ctx` with `args`, returning a textual result
    suitable for an MCP tool response body.

    Cap RMW invariant: the count is incremented and compared synchronously
    here — there is NO `await` between the read (`state.dispatch_count`) and
    the decision (`> state.cap`). Future maintainers MUST keep these two lines
    adjacent and synchronous.
    """
    # Decision 10 sweep site: EVERY MCP dispatch — the codex analog of
    # agents/dispatch.py's first-statement sweep. Placed BEFORE the cap RMW;
    # the RMW's two lines below stay adjacent and synchronous (their invariant
    # is about each other, not about preceding awaits).
    await _bg._sweep_bg(ctx)
    # ── cap RMW (no await between read and decision) ──────────────────────────
    state.dispatch_count += 1
    if state.dispatch_count > state.cap:
        state.abort_requested = True
        return (
            f"tool_call_ceiling_exceeded (cap={state.cap}); run is being aborted. "
            "No further tool calls will be honored."
        )
    # ─────────────────────────────────────────────────────────────────────────
    if tool.name == "complete_step":
        # Capture args; return OK so codex moves on to turn.completed. The
        # runner reads `state.complete_step_args` at that boundary, not via
        # a call-stack return (design "complete_step capture").
        state.complete_step_called = True
        # The dynamic arg-model (_register_imp_tool) defaults every non-required
        # property to None (PydanticField(None)), so model_dump materializes
        # e.g. spec_path=None even when codex omitted it. Drop None-valued keys
        # so complete_step_args reflects what the agent ACTUALLY sent —
        # otherwise output-schema validation rejects a present-but-None optional
        # field ("expected string, got NoneType"). imp output types are never
        # nullable, so a None here always means "field not provided".
        state.complete_step_args = {k: v for k, v in args.items() if v is not None}
        return "step complete"
    if tool.name == "sub_agent":
        # task special-case: never goes through async_invoke (its _invoke raises
        # TaskMustBeAwaited by design). Run the subagent and return its structured
        # output JSON. dispatch_tool does NOT touch the event log for the task
        # tool_call/result pair — process_event owns that lifecycle, keyed on the
        # SDK item id (item.started opens the fold, item.completed closes it). An
        # exception (ordinary failure OR SubagentSuspended) propagates out of this
        # handler; FastMCP wraps it as an isError tool result, so a batched child
        # cannot pause the run (Quest 1). A SubagentSuspended is re-raised with a
        # bracketed [interrupted] marker so the FastMCP-wrapped result text lets the
        # renderer classify the fold as (interrupted), not (failed) — the result
        # text is the only channel codex controls here.
        from imp.agents.subagents import SubagentSuspended, dispatch_task
        try:
            return await dispatch_task(
                ctx,
                args["subagent_name"],
                args["task_description"],
                tier=args.get("tier"),
            )
        except SubagentSuspended as exc:
            raise SubagentSuspended(f"{exc} [interrupted]") from exc
    try:
        with suspend_inactivity_watchdog(ctx):
            raw = await tool.async_invoke(ctx, args)
    except FatalToolError as fte:
        state.fatal_tool_error = (tool.name, str(fte))
        return f"fatal tool error: {fte}"
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        state.runtime_errors.append(text)
        return text
    if tool.name == "bash" and isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            rc = parsed.get("returncode") if isinstance(parsed, dict) else None
            if isinstance(rc, int) and rc < 0:
                state.killed_tool_uses.append(
                    {"id": "", "name": "bash", "signal": -rc})
        except (ValueError, TypeError):
            pass
    if (isinstance(raw, list) and raw
            and all(isinstance(p, (TextPart, ImagePart)) for p in raw)):
        return _parts_to_mcp_content(raw)
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, default=str)
    except (TypeError, ValueError):
        return str(raw)


# ── Server (FastMCP on ephemeral port with bearer-token auth) ─────────────────


@dataclass
class ServerHandle:
    """Live MCP server handle: host, resolved port, full URL, the running
    asyncio task, and a shutdown coroutine."""
    host: str
    port: int
    url: str
    task: asyncio.Task  # type: ignore[type-arg]
    _shutdown_cb: Any  # callable returning a coroutine

    async def shutdown(self) -> None:
        await self._shutdown_cb()


def _reserve_socket(host: str) -> tuple[socket.socket, int]:
    """Bind a socket to port 0, returning (socket, resolved_port).
    The socket is kept open and must be passed to `server.serve(sockets=[sock])`
    so uvicorn uses it directly — no close-then-rebind race."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, 0))
    port = s.getsockname()[1]
    return s, port


async def _serve_fastmcp(
    mcp_app: Any, *, bearer_token: str, host: str, runner_loop: Any = None,
    name: str,
) -> ServerHandle:
    """Serve a FastMCP app over bearer-auth'd streamable-HTTP on an ephemeral port,
    as an asyncio task on the CURRENT event loop (the same-loop topology pin)."""
    import uvicorn

    if (runner_loop is not None
            and asyncio.get_running_loop() is not runner_loop):
        raise RuntimeError(
            "codex MCP server must start on the runner's event loop "
            "(same-loop topology pin); refusing to start on a foreign loop")

    # streamable-HTTP transport on FastMCP returns a Starlette app suitable
    # for uvicorn. The mount path defaults to `/mcp`.
    asgi_app = mcp_app.streamable_http_app()
    # Bearer-token middleware: 401 on missing/wrong Authorization header.
    asgi_app = _bearer_auth_middleware(asgi_app, expected=bearer_token)

    sock, port = _reserve_socket(host)
    config = uvicorn.Config(
        asgi_app, host=host, port=port, log_level="warning",
        # lifespan MUST be on: FastMCP's streamable-HTTP app attaches its
        # `StreamableHTTPSessionManager.run()` as the ASGI lifespan; with
        # lifespan="off" every request raises "Task group is not initialized".
        access_log=False, lifespan="on",
    )
    server = uvicorn.Server(config)
    # MUST disable signal handler installation: signal handlers can only be
    # installed on the main-thread event loop; we run as a child task on the
    # runner's loop which may be on any thread.
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign,assignment]
    task = asyncio.create_task(server.serve(sockets=[sock]), name=name)
    # Do NOT close `sock` here — asyncio holds it and closes it on shutdown.

    # Give uvicorn a moment to bind and start accepting connections.
    await asyncio.sleep(0.2)

    async def _shutdown() -> None:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    return ServerHandle(
        host=host, port=port, url=f"http://{host}:{port}/mcp",
        task=task, _shutdown_cb=_shutdown,
    )


async def start_server(
    *, tools: dict[str, Tool], ctx: ToolContext, state: RunState,
    bearer_token: str, host: str = "127.0.0.1",
) -> ServerHandle:
    """Start the imp FastMCP/uvicorn server as an asyncio task on the CURRENT
    event loop (design "Concurrency / threading model — VALIDATED").

    Key invariants:
      - uvicorn's `capture_signals()` is replaced with a no-op context manager
        so signal handlers are never installed on a non-main-thread event loop.
      - Bearer-token auth gate on the streamable-HTTP endpoint; an unauth'd
        request returns 401 *before* a tool is dispatched (cross-UID defense
        — same-UID is in-trust-boundary).
    """
    from mcp.server.fastmcp import FastMCP

    mcp_app = FastMCP("imp")
    # Register each tool as a FastMCP tool. The wrapper goes through
    # `dispatch_tool` so the cap counter + complete_step capture are
    # uniform with the unit-test path.
    # Mirrors the claude_code strict_mcp_config note: this loop is where imp
    # exposes its OWN tools to codex. Adding a first-party server here (dict entry
    # + mcp__<name>__* allowlist + this codex mirror) is not a posture reversal —
    # strict_mcp_config blocks only .mcp.json / user-settings servers, not these.
    for tool in tools.values():
        _register_tool(mcp_app, tool, ctx=ctx, state=state)
    return await _serve_fastmcp(
        mcp_app, bearer_token=bearer_token, host=host,
        runner_loop=state.runner_loop, name="imp-codex-mcp-server")


async def start_impty_server(
    *, driver: Any, bearer_token: str, host: str = "127.0.0.1",
    runner_loop: Any = None,
) -> ServerHandle:
    """Start a second FastMCP("impty") hosting the ImptyDriver's tools on its own
    ephemeral port + bearer token, reusing the imp server's uvicorn machinery."""
    from impty.mcp import build_server

    return await _serve_fastmcp(
        build_server(driver, thin=True), bearer_token=bearer_token, host=host,
        runner_loop=runner_loop, name="imp-codex-impty-server")


def _register_tool(
    mcp_app: Any, tool: Tool, *, ctx: ToolContext, state: RunState,
) -> None:
    """Hook a single imp Tool into FastMCP with its JSON schema intact.

    FastMCP introspects the function signature to generate the JSON schema, so
    a plain `**kwargs` wrapper produces an incorrect schema.  Instead we:
      1. Build a Pydantic model from the tool's `input_schema` properties,
         subclassing FastMCP's `ArgModelBase` so `call_fn_with_arg_validation`
         can validate incoming arguments and unpack them by name.
      2. Directly construct an MCP `Tool` object — bypassing `from_function` —
         so the `parameters` field carries the original `input_schema` verbatim.
      3. Register the `Tool` directly in the tool-manager's registry.

    The `_impl(**kwargs)` function receives unpacked field values from
    `model_dump_one_level()` and forwards them to `dispatch_tool`.
    """
    from mcp.server.fastmcp.tools.base import Tool as MCPTool
    from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
    from pydantic import Field as PydanticField
    from pydantic import create_model

    tool_ref = tool

    # ── Build a typed arg-model from the tool's JSON schema ───────────────────
    props = (tool.input_schema or {}).get("properties", {})
    required_fields: set[str] = set((tool.input_schema or {}).get("required", []))

    field_defs: dict[str, Any] = {}
    for fname in props:
        if fname in required_fields:
            field_defs[fname] = (Any, PydanticField(...))
        else:
            field_defs[fname] = (Any, PydanticField(None))

    ArgModel = create_model(
        f"{tool.name}Arguments", __base__=ArgModelBase, **field_defs
    )
    fn_meta = FuncMetadata(arg_model=ArgModel)

    # ── Wrapper: receives unpacked kwargs from model_dump_one_level() ─────────
    async def _impl(**kwargs: Any) -> Any:
        return await dispatch_tool(tool=tool_ref, args=kwargs, ctx=ctx, state=state)

    # ── Register directly so FastMCP sees the original input_schema ───────────
    mcp_tool = MCPTool(
        fn=_impl,
        name=tool.name,
        title=None,
        description=tool.description or "",
        parameters=tool.input_schema or {"type": "object", "properties": {}},
        fn_metadata=fn_meta,
        is_async=True,
        context_kwarg=None,
        # task: posture+width gated. read_only tools: width-only honest hint.
        # Every other tool stays unannotated.
        annotations=(
            _task_annotations() if tool.name == "sub_agent"
            else _readonly_annotations() if tool.read_only
            else None
        ),
    )
    mcp_app._tool_manager._tools[tool.name] = mcp_tool


def _bearer_auth_middleware(app: Any, *, expected: str) -> Any:
    """Wrap an ASGI app to require `Authorization: Bearer <expected>` on every
    HTTP request. Anything else → 401."""
    expected_header = f"Bearer {expected}".encode("ascii")

    async def _wrapped(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if headers.get(b"authorization") != expected_header:
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body",
                        "body": b"unauthorized"})
            return
        await app(scope, receive, send)

    return _wrapped
