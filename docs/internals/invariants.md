# imp invariants

Read this when editing imp's engine/renderer or workflow prompts.

## Layering invariants

- Workflow agent prompts describe test/build **intent, not a runner** — imp
  targets any language. Say "the project's own test tooling", use
  `$IMP_TEST_COMMAND` / `$IMP_LINT_COMMAND`, and let the agent discover the real
  command. Illustrative code is allowed only with a disclaimer that it is
  illustrative. Exempt: imp's *own* tooling — `reconciler` /
  `integration-review` reading `quest.json`, `finalize`'s `PYTHONPATH`, and the
  impty grants on the review/gate holders (`batch-code-reviewer`, `threshold`,
  `finalize`, `spiker`) whose prompts name the impty tools and the
  `imp-driving-terminal-uis` / `imp-driving-web-uis` skills. Those prompts name
  the skills and nothing more: browser binaries and their flags live inside
  `imp-driving-web-uis` itself, never in a workflow prompt, so the invariant
  holds unweakened.
- The engine (`executor`) and the renderer (`narrative.py` / `render_text.py` /
  `live_driver.py`) expose only **generic primitives**: step kinds, and phases
  *derived* from each step's declared `phase` string. No hardcoded step, phase,
  or var names and no routing live in engine or renderer code — all specifics
  live in `workflow.yaml`, scripts, and prompts.
- **Claude session store.** The store body handed to native resume is a pure
  projection of imp's transcript — `message_to_session_records(_project_for_cli(
  messages[:resume_base]))`, overwritten wholesale, where `resume_base` (taken
  after every resume repair) starts the trailing run of user turns without a
  tool_result, the only part streamed. Records the CLI wrote are never merged in
  or counted; imp's transcript is the only source of truth for resumed context.
  At load the CLI splices a synthetic "No response requested." after a user-tail
  body; imp counters it with a stream-only `RESUME_STUB_NOTE` at the head of the
  streamed message, never persisted.
- **Codex native session.** One run-scoped process owner linearizes cancellation
  with subprocess publication and identity-safe cleanup; a live hard stop captures
  the process tree before waking blocked driver I/O, and the wake also releases
  drains held after parent exit. The coordinator's two-second hard deadline is
  acknowledged by receive or teardown independently of tool cleanup. Normal
  finalization still persists the exact native thread and rollout before removing
  the temporary Codex home.
- **Subagents must be restart-safe.** A subagent can be paused and resumed at
  any time, and resume **re-runs it from scratch** — the engine does not continue
  a paused subagent from its transcript, it restarts the affected subtree. This
  holds today because a subagent's durable work is its git commit and it grounds
  in current reality on entry (reads the repo, `progress.md`, the tag ledger). A
  subagent that acquires non-idempotent, in-transcript state — work that exists
  only in the conversation and would be lost or double-applied on a fresh run —
  breaks the invariant, and it fails as a silent double-effect on resume, not a
  loud error. No engine assertion guards this. Applies to every agent spawned
  under a parent, including the deeply-nested ones `MAX_SUBAGENT_DEPTH` allows.
  (If the invariant ever genuinely can't be met, that is the trigger to revisit
  the deferred re-descent / `State` frame-stack machinery, not a reason to ship a
  non-idempotent subagent.)
- **Never hand an agent a path its sandbox masks.** `<quest_dir>/steps/` (the
  overlay-home mountpoints and transcripts) and `<quest_dir>/events.jsonl` are
  masked for every agent by `resolve_policy`. Engine-authored text that points a
  sandboxed agent at recovery data must inline the content or route through
  `imp events`, which the daemon resolves outside the sandbox.
- **The sandbox policy handed to an invocation must agree with `quest.project_root`.**
  Going live mid-step flips `project_root` to the new worktree; `_run_agent_invocation`
  reconciles the policy's `worktree` and `git_worktree_self` on every entry, so a retry
  or reincarnation cannot re-enter the step with a policy still rooted at the control root.
- **The codex parse boundary is drift-visible, not permissive.** A codex item
  payload imp's own exec event models cannot parse is **fatal** for the item
  types `process_event` consumes (`mcp_tool_call`, `agent_message`, `reasoning`,
  `web_search`, `error`) — `CodexEventParseError`, a `ConfigError` → Pause — and a
  logged, skipped `runner_warning` for every other type. Unknown *item types*
  continue to degrade to `UnknownThreadItem`. The split lives in
  `codex_events.py`'s `parse_thread_item`, inside the parser: once an exception
  escapes the event stream's async generator, iteration cannot be resumed.

## Future / unbuilt

- **Memory retention (when implemented):** if imp ever adds memory/learnings
  storage, persist only (1) durable user preferences and (2) non-obvious
  invariants no test guards. Never persist "X landed/fixed/approved" status
  snapshots or branch-tied state — they rot fast.
- **User-level memory channel (P1):** `imp/memory/` is project-only today —
  read from the repo's `imp/` config dir and frozen per run into quest state,
  never merged through the floor → project → global → local config layers. A
  maintainer's cross-project preferences have no home but per-repo `imp/memory/`.
  Candidate feature: a user-level memory dir (e.g. under `~/.imp/`) whose
  `all.md` layers into every agent's `## Project memory` section alongside the
  project's.

## See also

- `internals/architecture.md` — the structures these invariants constrain.
- `internals/conventions.md` — terminal output, user-facing copy, code comments.
- `internals/knowledge-map.md` — which layer a new rule belongs in.
- The full corpus index is the `imp-knowledge` skill's *More depth* section.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
