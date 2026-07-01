"""Isolate the claude_code one-step flake and A/B test writer prompts.

Replicates tests/test_default_workflow_e2e_live_claude_code.py exactly, but:
  - runs N iterations in fresh tmp dirs,
  - lets you swap the writer.md prompt body via WRITER_PROMPT,
  - on a PAUSE (the flake), dumps what the model actually did from events.jsonl.

Usage:
  IMP_LIVE_PROVIDERS=claude_code PYTHONPATH=src \
    python3 scratchpad/isolate_flake.py [N] [variant]

variant: "baseline" (default) or "improved" (edit IMPROVED below).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.providers import register_builtin_providers

BASELINE = (
    "Call complete_step with status=Success. Do not call any other tool.\n"
)

# Edit this to test a prompt improvement:
IMPROVED = (
    "This is a self-contained step. There is nothing to investigate, no "
    "context or skill to load, and nothing to ask about — all needed "
    "information is already in this prompt.\n\n"
    "Your only action is to call the `complete_step` tool with "
    "status=Success.\n\n"
    "Do NOT ask questions. Do NOT write prose. Do NOT call load_skill or any "
    "other tool. Your first and only tool call is `complete_step`.\n"
)

VARIANTS = {"baseline": BASELINE, "improved": IMPROVED}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _setup(root: Path, writer_prompt: str) -> Change:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\n" + writer_prompt
    )
    (wf / "workflow.yaml").write_text(
        "name: live-cc\ndescription: One-step test\n"
        "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: a\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (root / "imp" / "project.yaml").write_text(
        "default-workflow: imp\n"
        "providers: [claude_code]\n"
        "tiers:\n"
        "  low:    {claude_code: claude-haiku-4-5-20251001}\n"
        "  medium: {claude_code: claude-haiku-4-5-20251001}\n"
        "  high:   {claude_code: claude-haiku-4-5-20251001}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return Change.create(root, title="live-cc", change_id="live-cc",
                         base_branch="main")


def _dump_transcript(change: Change) -> None:
    p = change.change_dir / "events.jsonl"
    if not p.exists():
        print("  (no events.jsonl)")
        return
    KEEP = {"thinking", "text", "tool_use", "tool_result", "error",
            "runner_warning", "step_completed", "agent_output"}
    for line in p.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t not in KEEP:
            continue
        body = ev.get("name") or ev.get("text") or ev.get("thinking") \
            or ev.get("content") or ev.get("action") or ev.get("error") or ""
        s = json.dumps(body) if not isinstance(body, str) else body
        print(f"  [{t}] {s[:300]}")


async def main() -> None:
    register_builtin_providers()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    variant = sys.argv[2] if len(sys.argv) > 2 else "baseline"
    prompt = VARIANTS[variant]
    print(f"=== variant={variant}  N={n} ===")
    print(f"prompt body: {prompt!r}\n")
    paused = 0
    for i in range(n):
        d = Path(tempfile.mkdtemp(prefix="flake_"))
        try:
            change = _setup(d, prompt)
            result = await run_change(change)
            ok = result is RunResult.COMPLETED
            print(f"[{i:02d}] {result.value}")
            if not ok:
                paused += 1
                _dump_transcript(change)
                # preserve failing dir alongside this harness for inspection
                keep = Path(__file__).parent / f"fail_{variant}_{i}"
                shutil.copytree(d / ".imp", keep / ".imp",
                                dirs_exist_ok=True)
                print(f"  preserved -> {keep}")
        finally:
            shutil.rmtree(d, ignore_errors=True)
    print(f"\n=== {variant}: {paused}/{n} PAUSED "
          f"({100*paused/n:.0f}% flake) ===")


if __name__ == "__main__":
    asyncio.run(main())
