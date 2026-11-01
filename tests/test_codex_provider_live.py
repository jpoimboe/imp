"""Live e2e for the codex provider, gated behind `--run-live` (codex).
Runs codex under the EXACT production flags: `sandbox_mode="read-only"`, with
imp's own MCP servers auto-approved via config.toml's
`default_tools_approval_mode` — the combination the unit tests can't validate.

Assertions:
  - >= 1 `mcp_tool_call` (server=imp) item completes (proves the codex->server
    round-trip works under the read-only sandbox -- M-1 / m-2).
  - Zero `web_search` items appear (confirms the config.toml disable holds).
  - Cap-abort: forcing a cap trip terminates within the bounded retry budget.
  - Tool exposure: codex's rollout carries direct `function_call`s rather than
    the code-mode `exec` indirection, and an image returned by an imp tool
    still reaches the model under `[features] view_image = false`.

Skipped unless `--run-live` selects codex and its `codex` CLI is detected on
this machine (capability detection at collection).
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from imp.agents.runner import AgentInvocation, QuestContext
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.completion import build_continuation_fn
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_rollout import CODEX_ROLLOUT_SLOTS
from imp.providers.codex_runner import CodexAgentRunner
from tests._live import native_denial_miss, stale_codex_oauth_skip_reason

pytestmark = pytest.mark.live("codex")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _git_init(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "T"], check=True)
    (p / "README.md").write_text("# t\n")
    subprocess.run(["git", "-C", str(p), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-m", "init", "-q"], check=True)


_SRC = SourceLoc(file=Path("a.md"), line=1)


def _inv(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=AgentDef(
            name="echoer",
            description="repeat the secret word",
            tools=frozenset({"complete_step"}),
            subagents=(),
            output_schema=OutputSchema(
                fields=(OutputField(name="word", type="string", required=True),),
                source=_SRC,
            ),
            prompt_template_source="",
            source_path=Path("a.md"),
            source=_SRC,
            tier=None,
        ),
        provider_name="codex",
        model_name="gpt-6-luna",
        system_prompt=(
            'Reply by calling complete_step with word="banana". Do not call '
            "any other tools and do not write any files."
        ),
        initial_messages=(),
        quest_context=QuestContext(
            title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1",
        ),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
    )


# How many independent round-trip attempts before the test gives up. The codex
# "tool is not exposed in this session" refusal was root-caused to codex hiding
# imp's MCP tools behind a discovery indirection the model had to walk first;
# imp now sets `[features.code_mode] direct_only_tool_namespaces` so they are
# declared directly, and `_build_prompt` says so. The round-trip is reliable in
# production via that, the nudge backstop (continuation_fn), and the engine's
# RetryNow policy for `agent_output_missing`. The multi-attempt loop stays as
# cheap belt-and-suspenders against residual LLM nondeterminism — it almost
# always passes on the first attempt now.
_ROUND_TRIP_ATTEMPTS = 5


@pytest.mark.anyio
async def test_codex_live_round_trip_under_production_flags(tmp_path: Path) -> None:
    _git_init(tmp_path)
    runner = CodexAgentRunner(model_name="gpt-6-luna")
    ev_lines: list[str] = []
    tool_calls: list[Any] = []
    last_events: list[Any] = []
    parked_attempts = 0
    for attempt in range(_ROUND_TRIP_ATTEMPTS):
        # Fresh per-attempt event log + transcript so a failed attempt's events
        # don't bleed into the assertions on the successful one.
        adir = tmp_path / f"attempt{attempt}"
        (adir / "tx").mkdir(parents=True)
        inv = dataclasses.replace(
            _inv(tmp_path),
            transcript_dir=adir / "tx",
            events_path=adir / "events.jsonl",
            # Nudge backstop, exactly as the engine wires it in production: a turn
            # that ends without complete_step is re-prompted rather than pausing for
            # input. Per attempt, because the one-shot bit is closure state.
            continuation_fn=build_continuation_fn(headless=True, has_schema=True),
        )
        events: list[Any] = []
        try:
            async with contextlib.aclosing(runner.run(inv)) as _run:
                async for ev in _run:
                    events.append(ev)
                    if ev.get("type") == "agent_result":
                        break
        except AwaitingUserInput:
            # Headless nudge-then-park backstop fired: the model ended two turns
            # without complete_step. That is a non-completing attempt — the same
            # failure a quiet loop exit produces, surfacing as an exception — so
            # record the partial events and retry rather than aborting the test.
            parked_attempts += 1
            last_events = events
            reason = stale_codex_oauth_skip_reason(events)
            if reason is not None:
                pytest.skip(reason)
            continue
        last_events = events
        reason = stale_codex_oauth_skip_reason(events)
        if reason is not None:
            pytest.skip(reason)
        ev_path = adir / "events.jsonl"
        ev_lines = ev_path.read_text().splitlines() if ev_path.exists() else []
        tool_calls = [
            json.loads(ln) for ln in ev_lines
            if ln and json.loads(ln).get("type") == "tool_call"
        ]
        # Success = the round-trip completed: agent_result yielded AND at least
        # one tool_call recorded (proves the codex->server round-trip works
        # under the read-only sandbox).
        if any(e.get("type") == "agent_result" for e in events) and tool_calls:
            break
    else:
        raise AssertionError(
            f"codex never completed the round-trip in {_ROUND_TRIP_ATTEMPTS} "
            f"attempts ({parked_attempts} parked after the nudge; external "
            f"codex/LLM nondeterminism). Last attempt's events: {last_events}"
        )

    # The web_search assertions hold on any attempt; check them on the winner.
    warns = [
        json.loads(ln) for ln in ev_lines
        if ln and json.loads(ln).get("type") == "runner_warning"
    ]
    # No *unexpected web_search tool* tripwire (web_search is disabled via
    # config.toml for this non-declaring agent).
    assert all(
        "unexpected codex web_search" not in str(w.get("payload", {})) for w in warns
    )
    # And the codex >=0.137 `features.web_search_request` deprecation `error`
    # item must NOT appear: imp stopped emitting the deprecated SDK flag and now
    # controls web_search via config.toml's top-level key (Finding A/B fix).
    assert all(not w.get("payload", {}).get("codex_error_item") for w in warns), warns
    assert all(
        "web_search_request" not in str(w.get("payload", {})) for w in warns
    ), warns


@pytest.mark.anyio
async def test_codex_live_cap_abort_terminates_within_budget(tmp_path: Path) -> None:
    """Force a real cap trip end-to-end and assert the loop TERMINATES (does
    not hang) within the bounded retry budget.

    The dispatch cap is "max ALLOWED calls": `dispatch_count > cap` trips, so
    `cap = max_tool_calls = 1` permits exactly one tool call. A single-call
    agent (the round-trip echoer) can never trip it — its lone `complete_step`
    is that one allowed call. (Note `cap = max_tool_calls or 1000` also makes
    cap=0 unreachable.) So to exercise the ceiling we give the agent a second
    read-only tool (`glob`) and instruct it to call that FIRST, then
    `complete_step`: dispatch #1 (`glob`) is allowed, dispatch #2 (`complete_step`)
    trips the cap. The runner then aborts the SDK and yields
    `tool_call_ceiling_exceeded` on its next event iteration — independent of
    what the model does next. The mechanism itself (RMW + abort flag) is
    unit-covered in test_codex_mcp_dispatch.py; this is the live proof that a
    real codex turn terminates under cap pressure rather than looping."""
    _git_init(tmp_path)
    (tmp_path / "tx").mkdir()
    runner = CodexAgentRunner(model_name="gpt-6-luna")
    inv = _inv(tmp_path)
    # Give the agent a second (read-only) tool so it can make two calls, and
    # order them so the SECOND call (complete_step) is the one that trips cap=1.
    agent_def = dataclasses.replace(
        inv.agent_def, tools=frozenset({"complete_step", "glob"})
    )
    inv = dataclasses.replace(
        inv,
        agent_def=agent_def,
        max_tool_calls=1,
        system_prompt=(
            "First call the `glob` tool with pattern \"*\". After it returns, call "
            'complete_step with word="banana". Call `glob` before complete_step. '
            "Do not call any other tools and do not write any files."
        ),
        # Nudge backstop, as production wires it: a turn that ends without a tool
        # call is re-prompted instead of pausing, so the model reliably calls
        # `glob` and then complete_step, tripping cap=1 on dispatch #2. Without it
        # the codex/LLM tool refusal ("tool not exposed in this session") can end
        # the turn with no `glob` call at all, leaving the cap unpressured and the
        # run terminating with `no_complete_step` instead — a flake.
        continuation_fn=build_continuation_fn(
            headless=True,
            has_schema=True,
        ),
    )
    try:
        events = [ev async for ev in runner.run(inv)]
    except AwaitingUserInput:
        # Headless nudge-then-park is a clean, bounded termination — it proves
        # the non-hang invariant exactly as a terminal error event does. A park
        # emits no `error` event, so accept it and skip the error-kind check.
        return
    reason = stale_codex_oauth_skip_reason(events)
    if reason is not None:
        pytest.skip(reason)
    # Terminated — and within the bounded budget (test runner has its own
    # session timeout; reaching here at all means we terminated). The core claim
    # is non-hang under cap pressure; the cap trip (tool_call_ceiling_exceeded)
    # is the expected path, but any terminal error proves termination. Kinds are
    # listed by their real `kind` value (NOT the `agent_output_missing` category
    # an earlier version mistakenly checked here).
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None, events
    assert err["payload"]["kind"] in (
        "tool_call_ceiling_exceeded",      # expected: cap tripped on dispatch #2
        "no_complete_step",                # model ended turn without completing
    ), err


_PROBE_OVERRIDE = """

