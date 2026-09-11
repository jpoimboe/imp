{# Scratch-dir note. Injected only when the agent holds a filesystem tool and the sandbox policy is active, immediately after the paths block (and the read-only note, when present). #}

## Scratch files go in your scratch dir

`/tmp` is read-only inside the sandbox — a literal write to `/tmp/<anything>`
is refused. Put scratch clones, working copies, screenshots, and throwaway
scripts under `{tmpdir}`, which is also your `$TMPDIR`; it is wiped when your
step ends, so nothing you leave there accumulates. Reading `/tmp` is
unaffected. The file tools don't expand `$TMPDIR` — give them the literal path.
