# imp-tool-reference

On-demand reference detail for imp's built-in tools. The one-line tool
descriptions carry only the silent-corruption warnings; every other mechanical
detail lives here. Load with `load_skill(name="imp-tool-reference")`.

## Paths & the sandbox (all filesystem tools)

`read_file`, `write_file`, `edit_file`, `grep`, `glob`, and `symbol_outline`
take paths relative to the project root (e.g. `src/foo.py`). Absolute and
out-of-worktree paths are also accepted, bounded by the sandbox exactly like
`bash` — unrestricted when unsandboxed, confined to the sandbox's
readable/writable set when active (masked secrets stay masked). For
`grep`/`glob`, in-worktree matches report relative to the root, external
matches as absolute paths.

Shared caps: text output is capped at 50 KB per call, and only the first 5 MB
of a file is read or scanned.

`grep` and `glob` both skip paths git ignores (build artifacts, caches,
`.venv`, etc.) and the `.git/` dir; a quest worktree's `.imp/quests/<id>/artifacts/`
subtree stays visible, and explicitly targeting an ignored path searches it
anyway.

## read_file

- Images: PNG/JPEG/GIF/WebP detected by content regardless of extension. An
  image returns a viewable part plus a short caption; text returns 1-based
  `cat -n` line numbers.
- Paging: `offset` (default 1) is the first line shown; `limit` (default 2000)
  caps the lines returned; a trailing `[notice]` says how to continue when more
  lines remain.

## write_file

- Overwrites the whole file and creates parent directories as needed.

## edit_file

- Refuses files larger than 5 MB (same ceiling as read_file's) and files that
  are not valid UTF-8 — use `bash` for those. A non-UTF-8 file LOOKS editable,
  so retrying `edit_file` keeps failing.
- Batch: pass `edits=[{old, new, replace_all?}]` to apply several edits to one
  file in a single call. The file is read once, edits apply sequentially (each
  sees earlier results), and the batch is all-or-nothing — if any edit fails its
  match rule the file is left untouched. Pass EITHER `edits` OR top-level
  `old`/`new`, never both.

## grep

- `output_mode`: 'files_with_matches' (default) lists matching paths; 'content'
  shows `path:lineno:line` rows (context rows from `before`/`after`/`context`
  use `path-lineno-line`); 'count' shows `path:count`. `before`/`after`/`context`
  require content mode; `head_limit` caps result rows.
- `multiline=True` lets the regex span line boundaries (matched with
  DOTALL+MULTILINE over the whole file); each match is reported at its START
  line. `case_insensitive` applies to the REGEX only — the `glob` filter is
  always case-sensitive.
- Lines split on `\n` only: on CRLF files match the invisible trailing `\r`
  with `\r?$`.
- `glob` filters by the worktree-relative path (in-worktree) or absolute path
  (external), NOT relative to the `path` argument — use `glob="src/a/*.py"` or a
  wildcard basename like `*.py`, with fnmatch semantics where both `*` and `**`
  cross `/`.
- Also skips binary files (git-ignored paths and `.git/` per the shared rule
  above). An external-directory search stops after a fixed file-count cap,
  disclosed in the output.

## glob

- Uses pathlib glob semantics — a bare `*.py` matches top-level files only; this
  differs from `grep`'s `glob` parameter, where `*` and `**` cross `/`.
- The `pattern` stays relative; search elsewhere via `path`, not an absolute
  pattern.
- Results are ordered most-recent-first by mtime (ties broken by path),
  head-truncated with a `… +N not shown` notice so the most-recent matches are
  kept.

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

- Foreground: each line over 2000 chars is clipped to 2000 with a
  `…[truncated]` marker, then the two streams are cut to a combined 30 KB
  head. When either fired, the full unmodified output spills to a file and a
  `notice` says which happened and carries a `read_file`-able path; no
  `notice` means the output is byte-exact — bar two standing exceptions: an
  `errors="replace"` decode, and the sandbox entry marker stripped from
  stderr. Runaway output is killed at 5 MB.
  Foreground commands are interruptible (SIGTERM to the process group, 4 s
  grace, then SIGKILL). A foreground command that exceeds its deadline (120 s by
  default — pass `timeout`, positive seconds, capped at 600, to extend) is
  promoted to a background task instead of being killed: the call returns the
  same `{id, pid, command, status, out_path, err_path}` descriptor plus a
  `note`, and the task is thereafter read via `bash_output(id)` and stopped via
  `bash_kill(id)`. The deadline starts when the command actually starts — not
  when you issued it. A bash call sharing an assistant message with `sub_agent`
  calls can wait for the entire subagent batch first, then burn its full budget
  and be promoted; issue bash in a separate message.
- A denied write (a sandbox-masked path) surfaces the raw OS error plus a
  `sandbox_note` naming the boundary — foreground only; retrying never helps.
- Background (`run_in_background=true`): returns `{id, pid, command, status,
  out_path, err_path}` immediately while the process keeps running.
  `out_path`/`err_path` are relative to the project root (bash's default cwd)
  and readable via `bash` or `read_file`; `grep`/`glob` exclude them (they live
  under `.imp/`). Background output is file-redirected, so most programs
  block-buffer (~4 KB): early lines (e.g. a server's 'listening' banner) may lag
  until a flush.

## bash_output

- Returns `{stdout, stderr, status, returncode, kill_reason?, wait_ended?}`;
  status is `running|exited|killed`. Each call returns only output new since the
  previous call (per-stream cursors), capped at 50 KB per stream per call; when
  more remains, the result carries a notice with per-stream remaining byte counts
  and the stream-file paths.
- Page fully only when the content matters — for bulk logs use `bash`
  (grep/tail) or `read_file` on the stream files instead (recent output may lag;
  see the block-buffer note above). An exited or killed id stays readable until
  the invocation ends.
- `wait=true` blocks until the process reaches a terminal status, your `timeout`
  elapses, or the 20-minute wait budget is exhausted — `wait_ended` says which
  (`'exit'|'timeout'|'budget'`). timeout/budget returns are normal results, and a
  timeout above the budget is clipped to it. `timeout` requires `wait=true` and
  must be > 0.

## bash_kill

- Returns a final-status JSON snapshot `{stdout, stderr, status, returncode,
  kill_reason?}` — the snapshot is a real read and advances the output cursors,
  so a follow-up `bash_output` continues from there without duplicate bytes;
  remaining output stays pageable. Idempotent on an already-exited or
  already-killed id.
