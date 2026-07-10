# before / after playbook — visual showcase for an imp change

> **You are producing a before/after visual showcase.** Given any imp Change,
> render the user-visible CLI surfaces it touched — once from the code *before*
> the change, once *after* — and assemble them into one self-contained HTML page in
> `/tmp`. The screenshots come from imp's **real** render code (the same functions
> the live CLI calls) through the impty pyte→PNG pipeline, so the page shows exactly
> what a user sees, not a mock-up.
>
> The mechanical parts are scripted (`scripts/showcase/`). The **judgment** is
> yours: read the diff, decide which surfaces actually changed, and drive each one
> with representative data. Read this whole file before you start.

This is not an acceptance test — it produces no pass/fail. It is a communication
artifact: "here is what changed, side by side."

---

## What's already built (don't rewrite these)

- **`scripts/showcase/render_png.py`** — reads an ANSI text blob and renders it to a
  color PNG via `impty.render` (real pyte screen → pillow). Generic; theme-aware.
  `PYTHONPATH=src python3 scripts/showcase/render_png.py <ansi_file> <out.png> [theme] [cols]`
- **`scripts/showcase/build_showcase.py`** — reads a JSON **manifest** and emits the
  self-contained HTML (every PNG base64-embedded). Knows nothing about any change;
  see its docstring for the manifest schema.
  `python3 scripts/showcase/build_showcase.py <manifest.json> [out.html]`

You author two things per change: the **scene emitters** (small scripts that print
real ANSI) and the **manifest**. Everything else is turn-the-crank.

---

## 1. Pin the before and after trees

A run executes its own branch's code in its own worktree, so "before" and "after"
are two git trees you check out side by side.

```bash
REPO=$(git rev-parse --show-toplevel)
ID=2026-06-11-9k6zii                 # the Change id (see `imp list --all`)
BR=imp/$ID                           # the Change's branch
BASE=$(python3 -c "import json;print(json.load(open('$REPO/.imp/worktrees/$ID/.imp/change/change.json'))['base_branch'])" 2>/dev/null || echo master)

# AFTER tree: the Change's live worktree if it still exists (in-flight), else a
# fresh worktree at the branch tip (works for a merged Change too — the ref lives on).
AFTER=$REPO/.imp/worktrees/$ID
[ -d "$AFTER" ] || { AFTER=/tmp/showcase/after-tree; git worktree add -q "$AFTER" "$BR"; }

# BEFORE tree: where the branch diverged from base — the code as it was.
MB=$(git merge-base "$BASE" "$BR")
BEFORE=/tmp/showcase/before-tree
git worktree add -q "$BEFORE" "$MB"
echo "before=$BEFORE  after=$AFTER"
```

> **If the Change already merged into `$BASE`**, your main checkout *is* the after
> code now — don't render "before" from it. Always render before from the
> merge-base worktree, after from the branch worktree. Capture before the merge if
> you can; if you missed it, the merge-base worktree still has the old code.

Both trees import imp with `PYTHONPATH=src` and need `pyte`+`pillow` (the project
deps — system `python3` already has them here).

## 2. Read the diff; list the surfaces that changed

```bash
git -C "$AFTER" diff "$MB"...HEAD --stat        # what files moved
git -C "$AFTER" log "$MB"..HEAD --oneline       # the feat: commits name the surfaces
```

Walk the diff and write down every **user-visible** surface it touches — the things
a person reads on screen. Typical imp surfaces: `imp list` rows, `imp resume`
resolution (picker / nothing / ambiguous / gone), run headers, the rename line, the
`wait_for_user` prompt, pause / merge-blocked lines, the transcript (phase / agent /
prose / tool-summary / quiet steps), error messages, colours. Ignore pure-internal
refactors with no on-screen effect. Aim for one showcase item per distinct surface.

For each surface note: what function or command renders it, and what the **old**
behaviour was (so you can reproduce it from the before tree).

## 3. Author the scene emitters

Write two scripts that each print one scene's real ANSI to stdout. Keep a third
pure-data module so before and after show the **same** underlying data (fix `now` so
relative ages are reproducible). Run each from its own tree.

`/tmp/showcase/data.py` (no imp imports — shared by both):
```python
from datetime import datetime, timedelta, timezone
NOW = datetime(2026, 6, 11, 12, 10, 0, tzinfo=timezone.utc)
def iso(age_min): return (NOW - timedelta(minutes=age_min)).isoformat()
ROWS = [("2026-06-11-yaitd7", "Add a --version flag", "paused", "implement", 2), ...]
```

`/tmp/showcase/after.py` — uses the **new** APIs (lazy-import inside each scene so a
missing symbol can't break unrelated scenes):
```python
import sys; sys.path.insert(0, "/tmp/showcase"); import data
from imp.cli import change_row            # a new module → only resolves in the after tree
def scene_status():
    rows = [change_row.header_line(color=True)]
    for cid, title, st, step, age in data.ROWS:
        rows.append(change_row.format_change_line(quest_id=cid, title=title,
            status=st, step=step, last_iso=data.iso(age), now=data.NOW, color=True))
    return rows
SCENES = {"status": scene_status, ...}
sys.stdout.write("\n".join(SCENES[sys.argv[1]]()) + "\n")
```

`/tmp/showcase/before.py` — reproduces the **old** behaviour from the before tree. New
functions don't exist there, so either call the old function (e.g. the old
`_render_status_table`) or just run the real old command and capture its output. The
goal is a faithful old rendering, by whatever path the old code offered.

