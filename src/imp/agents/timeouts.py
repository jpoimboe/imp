"""Watchdog / teardown timing constants shared by the engine drive loop and the
tool layer.

Deliberately stdlib-only — no `imp` imports, not even anyio — so
`imp.engine.agent_run` can read them without importing the `imp.agents.tools`
package, whose `__init__` costs pydantic and every builtin tool module.
"""

from __future__ import annotations

# B.2 is INDEPENDENT of B.1: the coordinator suspends the inactivity watchdog
# for the full duration of every foreground tool call (begin/end_tool_watchdog;
# see agent_run._pull_with_inactivity_watchdog and cli/signals.py), so B.2 never
# runs concurrently with a foreground tool and no longer has to outlast B.1. A
# runaway foreground tool is bounded by B.1 alone. B.2 is therefore a short
# freeze-detection window: catch a wedged agent (no bytes flowing from the
# model, no tool running) fast. tests/test_tool_limits.py pins the decoupling.
#
# This window measures PROCESS LIVENESS, not semantic-event cadence: every
# provider pulses coordinator.note_activity() once per received model event
# (claude_code partial StreamEvents; codex streamed events; the anthropic and
# openai streaming requests), so a long-but-streaming turn keeps resetting the
# deadline while a genuinely wedged producer (zero bytes) trips it. 300 s gives
# ~5x headroom over realistic model time-to-first-token while catching a true
# wedge ~3.7x faster than the interim window. Held below
# BG_WAIT_MAX_S (1200) < TOOL_WALLCLOCK_TIMEOUT_S (1800) — both in
# imp.agents.tools.limits — so the existing decoupling-order tests still hold.
AGENT_INACTIVITY_TIMEOUT_S = 300.0       # B.2: per-activity no-events window
# The finite inactivity ceiling for the request-in-flight-awaiting-response
# state: while imp has issued a model request (or handed a tool result back) and
# is awaiting the provider's next response event, the watchdog uses THIS window
# instead of the 300 s general one. Distinct from the 300 s window
# (AGENT_INACTIVITY_TIMEOUT_S) and from the math.inf freeze the solicit/tool
# states use. Every legitimate provider wait observed topped out ~9 min, so 15
# min never false-kills a healthy wait; the finite ceiling still eventually tears
# down a genuinely dead-but-open connection, narrowing the watchdog rather than
# gutting it. Held strictly between AGENT_INACTIVITY_TIMEOUT_S (300) and
# TOOL_WALLCLOCK_TIMEOUT_S (1800) — tests/test_tool_limits.py pins that ordering.
PROVIDER_REQUEST_SILENCE_CEILING_S = 900.0   # request-in-flight silence ceiling (15 min)
PROCESS_KILL_GRACE_S = 4.0               # C-ii: SIGTERM -> wait -> SIGKILL grace
