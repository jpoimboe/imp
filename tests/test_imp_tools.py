"""imp-provided tools: query_events, web_fetch, git_diff."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import httpx

from imp.agents.tools import (
    build_git_diff_tool, build_query_events_tool, build_web_fetch_tool,
)
from imp.engine.state import EventLog


def test_query_events_returns_event_list(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append("step_started", {"step": "a"})
    log.append("agent_output", {"status": "Success"})
    tool = build_query_events_tool(tmp_path / "events.jsonl")
    data = json.loads(tool.invoke({}))
    assert len(data) == 2
    assert data[0]["type"] == "step_started"


def test_query_events_type_filter(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append("step_started", {"step": "a"})
    log.append("agent_output", {"status": "Success"})
    log.append("step_started", {"step": "b"})
    tool = build_query_events_tool(tmp_path / "events.jsonl")
    data = json.loads(tool.invoke({"type": "step_started"}))
    assert len(data) == 2
    assert all(d["type"] == "step_started" for d in data)


def test_query_events_empty_log(tmp_path: Path) -> None:
    tool = build_query_events_tool(tmp_path / "events.jsonl")
    assert json.loads(tool.invoke({})) == []


def test_web_fetch_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_url = []

    class _Client:
        def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
            pass

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *a):  # type: ignore[no-untyped-def]
            return False

        def get(self, url, timeout):  # type: ignore[no-untyped-def]
            captured_url.append(url)
            req = httpx.Request("GET", url)
            return httpx.Response(200, text="hello from server", request=req)

    monkeypatch.setattr(httpx, "Client", _Client)
    tool = build_web_fetch_tool()
    out = tool.invoke({"url": "https://example.test/"})
    assert "hello from server" in out
    assert captured_url == ["https://example.test/"]


def test_web_fetch_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
            pass

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *a):  # type: ignore[no-untyped-def]
            return False

        def get(self, url, timeout):  # type: ignore[no-untyped-def]
            req = httpx.Request("GET", url)
            return httpx.Response(404, text="not found", request=req)

    monkeypatch.setattr(httpx, "Client", _Client)
    tool = build_web_fetch_tool()
    out = tool.invoke({"url": "https://example.test/"})
    assert "404" in out or "error" in out.lower()


def test_git_diff_unstaged(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("hello\nworld\n")

    tool = build_git_diff_tool(tmp_path)
    out = tool.invoke({})
    assert "+world" in out


def test_git_diff_staged(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)

    tool = build_git_diff_tool(tmp_path)
    out = tool.invoke({"staged": True})
    assert "+hello" in out


def test_git_diff_no_repo(tmp_path: Path) -> None:
    tool = build_git_diff_tool(tmp_path)
    out = tool.invoke({})
    assert "not a git" in out.lower() or "error" in out.lower()
