{# Scratch-dir note. Injected only when the agent holds a filesystem tool and the sandbox policy is active, immediately after the paths block (and the read-only note, when present). #}

## Scratch files go in your scratch dir

`/tmp` is read-only inside the sandbox — a literal write to `/tmp/<anything>`
is refused. Put scratch clones, working copies, screenshots, and throwaway
scripts under `{tmpdir}`, which is also your `$TMPDIR`; no agent dispatched
alongside you can see or write it, and it is wiped when the step ends, so
nothing you leave there accumulates. Reading `/tmp` is unaffected. The file
tools don't expand `$TMPDIR` — give them the literal path.
