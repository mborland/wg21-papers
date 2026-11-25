#!/usr/bin/env python3
"""Generate the GitHub Pages landing page from README.md.

README.md is the single source of truth for which papers are listed and in
which section. For papers built from a source file in this repository, the
paper's YAML front matter supplies extra details (date, audience, authors).

Usage: make_index.py <repo-root> <output.html>
"""

import datetime
import html
import pathlib
import re
import sys

import yaml

REPO_URL = "https://github.com/mborland/wg21-papers"

SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")
REFDEF_RE = re.compile(r"^\[([^\]]+)\]:\s*(\S+)\s*$")
ITEM_RE = re.compile(
    r"^[-*]\s+\[(?P<id>[^\]]+)\](?:\[\]|\((?P<url>[^)\s]+)\))\s*:?\s*(?P<title>.*?)\s*$"
)
NONE_RE = re.compile(r"^[-*]\s+(none|n/a|-)\s*$", re.IGNORECASE)
PAPER_ID_RE = re.compile(r"^([DPN])(\d+)(?:R(\d+))?$", re.IGNORECASE)


def fail(msg):
    sys.exit(f"make_index.py: error: {msg}")


def parse_readme(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    refs = {}
    for line in lines:
        m = REFDEF_RE.match(line)
        if m:
            refs[m.group(1).lower()] = m.group(2)

    title = "C++ Committee Papers"
    sections = []
    current = None
    for lineno, line in enumerate(lines, 1):
        if m := TITLE_RE.match(line):
            title = m.group(1)
            continue
        if m := SECTION_RE.match(line):
            current = {"name": m.group(1), "papers": []}
            sections.append(current)
            continue
        if current is None or not re.match(r"^[-*]\s", line):
            continue
        if NONE_RE.match(line):
            continue
        m = ITEM_RE.match(line)
        if not m:
            fail(f"{path.name}:{lineno}: cannot parse list entry: {line!r}")
        pid = m.group("id")
        url = m.group("url") or refs.get(pid.lower())
        if not url:
            fail(f"{path.name}:{lineno}: no link target for [{pid}]")
        current["papers"].append(
            {"id": pid, "url": url, "title": m.group("title"), "line": lineno}
        )

    return title, sections


def front_matter(path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = re.search(r"^(?:---|\.\.\.)\s*$", text[3:], re.MULTILINE)
    if not end:
        return {}
    data = yaml.safe_load(text[3 : 3 + end.start()]) or {}
    return data if isinstance(data, dict) else {}


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def author_names(value):
    names = []
    for a in as_list(value):
        if isinstance(a, dict):
            if a.get("name"):
                names.append(str(a["name"]))
        elif a:
            names.append(str(a))
    return names


def format_date(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime("%-d %B %Y"), value.isoformat()
    if value:
        return str(value), str(value)
    return None, None


def inline(text):
    """Escape text, then render `code` spans."""
    escaped = html.escape(text, quote=False)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)


def enrich(paper, root):
    url = paper["url"]
    local = not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.IGNORECASE)
    paper["local"] = local
    if local:
        if url.startswith("generated/"):
            url = url[len("generated/") :]
        paper["href"] = url
        stem = pathlib.PurePosixPath(url).stem
        src = root / f"{stem}.md"
        if src.is_file():
            fm = front_matter(src)
            paper["source"] = f"{REPO_URL}/blob/main/{stem}.md"
            paper["date"] = format_date(fm.get("date"))
            paper["audience"] = [str(a) for a in as_list(fm.get("audience"))]
            paper["authors"] = author_names(fm.get("author"))
            if not paper["title"] and fm.get("title"):
                paper["title"] = str(fm["title"])
    else:
        paper["href"] = url

    m = PAPER_ID_RE.match(paper["id"])
    kind = m.group(1).upper() if m else ""
    paper["kind"] = {"D": "Draft", "P": "Published", "N": "N-paper"}.get(kind, "")
    paper["kind_class"] = {"D": "draft", "P": "published"}.get(kind, "other")
    if m and kind == "P":
        paper["wg21"] = f"https://wg21.link/{kind.lower()}{m.group(2)}"
    return paper


def render_paper(p):
    meta = []
    if p.get("audience"):
        meta.append(
            '<span class="meta-item" title="Audience">'
            + ", ".join(html.escape(a) for a in p["audience"])
            + "</span>"
        )
    date_text, date_iso = p.get("date") or (None, None)
    if date_text:
        meta.append(
            f'<span class="meta-item"><time datetime="{html.escape(date_iso)}">'
            f"{html.escape(date_text)}</time></span>"
        )
    if p.get("authors"):
        meta.append(
            '<span class="meta-item">'
            + ", ".join(html.escape(a) for a in p["authors"])
            + "</span>"
        )

    label = "Read draft" if p["kind_class"] == "draft" else "Read paper"
    links = [f'<a href="{html.escape(p["href"])}">{label}</a>']
    if p.get("wg21") and p["wg21"] != p["href"]:
        links.append(f'<a href="{html.escape(p["wg21"])}">wg21.link</a>')
    if p.get("source"):
        links.append(f'<a href="{html.escape(p["source"])}">Source</a>')

    title = inline(p["title"]) if p["title"] else html.escape(p["id"])
    meta_html = (
        f'<p class="meta">{"".join(meta)}</p>' if meta else ""
    )
    return f"""      <li class="paper">
        <a class="paper-id {p['kind_class']}" href="{html.escape(p['href'])}" title="{html.escape(p['kind'])}">{html.escape(p['id'])}</a>
        <div class="paper-body">
          <h3 class="paper-title"><a href="{html.escape(p['href'])}">{title}</a></h3>
          {meta_html}
        </div>
        <nav class="paper-links" aria-label="{html.escape(p['id'])} links">{" ".join(links)}</nav>
      </li>"""


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def render(title, sections):
    total = sum(len(s["papers"]) for s in sections)
    nav = " ".join(
        f'<a href="#{slug(s["name"])}">{html.escape(s["name"])}'
        f' <span class="count">{len(s["papers"])}</span></a>'
        for s in sections
    )
    body = []
    for s in sections:
        if s["papers"]:
            items = "\n".join(render_paper(p) for p in s["papers"])
            content = f'    <ol class="papers">\n{items}\n    </ol>'
        else:
            content = '    <p class="empty">No papers yet.</p>'
        body.append(
            f'  <section id="{slug(s["name"])}">\n'
            f'    <h2>{html.escape(s["name"])} <span class="count">{len(s["papers"])}</span></h2>\n'
            f"{content}\n  </section>"
        )
    updated = datetime.date.today()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{total} proposals for WG21, the ISO C++ standards committee.">
<style>
:root {{
  color-scheme: light dark;
  --bg: #fbfbf9;
  --surface: #ffffff;
  --text: #1d1f21;
  --muted: #5f6368;
  --border: #e3e3df;
  --link: #2b6cb0;
  --link-hover: #1a4e85;
  --draft-bg: #fff4e0;
  --draft-fg: #8a5300;
  --pub-bg: #e3f0ff;
  --pub-fg: #1d5aa6;
  --other-bg: #eeeeee;
  --other-fg: #444444;
  --code-bg: #f1f1ee;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #15171a;
    --surface: #1d2024;
    --text: #e6e6e3;
    --muted: #9aa0a6;
    --border: #2e3237;
    --link: #7fb2ec;
    --link-hover: #a9cbf3;
    --draft-bg: #3a2a10;
    --draft-fg: #f2c27a;
    --pub-bg: #13304f;
    --pub-fg: #9cc6f5;
    --other-bg: #2c2f33;
    --other-fg: #cccccc;
    --code-bg: #2a2d31;
  }}
}}
* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 17px/1.55 Charter, "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif;
}}
.wrap {{ max-width: 52rem; margin: 0 auto; padding: 0 1rem; }}
a {{ color: var(--link); text-decoration: none; }}
a:hover {{ color: var(--link-hover); text-decoration: underline; }}
a:focus-visible {{ outline: 2px solid var(--link); outline-offset: 2px; border-radius: 3px; }}
code {{
  font: 0.88em/1 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  background: var(--code-bg);
  padding: 0.12em 0.35em;
  border-radius: 4px;
}}
header.site {{ padding: 3.5rem 0 1.5rem; border-bottom: 1px solid var(--border); }}
.eyebrow {{
  margin: 0 0 0.4rem;
  font: 600 0.78rem/1.2 system-ui, -apple-system, "Segoe UI", sans-serif;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
}}
h1 {{ margin: 0; font-size: clamp(1.9rem, 5vw, 2.6rem); line-height: 1.15; font-weight: 600; }}
.lede {{ margin: 0.75rem 0 0; color: var(--muted); max-width: 38rem; }}
nav.sections {{
  display: flex; flex-wrap: wrap; gap: 0.5rem 1.25rem;
  margin-top: 1.25rem;
  font: 500 0.9rem/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
}}
.count {{
  display: inline-block; min-width: 1.5em; padding: 0 0.4em;
  font: 600 0.72rem/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  text-align: center; vertical-align: 0.15em;
  color: var(--muted); background: var(--code-bg); border-radius: 999px;
}}
main {{ padding: 1rem 0 3rem; }}
section {{ margin-top: 2rem; scroll-margin-top: 1rem; }}
h2 {{
  margin: 0 0 0.75rem;
  font: 600 1.05rem/1.3 system-ui, -apple-system, "Segoe UI", sans-serif;
  letter-spacing: 0.01em;
}}
ol.papers {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 0.6rem; }}
.paper {{
  display: grid;
  grid-template-columns: 6.5rem 1fr auto;
  align-items: start;
  gap: 0.25rem 1rem;
  padding: 0.9rem 1rem;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
}}
.paper-id {{
  justify-self: start;
  margin-top: 0.15rem;
  padding: 0.15rem 0.5rem;
  border-radius: 6px;
  font: 600 0.85rem/1.4 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}}