> **Drive the real render functions, not your idea of them.** Import imp's own
> formatter / prompt / pause helpers and feed them representative data. If a scene is
> only reachable through the live CLI (a prompt_toolkit widget), reproduce its text
> with the same helper the widget uses (`_prompt_message_text`, `pal.caret`, …).
> Cover the spread: include a `paused`, `errored`, `merge_blocked`, an empty title
> (`(unnamed)`), and a legacy descriptive id so every code path shows.

## 4. Render each scene to PNG

```bash
for s in status status_all picker ambiguous pause rename quiet ... ; do
  ( cd "$BEFORE" && PYTHONPATH=src python3 /tmp/showcase/before.py $s > /tmp/showcase/ansi/before_$s.txt
    PYTHONPATH=src python3 "$REPO/scripts/showcase/render_png.py" /tmp/showcase/ansi/before_$s.txt /tmp/showcase/png/before_$s.png dark )
  ( cd "$AFTER"  && PYTHONPATH=src python3 /tmp/showcase/after.py  $s > /tmp/showcase/ansi/after_$s.txt
    PYTHONPATH=src python3 "$REPO/scripts/showcase/render_png.py" /tmp/showcase/ansi/after_$s.txt  /tmp/showcase/png/after_$s.png dark )
done
```

`render_png.py` auto-sizes to the longest line; pass an explicit `cols` to match a
before/after pair's width, or to force a clean single line. Snapshot a couple of the
PNGs with the Read tool and check truncation, colour, and alignment before moving on.

> **Offline render vs live impty — pick deliberately.**
> - **Offline `render_png.py`** rewrites dim (SGR 2) text into a visible gray, so it
>   is the right tool for any dim-heavy surface — the `wait_for_user` question label,
>   `(unnamed)`, dim markers, the `past changes` section.
> - **Live impty MCP** (`mcp__impty__start` → `wait` → `snapshot`) is the authentic
>   path for an *interactive* widget you want to prove actually runs (the arrow-key
>   resume picker). But its `snapshot` **discards dim text** — anything dim vanishes.
>   Use it only where the meaningful content isn't dim. For everything else, prefer
>   the offline path: it's deterministic, costs nothing, and keeps dim.
>
> A live run (`IMP_RUN_*`, billable) is only worth it when a surface can't be
> reproduced from render functions at all. It rarely is — almost every surface here
> is a pure function of its data.

## 5. Assemble the manifest and build

Write `/tmp/showcase/<slug>.manifest.json` grouping the items (mirror the change's
own structure — e.g. its spec's areas — so the page reads in the author's order).
Each item is `{title, caption, before, after}`; `after`-only and an optional
`swatches` colour table are allowed (see the builder docstring). Titles and captions
take inline HTML (`<code>`, `<kbd>`, `<em>`).

```bash
python3 "$REPO/scripts/showcase/build_showcase.py" /tmp/showcase/<slug>.manifest.json
# → /tmp/<slug>-before-after.html   (self-contained; open in any browser)
```

## 6. Verify, then clean up

```bash
google-chrome --headless=new --no-sandbox --hide-scrollbars \
  --window-size=1200,7600 --screenshot=/tmp/showcase/verify.png \
  "file:///tmp/<slug>-before-after.html"
```
Read `verify.png` (crop tall pages with PIL). Confirm every pair rendered, captions
read clearly, and the before/after bars (red = before, green = after) are right.

```bash
git worktree remove /tmp/showcase/before-tree --force
[ "$AFTER" = "/tmp/showcase/after-tree" ] && git worktree remove "$AFTER" --force
git worktree prune
```

---

## Gotchas (learned the hard way)

- **Don't render `full=True`** on a plain `pyte.Screen` — it wants a `HistoryScreen`.
  `render_png.py` uses `full=False` and pads one row so a trailing newline can't
  scroll the top line (the column header) off the screen.
- **A scene's screen must be ≥ its line count + 1 rows**, or the final `\n` scrolls
  line 0 away. `render_png.py` handles this; if you render by hand, remember it.
- **Lazy-import new symbols** inside each after-scene. A top-level
  `from imp.cli import change_row` makes the *whole* emitter fail to import in the
  before tree even for scenes that don't need it.
- **Reproduce, don't fabricate, old behaviour.** Read the old function in the before
  tree (or run the old command) — guessing the old wording undermines the contrast.
- **A merged Change rewrites your main checkout.** Once merged, `master`/`main` is the
  *after* code and the live worktree is gone. Render before from the merge-base
  worktree; the branch ref (`imp/<id>`) still gives you the after tree.
- **Subtle palette-only changes don't read as twin screenshots.** If the diff is just
  colour values, show one transcript plus a `swatches` table of the exact hex deltas
  instead of two near-identical images.
