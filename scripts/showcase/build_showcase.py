"""Assemble a self-contained before/after showcase HTML page from a manifest.

This is the generic, change-agnostic builder used by
`tests/playbooks/before-after-playbook.md`. It knows nothing about any
particular imp change — it just renders whatever the manifest describes, with
every PNG embedded as a base64 data URI so the output is one portable file.

Usage:
    python3 scripts/showcase/build_showcase.py <manifest.json> [out.html]

Manifest schema (JSON):
    {
      "title":    "imp · new / resume UX overhaul",      # page H1
      "subtitle": "One-paragraph framing of the change.",  # optional
      "footer":   "How the screenshots were produced.",    # optional
      "slug":     "resume-ux",                              # used for the default out path
      "areas": [
        {
          "id":    "a1",                  # anchor + nav label is the part before the first '·'
          "title": "Area 1 · Resume discovery",
          "blurb": "What this group of changes is about.",  # optional
          "items": [
            { "title": "...", "caption": "...",
              "before": "/abs/before_x.png", "after": "/abs/after_x.png" },
            { "title": "Colour hierarchy", "caption": "...",
              "after": "/abs/after_colors.png",          # after-only is allowed
              "swatches": [ ["phase header", "#6E90B8", "#7898BE"] ] }  # optional table
          ]
        }
      ]
    }

Each item needs a `title`. `before`+`after` makes a two-up pair; `after` alone
makes a single panel; `swatches` appends a before/after colour table. Captions
and titles may contain inline HTML (e.g. <code>, <kbd>, <em>).
"""

from __future__ import annotations

import base64
import html as _html
import json
import sys
from pathlib import Path


def _b64(path: str) -> str:
    data = Path(path).read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _img(path: str) -> str:
    return f'<img src="{_b64(path)}" alt="">'


def _figure(path: str, label: str, after: bool) -> str:
    cls = ' class="after"' if after else ""
    return f'<figure{cls}><figcaption>{label}</figcaption>{_img(path)}</figure>'


def _swatch_table(rows: list) -> str:
    body = "".join(
        f'<tr><td>{_html.escape(str(name))}</td>'
        f'<td><span class="sw" style="background:{a}"></span><code>{a}</code></td>'
        f'<td><span class="sw" style="background:{b}"></span><code>{b}</code></td></tr>'
        for name, a, b in rows)
    return ('<table class="swatches"><thead><tr><th>element</th><th>before</th>'
            f'<th>after</th></tr></thead><tbody>{body}</tbody></table>')


def _item(it: dict) -> str:
    title = it["title"]
    caption = it.get("caption", "")
    figs = []
    if it.get("before"):
        figs.append(_figure(it["before"], "before", after=False))
    if it.get("after"):
        figs.append(_figure(it["after"], "after", after=True))
    single = " single" if len(figs) == 1 else ""
    pair = f'<div class="pair{single}">{"".join(figs)}</div>' if figs else ""
    swatches = _swatch_table(it["swatches"]) if it.get("swatches") else ""
    cap = f'<p class="cap">{caption}</p>' if caption else ""
    return f'<div class="item"><h3>{title}</h3>{cap}{pair}{swatches}</div>'


def _section(area: dict) -> str:
    anchor = area["id"]
    blurb = f'<p class="blurb">{area["blurb"]}</p>' if area.get("blurb") else ""
    items = "".join(_item(it) for it in area.get("items", []))
    return (f'<section id="{anchor}"><h2>{area["title"]}</h2>{blurb}{items}</section>')


