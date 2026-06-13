"""Full-stream offline replay golden + no-stall invariant.

Deterministic offline replay of the complete 2026-06-03-imp-new-open-by-asking
journaled run through the real NarrativeBuilder/LiveDriver: golden snapshots of
the live + log bodies, plus a no-stall invariant generalizing the §11 guard to
the whole run.

Depends on Tasks 6 (§8 tool counter), 7 (§11 stall), 8 (§7 phase breadcrumb),
9 (§9 markdown) — all committed — so the goldens encode the FULLY-FIXED render.
"""
from __future__ import annotations

import gzip
import io
import json

from imp.cli import narrative as nv
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_text import render_tree
from tests.conftest import FIXTURES

_GZ = FIXTURES / "cc_open_by_asking_full.jsonl.gz"


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _events() -> list[dict]:
    with gzip.open(_GZ, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _live_body(events) -> str:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for e in events:
        drv.on_event(e)
    drv.finish()  # force-flush the final block
    return out.getvalue()


def _log_body(events) -> str:
    return "\n".join(render_tree(nv.build_tree(events), full=False, color=False, width=80)) + "\n"


_LIVE_REFRESH = (
    "live render drift. To accept intended changes, regenerate:\n"
    "  python -c \""
    "from tests.test_full_stream_view import _events, _live_body; "
    "open('tests/fixtures/open_by_asking_live.txt','w',encoding='utf-8')"
    ".write(_live_body(_events()))\""
)

_LOG_REFRESH = (
    "log render drift. To accept intended changes, regenerate:\n"
    "  python -c \""
    "from tests.test_full_stream_view import _events, _log_body; "
    "open('tests/fixtures/open_by_asking_log.txt','w',encoding='utf-8')"
    ".write(_log_body(_events()))\""
)


def test_live_body_matches_golden() -> None:
    actual = _live_body(_events())
    golden = (FIXTURES / "open_by_asking_live.txt").read_text(encoding="utf-8")
    assert actual == golden, _LIVE_REFRESH


def test_log_body_matches_golden() -> None:
    actual = _log_body(_events())
    golden = (FIXTURES / "open_by_asking_log.txt").read_text(encoding="utf-8")
    assert actual == golden, _LOG_REFRESH


def test_no_stall_invariant_final_breadcrumb_present() -> None:
    # After replaying ALL events + finish(), the live body must contain the final
    # step's completion breadcrumb — generalizes the §11 guard to the whole run.
    events = _events()
    last_step = None
    for e in events:
        if e.get("type") == "step_started":
            last_step = (e.get("payload") or {}).get("step")
    assert last_step is not None
    live = _live_body(events)
    # The final step "final-review" completes with a breadcrumb like
    # "final-review complete · 2983s · ..."; we check the step name prefix only so the
    # test is robust to timing changes in future replays.
    assert f"{last_step} complete" in live, (
        f"final step '{last_step}' completion breadcrumb missing from live output "
        f"— the live cursor may be wedged mid-stream (§11 stall regression)"
    )


def test_live_log_body_parity() -> None:
    # change_merged is intentionally suppressed in the live path (main.py owns
    # that line), so exclude it from the parity comparison.
    events = [e for e in _events() if e.get("type") != "change_merged"]
    assert _live_body(events) == _log_body(events), (
        "live and log bodies diverged — live/log parity invariant broken"
    )
