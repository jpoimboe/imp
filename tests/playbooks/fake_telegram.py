"""Stdlib fake Telegram Bot API: getUpdates, sendMessage, /_inject, /_outbox."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

_EMPTY_POLL_PAUSE = 0.05   # pace the adapter's poll loop when no inbound is queued


class FakeTelegram:
    def __init__(self, chat_id: int = 4242) -> None:
        self.chat_id = chat_id
        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] = []   # inbound updates (FIFO, by update_id)
        self._outbox: list[dict[str, Any]] = []     # messages the bot sent
        self._next_update_id = 1
        self._next_message_id = 1
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    # --- Python API -----------------------------------------------------------
    def start(self) -> None:
        handler = _make_handler(self)
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        assert self._server is not None
        _, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def inject(self, text: str, reply_to_message_id: int | None = None,
               chat_id: int | None = None) -> int:
        with self._lock:
            return self._add_inbound(text, reply_to_message_id, chat_id)

    def inject_reply(self, text: str, reply_to_message_id: int) -> int:
        return self.inject(text, reply_to_message_id=reply_to_message_id)

    def outbox(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._outbox)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # --- internal state (lock held by caller) ---------------------------------
    def _add_inbound(self, text: str, reply_to_message_id: int | None,
                     chat_id: int | None) -> int:
        uid = self._next_update_id
        self._next_update_id += 1
        mid = self._next_message_id
        self._next_message_id += 1
        message: dict[str, Any] = {
            "message_id": mid,
            "chat": {"id": self.chat_id if chat_id is None else chat_id},
            "text": text,
        }
        if reply_to_message_id is not None:
            message["reply_to_message"] = {"message_id": reply_to_message_id}
        self._pending.append({"update_id": uid, "message": message})
        return uid

    def _get_updates(self, offset: int) -> dict[str, Any]:
        with self._lock:
            result = [u for u in self._pending if u["update_id"] >= offset]
        if not result:
            time.sleep(_EMPTY_POLL_PAUSE)   # pace the adapter's poll loop on an empty inbox
        return {"ok": True, "result": result}

    def _send_message(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            mid = self._next_message_id
            self._next_message_id += 1
            sent = {
                "message_id": mid,
                "chat_id": params.get("chat_id"),
                "text": params.get("text", ""),
                "disable_notification": params.get("disable_notification", False),
            }
            self._outbox.append(sent)
        return {"ok": True, "result": {"message_id": mid, "text": sent["text"]}}


def _make_handler(fake: FakeTelegram) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:   # silence stderr access log
            pass

        def _json(self, code: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:
            if self.path == "/_outbox":
                self._json(200, {"ok": True, "result": fake.outbox()})
            else:
                self._json(404, {"ok": False})

        def do_POST(self) -> None:
            if self.path.endswith("/getUpdates"):
                try:
                    params = self._read_json()
                except (ValueError, json.JSONDecodeError):
                    params = {}
                self._json(200, fake._get_updates(int(params.get("offset", 0))))
                return
            if self.path.endswith("/sendMessage"):
                params = self._read_json()
                self._json(200, fake._send_message(params))
                return
            if self.path == "/_inject":
                try:
                    body = self._read_json()
                    text = body["text"]
                except (ValueError, json.JSONDecodeError, KeyError, TypeError):
                    self._json(400, {"ok": False, "description": "malformed /_inject"})
                    return
                uid = fake.inject(
                    text, body.get("reply_to_message_id"), body.get("chat_id"))
                self._json(200, {"ok": True, "update_id": uid})
                return
            self._json(404, {"ok": False})

    return Handler


def main() -> None:
    import os

    fake = FakeTelegram(chat_id=int(os.environ.get("IMP_TELEGRAM_CHAT_ID", "4242")))
    fake.start()
    print(fake.url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()
