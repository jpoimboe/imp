from __future__ import annotations

import json
from pathlib import Path

from imp.engine import archive
from imp.engine.archive import read as archive_read


def _archive(
    tmp_path: Path, quest_id: str, title: str, status: str = "abandoned",
    events_text: str = "",
) -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text(events_text)
    quest_json = tmp_path / f"{quest_id}.change.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{quest_id}.state.json"
    state_json.write_text(json.dumps({"id": quest_id, "status": status, "step": "spec"}))
    archive.archive_quest(
        tmp_path, quest_id, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _prose_line(text: str) -> str:
    return json.dumps({
        "id": "e0", "timestamp": "2026-06-01T00:00:01+00:00", "type": "llm_response",
        "payload": {"provider": "anthropic", "raw": {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}}}}) + "\n"


def _user_msg_line(text: str, *, human: bool) -> str:
    return json.dumps({
        "id": "u0", "timestamp": "2026-06-01T00:00:01+00:00", "type": "user_message",
        "payload": {"content": [{"type": "text", "text": text}], "human": human}}) + "\n"


def test_search_archive_matches_title_substring(tmp_path: Path) -> None:
    _archive(tmp_path, "alpha", "Dark mode toggle")
    _archive(tmp_path, "beta", "Login flow")
    hits = archive_read.search_archive(tmp_path, "dark")
    assert [h[0] for h in hits] == ["alpha"]          # title filter, not a list-all
    assert hits[0][1] == "Dark mode toggle"           # title carried in the row
    assert hits[0][2] == "abandoned"                  # status carried in the row


def test_search_archive_matches_id_substring_case_insensitively(tmp_path: Path) -> None:
    _archive(tmp_path, "beta", "Login flow")
    assert [h[0] for h in archive_read.search_archive(tmp_path, "BET")] == ["beta"]


def test_search_archive_no_match_returns_empty(tmp_path: Path) -> None:
    _archive(tmp_path, "alpha", "Dark mode toggle")
    assert archive_read.search_archive(tmp_path, "zzz") == []


def test_search_archive_missing_index_returns_empty(tmp_path: Path) -> None:
    assert archive_read.search_archive(tmp_path, "anything") == []


def test_search_archive_treats_percent_as_literal(tmp_path: Path) -> None:
    # A `%` in the query must match a literal percent, not act as a wildcard.
    _archive(tmp_path, "alpha", "Dark mode toggle")
    _archive(tmp_path, "pct", "50% faster")
    _archive(tmp_path, "other", "50 items")
    assert [h[0] for h in archive_read.search_archive(tmp_path, "50%")] == ["pct"]


def test_search_archive_matches_untitled_chat_by_body_term(tmp_path: Path) -> None:
    # An untitled Q&A chat: empty title, the term lives only in the body.
    _archive(tmp_path, "quiet", "", events_text=_prose_line("the 300s deadline holds"))
    _archive(tmp_path, "other", "Login flow")
    assert [h[0] for h in archive_read.search_archive(tmp_path, "300s")] == ["quiet"]


def test_search_archive_content_percent_is_literal(tmp_path: Path) -> None:
    # A `%` in the query must match a literal percent inside body content.
    _archive(tmp_path, "pct", "", events_text=_prose_line("throughput up 50% today"))
    _archive(tmp_path, "plain", "", events_text=_prose_line("50 items processed"))
    assert [h[0] for h in archive_read.search_archive(tmp_path, "50%")] == ["pct"]


def test_search_content_carries_conversation_body(tmp_path: Path) -> None:
    _archive(tmp_path, "quiet", "", events_text=_prose_line("the 300s deadline holds"))
    _archive(tmp_path, "other", "Login flow")
    hits = archive_read.search_archive_content(tmp_path, "300s")
    assert [h[0] for h in hits] == ["quiet"]           # body term filters
    assert hits[0][1] == ""                            # title carried (empty here)
    assert "300s deadline holds" in hits[0][2]         # content column carried for excerpting


def test_search_content_matches_title_and_returns_triples(tmp_path: Path) -> None:
    _archive(tmp_path, "alpha", "Dark mode toggle")
    hits = archive_read.search_archive_content(tmp_path, "dark")
    assert [h[0] for h in hits] == ["alpha"]
    assert hits[0][1] == "Dark mode toggle"
    assert len(hits[0]) == 3                            # (id, title, content), no status/created


def test_search_content_missing_index_returns_empty(tmp_path: Path) -> None:
    assert archive_read.search_archive_content(tmp_path, "anything") == []


def test_search_content_percent_is_literal(tmp_path: Path) -> None:
    # A `%` in the query must match a literal percent, not act as a wildcard.
    _archive(tmp_path, "pct", "", events_text=_prose_line("throughput up 50% today"))
    _archive(tmp_path, "plain", "", events_text=_prose_line("50 items processed"))
    assert [h[0] for h in archive_read.search_archive_content(tmp_path, "50%")] == ["pct"]


def test_search_content_surfaces_human_reply_not_injected_prompt(tmp_path: Path) -> None:
    _archive(tmp_path, "reply", "",
             events_text=_user_msg_line("please use HUMANTERM", human=True))
    _archive(tmp_path, "prompt", "",
             events_text=_user_msg_line("cover-check INJECTEDTERM", human=False))
    # The human reply is indexed and searchable (term is in neither id nor title)...
    assert [h[0] for h in
            archive_read.search_archive_content(tmp_path, "HUMANTERM")] == ["reply"]
    # ...but the injected prompt term matches nothing (excluded from content).
    assert archive_read.search_archive_content(tmp_path, "INJECTEDTERM") == []