SANDBOX PROBE — this section overrides the tool policy above, for this run only.
This is a deliberate probe of the sandbox. You MUST attempt all three writes
below, in order, even though the policy above forbids native tools. Attempting
them is the entire point of this run; being refused is the expected result and
is NOT a failure — do not skip a step because you expect it to fail.

1. Use your NATIVE `apply_patch` tool to change the single line in `probe.txt`
   from `KEEP` to `PATCHED`. Copy the exact error text it returns.
2. Use your NATIVE shell/exec tool (`exec_command` on codex 0.153.2) to run
   this shell command in the working directory: printf 'x' > exec_written.txt
   Copy the exact error text it returns.
3. Use the `write_file` tool to write the file `mcp_written.txt`
   with exactly the content `MCP-OK`.

Then call `complete_step` with `patch_error` set to step 1's verbatim
error text and `exec_error` set to step 2's verbatim error text.
Report it VERBATIM — do not summarise or paraphrase it.
If a step somehow succeeded, set that field to the literal text
`NO ERROR — IT SUCCEEDED`.
"""

_PROBE_ATTEMPTS = 5


class _ProbeRunner(CodexAgentRunner):
    """Appends the probe override AFTER `_build_prompt`'s tool policy, so the
    last instruction the model reads is the one telling it to attempt the
    natives."""

    def _build_prompt(self, invocation: AgentInvocation, elog: Any, aid: str) -> str:
        return super()._build_prompt(invocation, elog, aid) + _PROBE_OVERRIDE


def _probe_inv(root: Path) -> AgentInvocation:
    inv = _inv(root)
    agent_def = dataclasses.replace(
        inv.agent_def,
        name="prober",
        description="probe the sandbox",
        tools=frozenset({"complete_step", "write_file"}),
        output_schema=OutputSchema(
            fields=(
                OutputField(name="patch_error", type="string", required=True),
                OutputField(name="exec_error", type="string", required=True),
            ),
            source=_SRC,
        ),
    )
    return dataclasses.replace(
        inv,
        agent_def=agent_def,
        system_prompt=(
            "You are probing a sandbox. Follow the SANDBOX PROBE section "
            "exactly and report what each step returned."
        ),
        continuation_fn=build_continuation_fn(headless=True, has_schema=True),
    )


@pytest.mark.anyio
async def test_codex_live_refuses_native_writes_but_allows_mcp_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 5: both native write vectors — `apply_patch` and a shell write — are
    refused, while an imp MCP write lands. Measured on 0.153.2 / gpt-5.6-luna:
    `apply_patch` is refused by the read-only sandbox and `exec_command` is not
    bound at all (`[features] shell_tool = false`).
    """
    last_events: list[Any] = []
    notes: list[str] = []
    for attempt in range(_PROBE_ATTEMPTS):
        root = tmp_path / f"attempt{attempt}"
        root.mkdir()
        _git_init(root)
        (root / "probe.txt").write_text("KEEP\n")
        subprocess.run(["git", "-C", str(root), "add", "probe.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "probe"], check=True)
        (root / "tx").mkdir()
        # imp never sets ThreadOptions.working_directory, so codex inherits THIS
        # process's cwd. Without this the natives would target the imp checkout
        # and every file assertion below would be vacuous.
        monkeypatch.chdir(root)

        events: list[Any] = []
        try:
            async with contextlib.aclosing(
                    _ProbeRunner(model_name="gpt-6-luna").run(
                        _probe_inv(root))) as run:
                async for ev in run:
                    events.append(ev)
                    if ev.get("type") == "agent_result":
                        break
        except AwaitingUserInput:
            last_events = events
            reason = stale_codex_oauth_skip_reason(events)
            if reason is not None:
                pytest.skip(reason)
            notes.append(f"#{attempt + 1} parked awaiting input")
            continue
        last_events = events
        reason = stale_codex_oauth_skip_reason(events)
        if reason is not None:
            pytest.skip(reason)
        result = next(
            (e for e in events if e.get("type") == "agent_result"), None)
        if result is None:
            notes.append(f"#{attempt + 1} no agent_result")
            continue

        # (a) the apply_patch target is byte-identical.
        assert (root / "probe.txt").read_bytes() == b"KEEP\n"
        # (b) the exec target was never created.
        assert not (root / "exec_written.txt").exists()
        # (c) the MCP write landed, with its expected contents.
        assert (root / "mcp_written.txt").read_text().strip() == "MCP-OK"
        # Positive evidence that each native leg was ATTEMPTED and denied —
        # without this, (a) and (b) also pass when the model never tried. A
        # report the model paraphrased is a bad draw, not a regression: spend
        # an attempt on it rather than aborting the whole test.
        miss = native_denial_miss(
            str(result["payload"]["patch_error"]),
            str(result["payload"]["exec_error"]))
        if miss is None:
            return
        notes.append(f"#{attempt + 1} {miss}")

    raise AssertionError(
        f"codex never produced a completing probe run with native-denial "
        f"evidence in {_PROBE_ATTEMPTS} attempts (external codex/LLM "
        f"nondeterminism). Attempts: {'; '.join(notes)}. "
        f"Last events: {last_events}")


_OMITTED_MARKER = "BANANA-42"
# read_file returns this stub instead of the content when the same (offset,
# limit) window of an unchanged file is re-read within one ToolContext.
_DEDUP_STUB_PREFIX = "[unchanged since you read it"


def _reader_inv(root: Path) -> AgentInvocation:
    inv = _inv(root)
    agent_def = dataclasses.replace(
        inv.agent_def,
        name="reader",
        description="read one file",
        tools=frozenset({"complete_step", "read_file"}),
    )
    return dataclasses.replace(
        inv,
        agent_def=agent_def,
        system_prompt=(
            "Call the `read_file` tool with ONLY the `path` argument, set to "
            "`marker.txt`. Do not pass `offset` and do not pass `limit` — omit "
            "both arguments entirely. Then call complete_step with `word` set "
            "to the exact text of that file's single line. Do not call any "
            "other tools and do not write any files."
        ),
        continuation_fn=build_continuation_fn(headless=True, has_schema=True),
    )


def _call_args(payload: dict[str, Any]) -> dict[str, Any]:
    """`tool_call` payloads store the SDK's `arguments` under `"input"` verbatim —
    a dict on 0.153.2 but a JSON string on other shapes."""
    raw = payload.get("input") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


@pytest.mark.anyio
async def test_codex_live_omitted_optional_arg_reaches_python_default(
    tmp_path: Path,
) -> None:
    """AC 10: a real codex turn calls `read_file` without `offset`; the recorded
    tool_result must carry the file's contents and `error: false`, not the
    `offset must be an integer >= 1, got None` failure the fabricated null used
    to produce. A call where the model volunteered `offset`, or whose result is
    the re-read dedup stub, leaves the precondition unmet — keep scanning
    rather than assert."""
    last_events: list[Any] = []
    for attempt in range(_ROUND_TRIP_ATTEMPTS):
        root = tmp_path / f"attempt{attempt}"
        root.mkdir()
        _git_init(root)
        (root / "marker.txt").write_text(f"{_OMITTED_MARKER}\n")
        (root / "tx").mkdir()

        events: list[Any] = []
        try:
            async with contextlib.aclosing(
                    CodexAgentRunner(model_name="gpt-6-luna").run(
                        _reader_inv(root))) as run:
                async for ev in run:
                    events.append(ev)
                    if ev.get("type") == "agent_result":
                        break
        except AwaitingUserInput:
            last_events = events
            reason = stale_codex_oauth_skip_reason(events)
            if reason is not None:
                pytest.skip(reason)
            continue
        last_events = events
        reason = stale_codex_oauth_skip_reason(events)
        if reason is not None:
            pytest.skip(reason)

        ev_path = root / "events.jsonl"
        recs = [
            json.loads(ln)
            for ln in (ev_path.read_text().splitlines() if ev_path.exists() else [])
            if ln.strip()
        ]
        results = {r["payload"]["id"]: r["payload"]
                   for r in recs if r.get("type") == "tool_result"}
        for rec in recs:
            if (rec.get("type") != "tool_call"
                    or rec["payload"].get("name") != "read_file"):
                continue
            if _call_args(rec["payload"]).get("offset") is not None:
                continue
            result = results.get(rec["payload"]["id"])
            if result is None:
                continue
            content = str(result["content"])
            if content.startswith(_DEDUP_STUB_PREFIX):
                continue
            assert result["error"] is False, result
            assert _OMITTED_MARKER in content, result
            return

    raise AssertionError(
        f"codex never called read_file with `offset` omitted in "
        f"{_ROUND_TRIP_ATTEMPTS} attempts (external codex/LLM "
        f"nondeterminism). Last events: {last_events}")


# The digits drawn into the probe image. Nothing in the prompt names them, so a
# model that never received the image cannot report them.
_IMAGE_CODE = "742"
_IMAGE_ATTEMPTS = 3


def _marker_png() -> bytes:
    im = Image.new("RGB", (60, 20), "white")
    ImageDraw.Draw(im).text((6, 5), _IMAGE_CODE, fill="black")
    # PIL's default bitmap font is ~11px; upscale so the digits are legible.
    im = im.resize((480, 160), Image.NEAREST)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _viewer_inv(root: Path) -> AgentInvocation:
    inv = _inv(root)
    agent_def = dataclasses.replace(
        inv.agent_def,
        name="viewer",
        description="look at one image",
        tools=frozenset({"complete_step", "read_file"}),
        output_schema=OutputSchema(
            fields=(OutputField(name="code", type="string", required=True),),
            source=_SRC,
        ),
    )
    return dataclasses.replace(
        inv,
        agent_def=agent_def,
        # read_file's vision branch stores the image as a blob under the quest
        # home; without quest_dir it raises instead of returning the image.
        quest_context=QuestContext(
            title="t", project_root=root, workflow_dir=root, id="c1",
            quest_dir=root / "quest"),
        system_prompt=(
            "Call the `read_file` tool with `path` set to `marker.png`. That "
            "file is a picture of three digits. Then, in a plain message, "
            "write the sentence `The digits are <D>.` with <D> replaced by the "
            "three digits shown in the picture. Then call complete_step with "
            "`code` set to those same three digits and nothing else. Do not "
            "call any other tools and do not write any files."
        ),
        continuation_fn=build_continuation_fn(headless=True, has_schema=True),
    )


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _response_items(rollout: Path) -> list[dict[str, Any]]:
    """codex's rollout records the model wire, one JSON object per line; tool calls
    sit in `response_item` payloads, which the `--json` stream flattens away."""
    return [
        rec["payload"] for rec in _records(rollout)
        if rec.get("type") == "response_item" and isinstance(rec.get("payload"), dict)
    ]


def _agent_prose(events_path: Path) -> str:
    return "\n".join(
        block.get("text", "")
        for rec in _records(events_path) if rec.get("type") == "agent_message"
        for block in rec["payload"]["raw"]["content"]
    )


@pytest.mark.anyio
async def test_codex_live_exposes_imp_tools_directly_and_passes_images(
    tmp_path: Path,
) -> None:
    """AC 8: one real codex turn proves the exposure flip took effect (rollout
    `function_call`s, no `exec` custom_tool_call), that imp logged the call and
    its result, that complete_step's structured args were captured, and that an
    image returned by an imp tool still reaches the MODEL under `[features]
    view_image = false`."""
    notes: list[str] = []
    last_events: list[Any] = []
    for attempt in range(_IMAGE_ATTEMPTS):
        root = tmp_path / f"attempt{attempt}"
        root.mkdir()
        _git_init(root)
        (root / "marker.png").write_bytes(_marker_png())
        (root / "tx").mkdir()

        events: list[Any] = []
        try:
            async with contextlib.aclosing(
                    CodexAgentRunner(model_name="gpt-6-luna").run(
                        _viewer_inv(root))) as run:
                async for ev in run:
                    events.append(ev)
                    if ev.get("type") == "agent_result":
                        break
        except AwaitingUserInput:
            last_events = events
            reason = stale_codex_oauth_skip_reason(events)
            if reason is not None:
                pytest.skip(reason)
            notes.append(f"#{attempt + 1} parked awaiting input")
            continue
        last_events = events
        reason = stale_codex_oauth_skip_reason(events)
        if reason is not None:
            pytest.skip(reason)
        result = next((e for e in events if e.get("type") == "agent_result"), None)
        if result is None:
            notes.append(f"#{attempt + 1} no agent_result")
            continue
        recs = _records(root / "events.jsonl")
        read = next((r["payload"] for r in recs if r.get("type") == "tool_call"
                     and r["payload"].get("name") == "read_file"), None)
        if read is None:
            # The model never looked at the image, so there is nothing to assert.
            notes.append(f"#{attempt + 1} never called read_file")
            continue

        # (a) The exposure flip took effect. Only the rollout shows it: the
        # `--json` stream emits the same mcp_tool_call items either way. Reading
        # it here is safe because the `async with` above closed the generator,
        # running the teardown that persists the rollout.
        items = _response_items(root / "tx" / CODEX_ROLLOUT_SLOTS[0])
        named = {p.get("name"): p for p in items if p.get("type") == "function_call"}
        assert named.keys() >= {"read_file", "complete_step"}, [
            (p.get("type"), p.get("name")) for p in items]
        assert named["read_file"].get("namespace") == "mcp__imp", named["read_file"]
        assert not [p for p in items if p.get("type") == "custom_tool_call"
                    and p.get("name") == "exec"], items
        # (b) imp logged the call and its result off the --json stream.
        results = {r["payload"]["id"]: r["payload"]
                   for r in recs if r.get("type") == "tool_result"}
        assert results[read["id"]]["error"] is False, results[read["id"]]
        # (c) complete_step was captured with its structured arguments.
        assert set(result["payload"]) == {"code"}, result
        # The digit VALUES are a model draw — one spike attempt OCR'd this
        # byte-identical PNG as 712 — so a misread spends an attempt instead of
        # failing. The structural claims above stay hard: those are regressions.
        if _IMAGE_CODE not in str(result["payload"]["code"]):
            notes.append(
                f"#{attempt + 1} completed with code "
                f"{result['payload']['code']!r}, not {_IMAGE_CODE}")
            continue
        # (d) The image reached the MODEL, not merely the tool result: the digits
        # appear nowhere in the prompt, so the model can only have read them off
        # the picture.
        if _IMAGE_CODE not in _agent_prose(root / "events.jsonl"):
            notes.append(f"#{attempt + 1} digits absent from the agent's prose")
            continue
        return

    raise AssertionError(
        f"codex never completed an image round-trip in {_IMAGE_ATTEMPTS} "
        f"attempts (external codex/LLM nondeterminism). Attempts: "
        f"{'; '.join(notes)}. Last events: {last_events}")
