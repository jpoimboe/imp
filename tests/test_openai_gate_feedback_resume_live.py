"""Live: gate-failure feedback reaches the model on an OpenAI resume.

Regression for the co-riding-text drop bug (openai.py `_imp_to_responses_input`):
on a backward goto the executor stashes `format_script_feedback(...)` as
`State.retry_feedback`, and on the targeted step's resume the OpenAI provider
injects it via `balance_success_on_resume(..., wrapper_text=retry_feedback)` as the
is_error content of the synthesized complete_step tool_result (a function_call_output) on one user
message. The builder must emit that text as a follow-on user message; before the
fix it dropped any text sharing a user message with tool_results, so gate-failure
feedback never reached the model.

This drives a real one-retry gate-failure loop against the live Responses API and
captures the exact `input[]` sent on the wire, asserting the gate token arrives as
a `{type: message, role: user}` item (not only buried in a function_call_output,
and not dropped). Hitting the real API also proves the Responses API accepts a
function_call_output followed by a user message in the same input array — the
open question behind the chosen fix.

Skips unless `openai` is in IMP_LIVE_PROVIDERS. Costs a fraction of a cent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from tests._live import opted_in
from tests.conftest import _git

_GATE_TOKEN = "MAGIC_GATE_TOKEN_BANANA"


def _openai_key() -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".openai-key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _ensure_builtin_providers() -> None:
    # The provider registry is import-time global state; other tests clear it
    # via reset_providers_for_tests() teardown. Restore the built-ins so this
    # live test is independent of pytest-randomly ordering.
    from imp.providers import register_builtin_providers
    register_builtin_providers()


def _user_message_texts(inp: list[dict[str, Any]]) -> list[str]:
    """Every input_text on a user `message` item (NOT function_call_output)."""
    texts: list[str] = []
    for item in inp:
        if item.get("type") == "message" and item.get("role") == "user":
            for c in item.get("content", []):
                if c.get("type") == "input_text":
                    texts.append(c.get("text", ""))
    return texts


def _delivered_texts(inp: list[dict[str, Any]]) -> list[str]:
    """Every place resume feedback can ride: input_text on a user message AND the
    output of a function_call_output (post-Part-B the feedback IS the is_error
    complete_step result)."""
    texts = _user_message_texts(inp)
    for item in inp:
        if item.get("type") == "function_call_output":
            out = item.get("output", "")
            if isinstance(out, str):
                texts.append(out)
    return texts


@pytest.mark.skipif(
    not opted_in("openai"),
    reason="openai not in IMP_LIVE_PROVIDERS",
)
@pytest.mark.anyio
async def test_gate_feedback_reaches_model_on_openai_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _openai_key()
    assert key is not None, (
        "openai opted in via IMP_LIVE_PROVIDERS but no "
        "OPENAI_API_KEY env var or ~/.openai-key file found"
    )
    monkeypatch.setenv("OPENAI_API_KEY", key)

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(".imp/\n")
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\n"
        "Call complete_step with status=Success. If a previous attempt's "
        "feedback appears above, address it, then call complete_step again. "
        "Do not call any other tool.\n"
    )
    # Step `w` (agent) then `g` (script gate). The gate always fails with a
    # distinctive token on stderr and routes back to `w` (resume), so `w` runs a
    # second time WITH the gate feedback co-riding its resume turn. max_loops=1
    # caps it: w -> g(fail) -> resume w (loop=1) -> g(fail) (loop=2>1) -> pause.
    (wf / "workflow.yaml").write_text(
        "name: gate-fb\ndescription: gate feedback resume\n"
        "defaults: {max_retries: 1, max_loops: 1, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
        "      - name: g\n"
        f"        command: 'echo {_GATE_TOKEN} >&2; exit 1'\n"
        "        rules:\n"
        "          - {if: {exit: 0}, continue: true}\n"
        "          - {else: true, resume: w}\n"
    )
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\n"
        "providers: [openai]\n"
        "tiers:\n"
        "  low:    {openai: gpt-5.4-mini}\n"
        "  medium: {openai: gpt-5.4-mini}\n"
        "  high:   {openai: gpt-5.4-mini}\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")

    # Capture the exact input[] sent on the wire. Wrapping the module global is
    # faithful: openai.py:628 resolves `_imp_to_responses_input` at call time, so
    # the spy sees precisely what the real API call receives (and the real call
    # still runs, validating the shape).
    import imp.providers.openai as oai_mod
    real = oai_mod._imp_to_responses_input
    captured: list[list[dict[str, Any]]] = []

    def _spy(messages: list[Any]) -> list[dict[str, Any]]:
        out = real(messages)
        captured.append(out)
        return out

    monkeypatch.setattr(oai_mod, "_imp_to_responses_input", _spy)

    change = Change.create(
        tmp_path, title="gate-fb", quest_id="gate-fb", base_branch="main")

    # The gate never passes, so the run pauses at max_loops. Reaching PAUSED (not
    # an exception) already proves the live API accepted every request, including
    # the co-riding resume turn.
    result = await run_change(change)
    assert result is RunResult.PAUSED, f"expected PAUSED, got {result}"

    # A resume must actually have happened (>=2 LLM calls: w attempt1 + attempt2).
    assert len(captured) >= 2, (
        f"expected >=2 API calls (a retry), saw {len(captured)}"
    )

    # The gate token must reach the model as a USER MESSAGE on some call — this is
    # the co-riding feedback that the bug dropped. (The token also appears inside
    # the gate's command echo, but only the user-message delivery proves the fix.)
    delivered = any(
        _GATE_TOKEN in text
        for inp in captured
        for text in _delivered_texts(inp)
    )
    assert delivered, (
        f"gate feedback token {_GATE_TOKEN!r} never reached the model as a user "
        f"message — co-riding resume feedback was dropped"
    )
