# imp-tool-reference

On-demand reference detail for imp's built-in tools. The one-line tool
descriptions carry only the silent-corruption warnings; every other mechanical
detail — caps, sandbox path rules, buffering, image handling, `max_depth`, and
the like — lives here. Load it with `load_skill(name="imp-tool-reference")` when you
need the specifics.

## Paths & the sandbox (all filesystem tools)

`read_file`, `write_file`, `edit_file`, `grep`, `glob`, and `symbol_outline`
take paths relative to the project root (e.g. `src/foo.py`). Absolute and
out-of-worktree paths are also accepted, bounded by the sandbox exactly like
`bash` — unrestricted when the run is unsandboxed, confined to the sandbox's
readable/writable set when it is active (masked secrets stay masked). For
`grep`/`glob`, in-worktree matches are reported relative to the root and
external matches as absolute paths.

## read_file

- Images: PNG/JPEG/GIF/WebP are detected by content regardless of extension. An
  image returns a viewable image part plus a short caption; a text file returns
  1-based `cat -n` line numbers.
- Paging: `offset` (default 1) is the first line shown; `limit` (default 2000)
  caps the lines returned; a trailing `[notice]` says how to continue when more
  lines remain.
- Caps: text output is bounded at 50 KB, and only the first 5 MB of a file is
  readable.

## write_file

- Overwrites the whole file and creates parent directories as needed.

## edit_file

- Refuses files larger than 5 MB and files that are not valid UTF-8 — use
  `bash` for those. A non-UTF-8 file LOOKS editable, so retrying `edit_file` on
  it will keep failing. (The 5 MB ceiling is the same as read_file's: only the
  first 5 MB of a file is readable/editable.)
- Batch: pass `edits=[{old, new, replace_all?}]` to apply several edits to one
  file in a single call. The file is read once, the edits apply sequentially
  (each sees the result of the earlier ones), and the batch is all-or-nothing —
  if any edit fails its match rule the file is left untouched. Pass EITHER
  `edits` (batch) OR top-level `old`/`new`, never both.

## grep

- `output_mode`: 'files_with_matches' (default) lists matching paths; 'content'
  shows `path:lineno:line` rows (context rows from `before`/`after`/`context`
  use `path-lineno-line`); 'count' shows `path:count`. `before`/`after`/`context`
  require content mode; `head_limit` caps result rows.
- `multiline=True` lets the regex span line boundaries (matched with
  DOTALL+MULTILINE over the whole file); each match is reported at its START
  line. `case_insensitive` applies to the REGEX only — the `glob` filter is
  always case-sensitive.
- Lines are split on `\n` only: on CRLF files match the invisible trailing `\r`
  with `\r?$`.
- `glob` filters by the worktree-relative path for in-worktree targets or the
  absolute path for external targets (NOT relative to the `path` argument — use
  `glob="src/a/*.py"` or a wildcard basename like `*.py`), with fnmatch
  semantics where both `*` and `**` cross `/`.
- Skips paths git ignores (build artifacts, caches, `.venv`, etc.), the `.git/`
  dir, and binary files; the quest's `.imp/quests/<id>/artifacts/` subtree stays
  visible, and explicitly targeting an ignored path searches it anyway.
- Scans the first 5 MB per file; output capped at 50 KB. An external-directory
  search stops after a fixed file-count cap, disclosed in the output.

## glob

- Uses pathlib glob semantics — a bare `*.py` matches top-level files only; this
  differs from `grep`'s `glob` parameter, where `*` and `**` cross `/`.
- The `pattern` stays relative; search elsewhere via `path`, not an absolute
  pattern.
- Skips paths git ignores (build artifacts, caches, `.venv`, etc.) and the
  `.git/` dir; the quest's `.imp/quests/<id>/artifacts/` subtree stays visible,
  and explicitly targeting an ignored path searches it anyway.
- Results are ordered most-recent-first by mtime (ties broken by path) and
  capped at 50 KB, head-truncated with a `… +N not shown` notice so the
  most-recent matches are the ones kept.

## symbol_outline

- A file path gives a per-file outline; a directory path recurses
  gitignore-aware with per-file and recursive per-directory counts.
- `detail` is 'signatures' (default: counts plus signatures) or 'counts' (names
  and counts only). `max_depth` caps directory recursion depth (directory paths
  only; ignored for a file).
- Read-only and never raises: an unsupported or unparseable file degrades to a
  one-line 'read directly' note, and output is capped at 50 KB with
  breadth-first thinning.

## bash

- Foreground: the 30 KB head of each stream is shown; when combined output
  exceeds 30 KB the full output spills to a file and a `notice` field carries a
  `read_file`-able path to recover the tail. Runaway output is killed at 5 MB. A
  foreground command is interruptible (SIGTERM to the process group, 4 s grace,
  then SIGKILL) and is reaped after 120 s by default — pass `timeout` (positive
  seconds, capped at 600) to extend a slow foreground command.
- Background (`run_in_background=true`): returns `{id, pid, command, status,
  out_path, err_path}` immediately while the process keeps running.
  `out_path`/`err_path` are relative to the project root (bash's default cwd) and
  readable via `bash` or `read_file`; `grep`/`glob` exclude them (they live under
  `.imp/`). Background output is file-redirected, so most programs block-buffer
  (~4 KB): early lines (e.g. a server's 'listening' banner) may lag until a
  flush.

## bash_output

- Returns `{stdout, stderr, status, returncode, kill_reason?, wait_ended?}`;
  status is `running|exited|killed`. Each call returns only output new since the
  previous call (per-stream cursors), capped at 50 KB per stream per call; when
  more remains, the result carries a notice with per-stream remaining byte counts
  and the stream-file paths.
- Page fully only when the content matters — for bulk logs (a 5 MB stream is
  ~100 pages of context), use `bash` (grep/tail) or `read_file` on the stream
  files instead. An exited or killed id stays readable until the invocation ends.
- `wait=true` blocks until the process reaches a terminal status, your `timeout`
  elapses, or the 20-minute wait budget is exhausted — `wait_ended` says which
  (`'exit'|'timeout'|'budget'`). timeout/budget returns are normal results, and a
  timeout above the budget is clipped to it. `timeout` requires `wait=true` and
  must be > 0.
- File-redirected output is block-buffered (~4 KB) by most programs, so very
  recent output may lag until the child flushes.

## bash_kill

- Returns a final-status JSON snapshot `{stdout, stderr, status, returncode,
  kill_reason?}` — the snapshot is a real read and advances the output cursors,
  so a follow-up `bash_output` continues from there without duplicate bytes;
  remaining output stays pageable. Idempotent on an already-exited or
  already-killed id.
