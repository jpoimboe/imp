from __future__ import annotations

import threading


class Focus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._change_id: str | None = None

    def current(self) -> str | None:
        with self._lock:
            return self._change_id

    def focus_on(self, change_id: str) -> None:
        with self._lock:
            self._change_id = change_id

    def clear(self) -> None:
        with self._lock:
            self._change_id = None

    def clear_if(self, change_id: str) -> None:
        with self._lock:
            if self._change_id == change_id:
                self._change_id = None
