# imp capabilities

A catalog of what imp can do, one capability per section.

## Drive a run from your phone

imp streams a run's events to a chat surface (Telegram out of the box) and accepts
your replies back, so you can watch and steer a change from your phone or any
remote device — not only the terminal you launched it from.

## Pause and resume mid-call

A run can be interrupted in the middle of a model call — via ESC, a signal, or a
watchdog — and resumed later from the event log without discarding the in-flight
step. Pause and resume work because the event log is the source of truth, not
in-memory state.

## Per-role, multi-vendor model selection

Model choice is per-role. A multi-vendor config can run different roles on
different vendors in one run — for example the coder on one vendor and the reviewer
on another. This is opt-in: the shipped workflow gets reviewer independence by model
tier within a single provider, so splitting roles across vendors is a deliberate
configuration choice.

## Drafts and zones

A fresh quest starts as a draft — a conversation and notes on disk, with no git
branch or worktree — so you can scope something out with zero commitment. Work is
organized into zones: an active list of things in flight, a backlog of parked
ideas, and a searchable archive of finished or abandoned quests.

## Project memory

Standing, human-authored instructions under `imp/memory/` are layered onto every
agent call, so project memory and cross-quest preferences persist without
re-stating them each time.

## Review gates

Work runs autonomously only up to a gate. Each change is isolated in its own
worktree until you approve it; human review gates sit at the spec, plan, and
merge boundaries by design.

## Event-log auditability and resumability

Every byte of every run is appended to a per-quest event log. The event log is the
single source of truth — not a side effect you hope was captured — making every run
auditable after the fact and resumable if it is interrupted.
