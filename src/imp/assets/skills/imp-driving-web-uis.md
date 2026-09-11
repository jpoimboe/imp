# Driving web UIs

## Overview

Some work changes how a page *looks or behaves in a browser* — layout, styling,
click paths, JavaScript. You can't judge that from source alone: neither a diff nor
an HTML-string assertion shows you a rendered page. Render it and look.

Two jobs, two different tools:

- **See a page** — a static mockup or a real URL rendered to a PNG. Needs only a
  browser binary; use the headless one-shot below.
- **Drive a page** — click, type, wait for updates, read console errors. Needs the
  project's own browser automation; see "Driving".

## Seeing a page

Find a browser once, in this order — stop at the first hit:

1. `google-chrome`, `google-chrome-stable`, `chromium`, or `chromium-browser` on `$PATH`
2. `firefox` on `$PATH`
3. Playwright's cached build: `~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome`
4. the project's own browser tooling (see "Driving")

Chromium-family — `--no-sandbox` is required inside imp's sandbox:

```
<chrome> --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
  --screenshot=${TMPDIR:-/tmp}/shot.png --window-size=1280,900 <url-or-file://path>
```

Firefox:

```
firefox --headless --screenshot ${TMPDIR:-/tmp}/shot.png --window-size=1280,900 <url-or-file://path>
```

Then **`read_file` the PNG at its literal path** — `read_file` is not a shell and
won't expand `$TMPDIR`. That is how you actually see it. Ignore the dbus and GPU
errors on stderr; the file is written regardless. The shot captures the viewport, so
set `--window-size` to the size you're testing and make it taller for a long page.
Write shots to `$TMPDIR`, never into the worktree, which is read-only at review time.

Don't add a dependency to the project just to look at a page. If every probe misses,
say so plainly and hand the human the HTML file's path to open themselves — never
fall back to drawing the page in ASCII.

## Mockups

A design mockup is **real HTML** — one self-contained file with inline CSS, written
to `$TMPDIR`, rendered and read back so you have actually seen it, with its path given
to the human so they can open the real thing in their own browser. A mockup that
survives into a spec goes in as a fenced `html` block, so the implementer builds
from the markup instead of re-deriving it from a picture.

## Driving

Interaction needs a real automation library, and the right one is whatever the
project already depends on — discover it from the project's manifest and test config
rather than assuming. If the project has none and the work needs one, that's a
dependency the quest should introduce, not something to improvise around.

Write throwaway driver scripts to `$TMPDIR` and run them there. A driver script
should start the app (or attach to a running one) on an ephemeral port, perform the
interaction, screenshot to `$TMPDIR`, and print the console errors and failed requests
it collected — those are the failures a screenshot hides.

Don't hand-roll the DevTools Protocol against a background browser. It needs a
websocket client that isn't in the standard library, and the project's own tooling
is a better transport.

## Checking a real app

1. Start the project's server on an ephemeral port, in the background.
2. Poll the port until it answers — never a fixed sleep.
3. Render or drive the pages the quest touches.
4. `read_file` each PNG and judge it.
5. Stop the server.

Check the widths the design calls out, not just one. Color, spacing, overflow, and
whether a thing is visible at all are only knowable from the rendered image.
