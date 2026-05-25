"""imp-provided tools (spec §10).

complete_step is intentionally absent — the design pivot to response_format=
(review #5) replaces it. Agents declare their output schema; the LLM produces
the structured response as its terminal turn.

Each tool is a factory taking per-Change context (events path, project root);
the runner calls these per-invocation.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from imp.engine.state import EventLog


class _QueryEventsArgs(BaseModel):
    type: str | None = Field(default=None, description="Filter by event type.")
    limit: int | None = Field(default=None, description="Most recent N events.")


def build_query_events_tool(events_path: Path) -> BaseTool:
    @tool("query_events", args_schema=_QueryEventsArgs)
    def query_events(type: str | None = None, limit: int | None = None) -> str:
        """Return events.jsonl as JSON. Optionally filter by type, limit to last N."""
        log = EventLog(events_path)
        events = list(log.read())
        if type:
            events = [e for e in events if e["type"] == type]
        if limit:
            events = events[-limit:]
        return json.dumps(events, default=str)

    return query_events


class _WebFetchArgs(BaseModel):
    url: str = Field(..., description="URL to GET.")
    timeout: float = Field(default=30.0, description="Timeout in seconds.")


def build_web_fetch_tool() -> BaseTool:
    @tool("web_fetch", args_schema=_WebFetchArgs)
    def web_fetch(url: str, timeout: float = 30.0) -> str:
        """GET a URL, return text. HTTP errors → readable error string."""
        try:
            with httpx.Client() as c:
                r = c.get(url, timeout=timeout)
                if r.status_code >= 400:
                    return f"error: HTTP {r.status_code}"
                return r.text
        except httpx.HTTPError as e:
            return f"error: {type(e).__name__}: {e}"

    return web_fetch


class _GitDiffArgs(BaseModel):
    staged: bool = Field(default=False, description="`git diff --staged` if True.")


def build_git_diff_tool(project_root: Path) -> BaseTool:
    @tool("git_diff", args_schema=_GitDiffArgs)
    def git_diff(staged: bool = False) -> str:
        """`git diff` (or `git diff --staged`). Returns diff or error string."""
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--staged")
        try:
            proc = subprocess.run(
                cmd, cwd=project_root, capture_output=True, text=True,
                check=False,
            )
        except FileNotFoundError:
            return "error: git not installed"
        if proc.returncode != 0:
            stderr = (proc.stderr or "").lower()
            if "not a git" in stderr:
                return "error: not a git repository"
            return f"error: git diff failed: {proc.stderr.strip()}"
        return proc.stdout

    return git_diff
