{# `## Paths are relative to the project root`. Injected only when the agent holds a filesystem tool (read_file/write_file/edit_file/grep/glob/bash), after the tone block and before the date line. #}

## Paths are relative to the project root

`read_file`, `write_file`, and `edit_file` take paths
**relative to the project root** (e.g. `src/foo.py`), and also accept absolute
or out-of-worktree paths, bounded exactly like `bash`: unrestricted when the run
is unsandboxed, confined to the sandbox's readable/writable set when active
(secrets stay masked). `grep` and `glob` stay worktree-relative and reject
absolute paths and `..` escapes.

For `bash`, the working-directory contract is below.

`cwd` defaults to the project root. Relative values are resolved from that root;
absolute values are used as written. Use project-relative paths where practical
(e.g. `cat src/foo.py`); for an absolute project path, use `$IMP_PROJECT_ROOT`.
