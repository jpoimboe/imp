from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from imp.cli.slash import REPROMPT, handle_slash_line
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def test_list_builtin_lists_changes_before_skill_fallback(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Live one", quest_id="live1", base_branch="main")
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/list", change=change, output_fn=out.append)
    printed = capsys.readouterr().out
    assert result is REPROMPT                 # a built-in, not a skill lookup
    assert "live1" in printed                 # the listing rendered
    assert not any("no such skill" in line for line in out)


def test_list_builtin_all_flag_includes_archived_completed(tmp_path: Path, capsys) -> None:
    # Completed changes now show by default (as `merged`); abandoned ones are the
    # default-hidden / -a-surfaced case that pins `-a` -> all=True.
    from imp.engine import archive
    for cid, status in (("doneid", "completed"), ("deadid", "abandoned")):
        events = tmp_path / f"{cid}-e.jsonl"
        events.write_text("")
        quest_json = tmp_path / f"{cid}-c.json"
        quest_json.write_text(json.dumps({
            "id": cid, "title": f"Title {cid}", "base_branch": "main",
            "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
        state_json = tmp_path / f"{cid}-s.json"
        state_json.write_text(json.dumps({"id": cid, "status": status, "step": "merge"}))
        archive.archive_quest(
            tmp_path, cid, events_path=events,
            quest_json_path=quest_json, state_json_path=state_json)
    change = SimpleNamespace(control_root=tmp_path)

    assert handle_slash_line("/list", change=change, output_fn=[].append) is REPROMPT
    default_out = capsys.readouterr().out
    assert "doneid" in default_out          # completed shows by default (merged)
    assert "deadid" not in default_out      # abandoned hidden by default

    assert handle_slash_line("/list -a", change=change, output_fn=[].append) is REPROMPT
    all_out = capsys.readouterr().out
    assert "deadid" in all_out              # -a surfaces abandoned


def test_ls_is_unknown_after_rename(tmp_path: Path) -> None:
    # `/ls` is not the `/list` builtin: it falls through the skill lookup and,
    # with no `ls` skill present, is submitted verbatim rather than listing
    # changes or erroring.
    change = SimpleNamespace(control_root=tmp_path, workflow_dir=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/ls", change=change, output_fn=out.append)
    assert result == "/ls"
    assert out == []


def test_search_builtin_prints_matching_archived_change(tmp_path: Path) -> None:
    import json

    from imp.engine import archive
    events = tmp_path / "e.jsonl"
    events.write_text("")
    quest_json = tmp_path / "c.json"
    quest_json.write_text(json.dumps({
        "id": "darkid", "title": "Dark mode toggle", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / "s.json"
    state_json.write_text(json.dumps({"id": "darkid", "status": "abandoned", "step": "spec"}))
    archive.archive_quest(
        tmp_path, "darkid", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)

    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/search dark", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("darkid" in line and "Dark mode toggle" in line for line in out)


def test_search_builtin_requires_a_query(tmp_path: Path) -> None:
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/search", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("Usage: /search" in line for line in out)


def test_abandon_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /abandon is a built-in (NOT a skill): it returns the literal so the session
    # gate loop (cli/session_terminal) confirms and sends CONTROL_ABANDON.
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/abandon", change=change, output_fn=out.append)
    assert result == "/abandon"
    assert not any("no such skill" in line for line in out)


def test_abandon_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append)
    assert any("/abandon" in line for line in lines)


def test_resume_builtin_rehydrates_resumable_match(tmp_path: Path) -> None:
    import json

    from imp.engine import archive
    events = tmp_path / "e.jsonl"
    events.write_text("")
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({
        "id": "conv1", "title": "Conv", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
        "materialized": False}))
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({"id": "conv1", "status": "paused", "step": "threshold"}))
    archive.archive_quest(
        tmp_path, "conv1", events_path=events,
        quest_json_path=cj, state_json_path=sj, resumable=True)

    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume conv1", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert (tmp_path / ".imp" / "quests" / "conv1").exists()       # rehydrated live home
    assert any("imp resume conv1" in line for line in out)          # how to continue


def test_resume_builtin_refuses_non_resumable(tmp_path: Path) -> None:
    import json

    from imp.engine import archive
    events = tmp_path / "e.jsonl"
    events.write_text("")
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({
        "id": "done1", "title": "Done", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({"id": "done1", "status": "completed", "step": "merge"}))
    archive.archive_quest(
        tmp_path, "done1", events_path=events,
        quest_json_path=cj, state_json_path=sj)                    # resumable False

    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume done1", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert not (tmp_path / ".imp" / "quests" / "done1").exists()   # never rehydrated
    assert any("not resumable" in line for line in out)


def test_resume_builtin_requires_a_query(tmp_path: Path) -> None:
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("Usage: /resume" in line for line in out)


def test_resume_builtin_no_match(tmp_path: Path) -> None:
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume zzz999", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("No Quest matches" in line for line in out)


def test_resume_builtin_ambiguous_match(tmp_path: Path) -> None:
    import json

    from imp.engine import archive

    for cid in ("conv-a", "conv-b"):
        events = tmp_path / f"{cid}.jsonl"
        events.write_text("")
        cj = tmp_path / f"{cid}.json"
        cj.write_text(json.dumps({
            "id": cid, "title": cid.title(), "base_branch": "main",
            "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
        sj = tmp_path / f"{cid}s.json"
        sj.write_text(json.dumps({"id": cid, "status": "completed", "step": "merge"}))
        archive.archive_quest(
            tmp_path, cid, events_path=events,
            quest_json_path=cj, state_json_path=sj)

    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume conv", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("ambiguous" in line for line in out)


def test_resume_builtin_live_match_skips_rehydration(tmp_path: Path) -> None:
    from tests.conftest import _seed_control
    _seed_control(tmp_path)
    from imp.engine.quest import Quest
    Quest.create_draft(tmp_path, title="Live", quest_id="live1", base_branch="main")

    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume live1", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("live" in line and "imp resume live1" in line for line in out)
    # live home is still there (not removed/re-created by rehydrate)
    assert (tmp_path / ".imp" / "quests" / "live1").exists()


def test_save_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /save is a built-in (NOT a skill): it returns the literal so the session gate
    # loop (cli/session_terminal) records the save intent and detaches.
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/save", change=change, output_fn=out.append)
    assert result == "/save"
    assert not any("no such skill" in line for line in out)


def test_save_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append)
    assert any("/save" in line for line in lines)


def test_clear_builtin_returns_literal_for_the_gate_loop(tmp_path: Path) -> None:
    # /clear is a built-in (NOT a skill): it returns the literal so the session
    # gate loop records the clear intent and detaches like Ctrl-D.
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/clear", change=change, output_fn=out.append)
    assert result == "/clear"
    assert not any("no such skill" in line for line in out)


def test_clear_is_in_the_help_text() -> None:
    from imp.cli.slash import _help
    lines: list[str] = []
    _help(lines.append)
    assert any("/clear" in line for line in lines)


def test_search_builtin_matches_archived_change_by_body_term(tmp_path: Path) -> None:
    from imp.engine import archive
    events = tmp_path / "e.jsonl"
    events.write_text(json.dumps({
        "id": "e0", "timestamp": "2026-06-01T00:00:01+00:00", "type": "llm_response",
        "payload": {"provider": "anthropic", "raw": {
            "content": [{"type": "text", "text": "the 300s deadline is enforced"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}}}}) + "\n")
    quest_json = tmp_path / "c.json"
    quest_json.write_text(json.dumps({
        "id": "quietid", "title": "", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / "s.json"
    state_json.write_text(json.dumps({"id": "quietid", "status": "abandoned", "step": "spec"}))
    archive.archive_quest(
        tmp_path, "quietid", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)

    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/search 300s", change=change, output_fn=out.append)
    assert result is REPROMPT
    # An untitled archived chat is found by a term living only in its body.
    assert any("quietid" in line for line in out)


def test_help_documents_content_search() -> None:
    change = SimpleNamespace()
    out: list[str] = []
    result = handle_slash_line("/help", change=change, output_fn=out.append)
    assert result is REPROMPT
    assert any("/search" in line and "content" in line for line in out)


def test_help_is_echoed_with_caret(tmp_path: Path) -> None:
    change = SimpleNamespace()
    out: list[str] = []
    result = handle_slash_line("/help", change=change, output_fn=out.append, color=False)
    assert result is REPROMPT
    assert out[0] == "> /help"          # caret echo heads the command's own output


def test_search_echo_precedes_its_own_output(tmp_path: Path) -> None:
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    handle_slash_line("/search", change=change, output_fn=out.append, color=False)
    assert out[0] == "> /search"        # echo first
    assert any("Usage: /search" in line for line in out[1:])   # command output below


def test_valid_model_is_not_echoed_here(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.slash as slash
    monkeypatch.setattr(slash, "validate_model_arg", lambda arg, *, change: None)
    change = SimpleNamespace()
    out: list[str] = []
    result = slash.handle_slash_line(
        "/model anthropic:claude-fable-5", change=change, output_fn=out.append,
        color=False)
    assert result == "/model anthropic:claude-fable-5"
    assert not any(line.startswith("> ") for line in out)


def test_invalid_model_is_echoed(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.slash as slash
    monkeypatch.setattr(
        slash, "validate_model_arg",
        lambda arg, *, change: "provider 'openai' is not available")
    change = SimpleNamespace()
    out: list[str] = []
    result = slash.handle_slash_line(
        "/model openai:gpt-5", change=change, output_fn=out.append, color=False)
    assert result is slash.REPROMPT
    assert out[0] == "> /model openai:gpt-5"


def test_bare_model_roster_is_echoed(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.slash as slash
    monkeypatch.setattr(
        slash, "print_model_roster",
        lambda *, change, output_fn: output_fn("Current: x"))
    change = SimpleNamespace()
    out: list[str] = []
    result = slash.handle_slash_line("/model", change=change, output_fn=out.append,
                                     color=False)
    assert result is slash.REPROMPT
    assert out[0] == "> /model"


def test_skill_and_pasted_path_are_not_command_echoed(tmp_path: Path) -> None:
    change = SimpleNamespace(workflow_dir=tmp_path, control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line(
        "/home/user/foo.py", change=change, output_fn=out.append, color=False)
    assert result == "/home/user/foo.py"
    assert out == []


def test_skills_is_echoed_with_caret(tmp_path: Path) -> None:
    change = SimpleNamespace(workflow_dir=tmp_path)   # empty skills dir -> "none" listing
    out: list[str] = []
    result = handle_slash_line("/skills", change=change, output_fn=out.append, color=False)
    assert result is REPROMPT
    assert out[0] == "> /skills"


def test_list_is_echoed_with_caret(tmp_path: Path) -> None:
    _seed_control(tmp_path)                            # _cmd_status needs a control root
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/list", change=change, output_fn=out.append, color=False)
    assert result is REPROMPT
    assert out[0] == "> /list"                         # _cmd_status prints via stdout, not out


def test_resume_is_echoed_with_caret(tmp_path: Path) -> None:
    change = SimpleNamespace(control_root=tmp_path)
    out: list[str] = []
    result = handle_slash_line("/resume", change=change, output_fn=out.append, color=False)
    assert result is REPROMPT
    assert out[0] == "> /resume"                        # echo above the usage line
    assert any("Usage: /resume" in line for line in out[1:])