.paper-id:hover {{ text-decoration: none; filter: brightness(0.96); }}
.paper-id.draft {{ background: var(--draft-bg); color: var(--draft-fg); }}
.paper-id.published {{ background: var(--pub-bg); color: var(--pub-fg); }}
.paper-id.other {{ background: var(--other-bg); color: var(--other-fg); }}
.paper-body {{ min-width: 0; }}
.paper-title {{ margin: 0; font-size: 1.1rem; font-weight: 600; line-height: 1.35; }}
.paper-title a {{ color: var(--text); }}
.paper-title a:hover {{ color: var(--link); }}
.meta {{
  margin: 0.25rem 0 0;
  font: 0.85rem/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--muted);
}}
.meta-item + .meta-item::before {{ content: "·"; margin: 0 0.45em; }}
.paper-links {{
  display: flex; gap: 0.9rem; white-space: nowrap;
  margin-top: 0.2rem;
  font: 500 0.85rem/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
}}
.empty {{
  margin: 0; padding: 0.9rem 1rem;
  border: 1px dashed var(--border); border-radius: 10px;
  color: var(--muted); font-style: italic;
}}
footer {{
  padding: 1.5rem 0 3rem;
  border-top: 1px solid var(--border);
  font: 0.85rem/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--muted);
}}
footer .legend {{ display: inline-flex; gap: 0.5rem; align-items: center; margin-right: 1rem; }}
footer .paper-id {{ margin: 0; font-size: 0.75rem; }}
@media (max-width: 40rem) {{
  body {{ font-size: 16px; }}
  header.site {{ padding-top: 2.25rem; }}
  .paper {{ grid-template-columns: 1fr; }}
  .paper-links {{ margin-top: 0.4rem; }}
}}
</style>
</head>
<body>
<div class="wrap">
<header class="site">
  <p class="eyebrow">ISO/IEC JTC1/SC22/WG21</p>
  <h1>{html.escape(title)}</h1>
  <p class="lede">Proposals for the C++ standard: published papers and working drafts, with their sources.</p>
  <nav class="sections" aria-label="Sections">{nav}</nav>
</header>
<main>
{chr(10).join(body)}
</main>
<footer>
  <p>
    <span class="legend"><span class="paper-id draft">D</span> draft, not yet published</span>
    <span class="legend"><span class="paper-id published">P</span> published paper</span>
  </p>
  <p>Built from <a href="{REPO_URL}">{REPO_URL.removeprefix("https://")}</a> with <a href="https://github.com/mpark/wg21">mpark/wg21</a>. Updated <time datetime="{updated.isoformat()}">{updated.strftime("%-d %B %Y")}</time>.</p>
</footer>
</div>
</body>
</html>
"""


def main(argv):
    if len(argv) != 3:
        fail("usage: make_index.py <repo-root> <output.html>")
    root = pathlib.Path(argv[1])
    out = pathlib.Path(argv[2])
    title, sections = parse_readme(root / "README.md")
    for s in sections:
        for p in s["papers"]:
            enrich(p, root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(title, sections), encoding="utf-8")
    n = sum(len(s["papers"]) for s in sections)
    print(f"make_index.py: wrote {out} ({n} papers in {len(sections)} sections)")


if __name__ == "__main__":
    main(sys.argv)