_CSS = """
  :root { --bg:#15171a; --panel:#1c1f23; --ink:#e6e6e6; --dim:#9aa0a6;
    --line:#2b2f34; --accent:#a884b5; --before:#3a2c2c; --after:#26352a; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  header.top { padding:48px 24px 28px; max-width:1100px; margin:0 auto; }
  header.top h1 { margin:0 0 6px; font-size:30px; letter-spacing:-0.01em; }
  header.top .sub { color:var(--dim); font-size:17px; max-width:760px; }
  nav { position:sticky; top:0; background:rgba(21,23,26,.92); backdrop-filter:blur(6px);
    border-bottom:1px solid var(--line); padding:12px 24px; z-index:5; }
  nav .inner { max-width:1100px; margin:0 auto; color:var(--dim); font-size:14px; }
  nav a { color:var(--accent); text-decoration:none; }
  nav a:hover { text-decoration:underline; }
  main { max-width:1100px; margin:0 auto; padding:8px 24px 80px; }
  section { padding:34px 0 6px; border-top:1px solid var(--line); }
  section:first-of-type { border-top:none; }
  h2 { font-size:22px; margin:8px 0 4px; }
  h2 code { font-size:18px; }
  .blurb { color:var(--dim); max-width:760px; margin:0 0 18px; }
  .item { background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:18px 18px 20px; margin:18px 0; }
  .item h3 { margin:0 0 6px; font-size:17px; font-weight:600; }
  .cap { color:var(--dim); font-size:14.5px; margin:0 0 14px; max-width:880px; }
  .pair { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  .pair.single { grid-template-columns:1fr; }
  @media (max-width:760px) { .pair { grid-template-columns:1fr; } }
  figure { margin:0; background:#000; border:1px solid var(--line); border-radius:9px;
    overflow:hidden; }
  figure img { display:block; width:100%; height:auto; }
  figcaption { font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
    text-transform:uppercase; letter-spacing:.08em; color:var(--dim);
    padding:8px 10px; background:var(--before); border-bottom:1px solid var(--line); }
  figure.after figcaption { background:var(--after); color:#cfe6d4; }
  kbd { background:#2b2f34; border:1px solid #3a3f45; border-bottom-width:2px;
    border-radius:5px; padding:0 5px; font:600 12px ui-monospace,monospace; }
  code { background:#23262b; padding:1px 5px; border-radius:5px;
    font:13px ui-monospace,SFMono-Regular,Menlo,monospace; color:#d8cfe0; }
  table.swatches { border-collapse:collapse; margin:14px 0 2px; font-size:14px; }
  table.swatches th, table.swatches td { border:1px solid var(--line);
    padding:7px 12px; text-align:left; }
  table.swatches th { color:var(--dim); font-weight:600; }
  .sw { display:inline-block; width:13px; height:13px; border-radius:3px;
    vertical-align:-1px; margin-right:7px; border:1px solid #0006; }
  footer { max-width:1100px; margin:0 auto; padding:0 24px 60px; color:var(--dim);
    font-size:13px; }
"""


def build(manifest: dict) -> str:
    areas = manifest.get("areas", [])
    nav = " · ".join(
        f'<a href="#{a["id"]}">{a["title"].split("·")[0].strip()}</a>' for a in areas)
    sections = "".join(_section(a) for a in areas)
    sub = (f'<p class="sub">{manifest["subtitle"]}</p>'
           if manifest.get("subtitle") else "")
    footer = (f'<footer>{manifest["footer"]}</footer>'
              if manifest.get("footer") else "")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{manifest.get("title", "before / after")}</title>
<style>{_CSS}</style>
</head>
<body>
<header class="top"><h1>{manifest.get("title", "before / after")}</h1>{sub}</header>
<nav><div class="inner">{nav}</div></nav>
<main>{sections}</main>
{footer}
</body>
</html>"""


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: build_showcase.py <manifest.json> [out.html]")
    manifest = json.loads(Path(sys.argv[1]).read_text())
    slug = manifest.get("slug", "showcase")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(f"/tmp/{slug}-before-after.html")
    html = build(manifest)
    out.write_text(html)
    print(f"wrote {out} ({len(html.encode()) // 1024} KB, "
          f"{html.count('data:image/png')} images)")


if __name__ == "__main__":
    main()
