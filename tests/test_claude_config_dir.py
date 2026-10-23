"""Ephemeral imp-owned CLAUDE_CONFIG_DIRs: fresh seeding, bare creation, resume filling."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from imp.engine.env import quest_scratch_root
from imp.providers.claude_config_dir import (
    fill_resume_config_dir,
    make_config_dir,
    materialize_fresh_config_dir,
)
from imp.providers.claude_session_store import (
    resume_session_id,
    session_project_key,
    session_store_for,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_creates_isolated_projects_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No credentials at the source → nothing to seed, but the layout is created.
    src = tmp_path / "empty_home"
    src.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(src))
    cfg = materialize_fresh_config_dir()
    try:
        assert cfg.is_dir()
        assert (cfg / "projects").is_dir()
        assert not (cfg / ".credentials.json").exists()
    finally:
        shutil.rmtree(cfg, ignore_errors=True)


def test_seeds_redacted_credentials_from_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fresh subprocess runs under a redirected home; the single-use
    # refreshToken must be stripped so it can't be consumed and revoke the
    # parent's stored creds (SDK resume semantics).
    src = tmp_path / "src_home"
    src.mkdir()
    (src / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "keep-me", "refreshToken": "secret"}})
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(src))
    cfg = materialize_fresh_config_dir()
    try:
        seeded = json.loads((cfg / ".credentials.json").read_text())
        assert seeded["claudeAiOauth"]["accessToken"] == "keep-me"
        assert "refreshToken" not in seeded["claudeAiOauth"]
    finally:
        shutil.rmtree(cfg, ignore_errors=True)


def test_fresh_config_dir_routes_under_imp_quest_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With IMP_QUEST_TMP set (the session-daemon case), the ephemeral config dir
    lands under the per-quest dir — so it is reaped with the quest — instead of the
    ambient system temp."""
    src = tmp_path / "empty_home"
    src.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(src))
    qtmp = tmp_path / "qtmp"
    qtmp.mkdir()  # daemon creates quest.tmp_dir before provisioning
    monkeypatch.setenv("IMP_QUEST_TMP", str(qtmp))

    cfg = materialize_fresh_config_dir()
    try:
        assert cfg.parent == qtmp                  # under the per-quest dir, not ambient temp
    finally:
        shutil.rmtree(cfg, ignore_errors=True)


def test_make_config_dir_is_private_bare_and_under_the_scratch_root() -> None:
    cfg = make_config_dir("claude-resume-")
    try:
        assert cfg.parent == quest_scratch_root()
        assert cfg.name.startswith("claude-resume-")
        assert cfg.stat().st_mode & 0o777 == 0o700
        assert sorted(os.listdir(cfg)) == ["projects"]  # no auth copied at creation
        assert (cfg / "projects").is_dir()
    finally:
        shutil.rmtree(cfg, ignore_errors=True)


def test_make_config_dir_leaves_nothing_behind_when_its_mkdir_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scan the per-test temp root, not a shared one.
    monkeypatch.delenv("IMP_QUEST_TMP", raising=False)

    def _fail(self: Path, *_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", _fail)  # mkdtemp uses os.mkdir, so only projects/ fails
    with pytest.raises(OSError, match="disk full"):
        make_config_dir("claude-resume-")
    assert not [p for p in os.listdir(quest_scratch_root()) if p.startswith("claude-resume-")]


@pytest.mark.anyio
async def test_fill_resume_config_dir_writes_session_subagents_and_redacted_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "src_home"
    src.mkdir()
    (src / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "keep-me", "refreshToken": "secret"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(src))
    store = session_store_for(tmp_path / "step")
    pkey = session_project_key(tmp_path)
    sid = resume_session_id("step:p/s")
    key = {"project_key": pkey, "session_id": sid}
    main = [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": "hi"}},
    ]
    await store.overwrite(key, main)
    sub_line = {"type": "user", "uuid": "s1", "isSidechain": True,
                "message": {"role": "user", "content": "sub"}}
    await store.append({**key, "subpath": "subagents/agent-a1b2"}, [
        sub_line, {"type": "agent_metadata", "agentType": "general-purpose"}])

    cfg = make_config_dir("claude-resume-")
    try:
        await fill_resume_config_dir(cfg, store, pkey, sid)

        project_dir = cfg / "projects" / pkey
        lines = (project_dir / f"{sid}.jsonl").read_text().splitlines()
        assert [json.loads(x) for x in lines] == main
        sub_dir = project_dir / sid / "subagents"
        sub_lines = (sub_dir / "agent-a1b2.jsonl").read_text().splitlines()
        assert [json.loads(x) for x in sub_lines] == [sub_line]
        meta = json.loads((sub_dir / "agent-a1b2.meta.json").read_text())
        assert meta == {"agentType": "general-purpose"}
        creds = json.loads((cfg / ".credentials.json").read_text())
        assert creds["claudeAiOauth"]["accessToken"] == "keep-me"
        assert "refreshToken" not in creds["claudeAiOauth"]
        assert cfg.stat().st_mode & 0o777 == 0o700
    finally:
        shutil.rmtree(cfg, ignore_errors=True)
