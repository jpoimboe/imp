from __future__ import annotations

import subprocess
import sys

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import Message, TextBlock, ThinkingBlock, ToolUseBlock
from imp.agents.transcript import Transcript, repair_dangling_tool_use_on_resume
from imp.engine.replay import parse_message


def test_parse_message_dispatches_by_provider() -> None:
    msg = parse_message("anthropic",
                        {"content": [{"type": "text", "text": "x"}]})
    assert msg == Message(role="assistant", content=(TextBlock(text="x"),))


def test_repair_backfills_every_dangling_tool_use_not_just_the_last(tmp_path) -> None:
    # AC9: a suspended BATCH leaves an assistant message whose [task1, task2, task3]
    # tool_uses are ALL dangling (multiple dangling tool_uses, not a trailing one).
    # repair_dangling_tool_use_on_resume must synthesize a placeholder result for
    # EACH, restoring the tool_use<->tool_result balance for the whole batch.
    assistant = Message(role="assistant", content=(
        ToolUseBlock(id="t1", name="sub_agent", input={}),
        ToolUseBlock(id="t2", name="sub_agent", input={}),
        ToolUseBlock(id="t3", name="sub_agent", input={})))
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        assistant]
    transcript = Transcript(tmp_path / "transcript.jsonl")
    synth = repair_dangling_tool_use_on_resume(messages, transcript)
    assert synth is not None
    repaired_ids = sorted(b.tool_use_id for b in synth)
    assert repaired_ids == ["t1", "t2", "t3"]


def test_stall_redrive_balances_torn_down_turn_no_replay(tmp_path) -> None:
    # D5 / AC7: a torn-down turn (unmatched tool_use tail) must be repaired
    # before re-drive, never replayed unbalanced.
    torn_down = Message(role="assistant", content=(
        TextBlock(text="calling a tool"),
        ToolUseBlock(id="t1", name="bash", input={"command": "sleep 99999"})))
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        torn_down]

    # Pre-repair: replaying this tail unbalanced is the 400-triggering shape.
    assert classify_resume(messages) is ResumeState.DANGLING_TOOL_USE

    transcript = Transcript(tmp_path / "transcript.jsonl")
    synth = repair_dangling_tool_use_on_resume(messages, transcript)

    assert synth is not None
    assert [b.tool_use_id for b in synth] == ["t1"]
    # Post-repair: the dangling tool_use is paired, so the re-drive sends a balanced transcript.
    assert classify_resume(messages) is not ResumeState.DANGLING_TOOL_USE
    assert classify_resume(messages) is ResumeState.SEND_TO_LLM


def test_parse_message_dispatches_each_provider_to_its_own_wire_format() -> None:
    """Each registry key must reach the parser for that provider's wire format,
    not merely *a* parser: anthropic (and scripted, which shares it) preserves
    thinking blocks, claude_code/codex drop them, and openai reads Responses
    `output[]` rather than `content[]`."""
    blocks_raw = {"content": [
        {"type": "thinking", "thinking": "hmm", "signature": "sig"},
        {"type": "text", "text": "hi"}]}
    for provider in ("anthropic", "scripted"):
        assert parse_message(provider, blocks_raw).content == (
            ThinkingBlock(thinking="hmm", signature="sig"),
            TextBlock(text="hi"),
        ), provider
    for provider in ("claude_code", "codex"):
        assert parse_message(provider, blocks_raw).content == (
            TextBlock(text="hi"),
        ), provider

    responses_raw = {"output": [
        {"type": "message", "content": [{"type": "output_text", "text": "hi"}]},
        {"type": "function_call", "call_id": "c1", "name": "read_file",
         "arguments": '{"p": "x"}'}]}
    assert parse_message("openai", responses_raw).content == (
        TextBlock(text="hi"),
        ToolUseBlock(id="c1", name="read_file", input={"p": "x"}),
    )
    # The same payload through the anthropic parser yields nothing — proof the
    # openai entry is not silently bound to the wrong transform.
    assert parse_message("anthropic", responses_raw).content == ()


def test_building_the_parse_table_imports_no_provider_module() -> None:
    """replay's dispatch table is on the read-only CLI path (imp list -> store ->
    render_helpers -> replay); importing it must not pull a provider module, and
    with it httpx. Fresh interpreter: in-process, other tests have already
    imported them."""
    code = (
        "import sys, imp.engine.replay\n"
        "watched = {'imp.providers.anthropic', 'imp.providers.openai',\n"
        "           'imp.providers.claude', 'imp.providers.codex',\n"
        "           'imp.providers.codex_runner', 'imp.providers.scripted',\n"
        "           'httpx'}\n"
        "print('LEAKED:' + ','.join(sorted(watched & sys.modules.keys())))\n"
        "print('PARSERS:' + ','.join(sorted(imp.engine.replay._MESSAGE_PARSERS)))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    reported = {
        key: value
        for key, _, value in (ln.partition(":") for ln in proc.stdout.splitlines())
        if key in {"LEAKED", "PARSERS"}
    }
    assert reported.keys() == {"LEAKED", "PARSERS"}, (
        f"child did not report both lines; stdout was {proc.stdout!r}"
    )
    assert reported["PARSERS"] == "anthropic,claude_code,codex,openai,scripted"
    assert reported["LEAKED"] == "", (
        f"importing imp.engine.replay loaded: {reported['LEAKED']}"
    )


def test_the_relocated_parsers_leave_no_re_export_shim() -> None:
    import imp.providers.claude as cc
    import imp.providers.codex_runner as cr
    from imp.providers import message_parsers

    assert not hasattr(cc, "_parse_claude_code_message")
    assert not hasattr(cr, "_parse_codex_message")
    assert callable(message_parsers._parse_claude_code_message)
    assert callable(message_parsers._parse_codex_message)
