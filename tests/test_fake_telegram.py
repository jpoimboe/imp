from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from tests.playbooks.fake_telegram import FakeTelegram


def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_inject_then_getupdates_drains_the_fifo() -> None:
    fake = FakeTelegram()
    fake.start()
    try:
        fake.inject("hello")
        result = _post(f"{fake.url}/botTOK/getUpdates", {"offset": 0})["result"]
        assert result[0]["message"]["text"] == "hello"
        assert result[0]["message"]["chat"]["id"] == fake.chat_id
    finally:
        fake.stop()


def test_getupdates_honors_the_offset() -> None:
    fake = FakeTelegram()
    fake.start()
    try:
        fake.inject("one")
        fake.inject("two")
        first = _post(f"{fake.url}/botTOK/getUpdates", {"offset": 0})["result"]
        next_offset = first[0]["update_id"] + 1
        rest = _post(f"{fake.url}/botTOK/getUpdates", {"offset": next_offset})["result"]
        assert [u["message"]["text"] for u in rest] == ["two"]
    finally:
        fake.stop()


def test_sendmessage_records_outbox_with_monotonic_ids() -> None:
    fake = FakeTelegram()
    fake.start()
    try:
        r1 = _post(f"{fake.url}/botTOK/sendMessage", {"chat_id": fake.chat_id, "text": "a"})
        r2 = _post(f"{fake.url}/botTOK/sendMessage", {"chat_id": fake.chat_id, "text": "b"})
        assert r2["result"]["message_id"] == r1["result"]["message_id"] + 1
        assert [m["text"] for m in fake.outbox()] == ["a", "b"]
    finally:
        fake.stop()


def test_http_outbox_endpoint_returns_sent_messages() -> None:
    import urllib.request as _ur
    fake = FakeTelegram()
    fake.start()
    try:
        _post(f"{fake.url}/botTOK/sendMessage", {"chat_id": fake.chat_id, "text": "x"})
        with _ur.urlopen(f"{fake.url}/_outbox", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data["ok"] is True
        assert data["result"][0]["text"] == "x"
    finally:
        fake.stop()


def test_inject_reply_threads_the_reply_to() -> None:
    fake = FakeTelegram()
    fake.start()
    try:
        fake.inject_reply("yes", reply_to_message_id=99)
        result = _post(f"{fake.url}/botTOK/getUpdates", {"offset": 0})["result"]
        assert result[0]["message"]["reply_to_message"]["message_id"] == 99
    finally:
        fake.stop()


def test_http_inject_posts_inbound_and_getupdates_returns_it() -> None:
    fake = FakeTelegram()
    fake.start()
    try:
        r = _post(f"{fake.url}/_inject", {"text": "via http"})
        assert r["ok"] is True
        result = _post(f"{fake.url}/botTOK/getUpdates", {"offset": 0})["result"]
        assert result[0]["message"]["text"] == "via http"
    finally:
        fake.stop()


def test_malformed_inject_returns_400() -> None:
    fake = FakeTelegram()
    fake.start()
    try:
        req = urllib.request.Request(
            f"{fake.url}/_inject", data=b"not json",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected HTTP 400")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        fake.stop()
