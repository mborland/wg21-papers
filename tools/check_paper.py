#!/usr/bin/env python3
"""Lint mpark/wg21 paper sources for common mistakes that still build.

Pandoc accepts most of these silently, or with a warning buried in the build
log, and produces a paper that looks wrong. Run by pre-commit on every
top-level paper source; can also be run by hand:

    tools/check_paper.py D4497R0.md [more.md ...]

Checks:
  * front matter: present, valid YAML, required keys, `document` is a bare
    paper number (D1234R0, not D1234R0.md) matching the file name, ISO date
  * other YAML metadata blocks (e.g. `references:`) parse; reference ids unique
  * code fences (``` / ~~~) and fenced divs (:::) are closed
  * inline code spans are closed within their paragraph
  * citations: `[@ ID]` with a space, lower-case ids, P-papers with no
    revision, and (when wg21/data/csl.json has been built) ids that are in
    neither the WG21 index nor the paper's own `references:`
  * footnotes: every `[^x]` is defined and every definition is used
  * `[text] (url)`: a space that breaks the link
  * `#Heading` with no space after the hashes
  * repeated words ("the the"), including across a line break
  * curly quotes inside code, which break copy-pasted examples

Exit status is 1 if any problem was found.
"""

import datetime
import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "wg21" / "data" / "csl.json"

REQUIRED_KEYS = ("title", "document", "date", "audience", "author")
DOC_ID_RE = re.compile(r"^(?:[DP]\d{4}R\d+|N\d{4})$")

# Ids that mpark/wg21 resolves from https://wg21.link/index.yaml.
WG21_ID_RE = re.compile(r"^(?:N\d+|[DP]\d+R\d+|(?:CWG|EWG|LWG|LEWG|FS)\d+|EDIT\d+|SD\d+)$")
CITE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_][\w:.#$%&+?<>~/-]*)")
CITE_SPACE_RE = re.compile(r"\[@\s")
FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
FOOTNOTE_DEF_RE = re.compile(r"^ {0,3}\[\^([^\]\s]+)\]:")
SPACED_LINK_RE = re.compile(r"\]\s+\((?:https?://|#|\./|generated/)")
# '#.' is mpark/wg21's numbered-paragraph marker, not a heading.
BAD_HEADING_RE = re.compile(r"^#{1,6}[^#\s.]")
REPEATED_RE = re.compile(r"\b([A-Za-z]+)\s+\1\b", re.IGNORECASE)
# Legitimate doubles: "long long", "that that", "had had".
REPEAT_OK = {"long", "that", "had", "is", "do"}
CURLY_RE = re.compile("[‘’“”]")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
DIV_OPEN_RE = re.compile(r"^ {0,3}:{3,}\s*\S")
DIV_CLOSE_RE = re.compile(r"^ {0,3}:{3,}\s*$")
META_START_RE = re.compile(r"^[A-Za-z_][\w-]*\s*:")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class Paper:
    def __init__(self, path):
        self.path = path
        self.lines = path.read_text(encoding="utf-8").splitlines()
        self.problems = []
        # Per line: "text", "code", "fence" or "meta".
        self.kind = ["text"] * len(self.lines)
        self.front = None
        self.local_refs = set()

    def err(self, lineno, msg):
        self.problems.append((lineno, msg))

    # -- structure ----------------------------------------------------------

    def scan_metadata(self):
        """Find YAML blocks outside code; mark them so prose checks skip them."""
        i, n = 0, len(self.lines)
        if not self.lines or self.lines[0].strip() != "---":
            self.err(1, "missing YAML front matter (file must start with '---')")
        while i < n:
            if self.kind[i] != "text":
                i += 1
                continue
            starts_block = self.lines[i].strip() == "---" and (
                i == 0
                or (
                    not self.lines[i - 1].strip()
                    and i + 1 < n
                    and META_START_RE.match(self.lines[i + 1])
                )
            )
            if self.kind[i] != "text" or not starts_block:
                i += 1
                continue
            end = next(
                (j for j in range(i + 1, n) if self.lines[j].strip() in ("---", "...")),
                None,
            )
            if end is None:
                self.err(i + 1, "YAML metadata block is never closed with '---'")
                return
            for j in range(i, end + 1):
                self.kind[j] = "meta"
            self.parse_block(i, end)
            i = end + 1

    def parse_block(self, start, end):
        text = "\n".join(self.lines[start + 1 : end])
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            line = start + 2 + (mark.line if mark else 0)
            problem = getattr(e, "problem", None) or str(e).splitlines()[0]
            self.err(line, f"invalid YAML: {problem}")
            return
        if not isinstance(data, dict):
            self.err(start + 1, "YAML metadata block is not a mapping")
            return
        if start == 0:
            self.front = data
            self.check_front_matter(data)
        refs = data.get("references") or []
        if not isinstance(refs, list):
            self.err(start + 1, "'references' must be a list")
            return
        for ref in refs:
            rid = ref.get("id") if isinstance(ref, dict) else None
            if not rid:
                self.err(start + 1, f"reference without an 'id': {ref!r}")
                continue
            rid = str(rid)
            if rid in self.local_refs:
                self.err(self.find_line(f"id: {rid}", start), f"duplicate reference id '{rid}'")
            self.local_refs.add(rid)

    def check_front_matter(self, fm):
        for key in REQUIRED_KEYS:
            if fm.get(key) in (None, "", []):
                self.err(1, f"front matter is missing '{key}'")
        doc = fm.get("document")
        if doc:
            doc = str(doc)
            line = self.find_line("document:", 0)
            if not DOC_ID_RE.match(doc):
                self.err(
                    line,
                    f"'document: {doc}' is not a paper number like D1234R0 or P1234R0"
                    " (it is printed verbatim as 'Document #')",
                )
            elif doc.lower() != self.path.stem.lower():
                self.err(line, f"'document: {doc}' does not match the file name {self.path.name}")
        date = fm.get("date")
        if date is not None and not isinstance(date, datetime.date):
            self.err(self.find_line("date:", 0), f"'date: {date}' is not an ISO date (YYYY-MM-DD)")
        authors = fm.get("author") or []
        for a in authors if isinstance(authors, list) else [authors]:
            if not (isinstance(a, dict) and a.get("name")):
                self.err(self.find_line("author:", 0), f"author entry without a 'name': {a!r}")

    def scan_code(self):
        """Mark fenced code lines and check every fence is closed."""
        fence = None  # (char, length, opening line)
        for i, line in enumerate(self.lines):
            m = FENCE_RE.match(line)
            if fence:
                self.kind[i] = "code"
                if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip():
                    self.kind[i] = "fence"
                    fence = None
                continue
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                self.kind[i] = "fence"
                fence = (m.group(1)[0], len(m.group(1)), i + 1)
        if fence:
            self.err(fence[2], f"code fence {fence[0] * fence[1]} is never closed")

    def scan_divs(self):
        """Check every fenced div ':::' outside code is closed."""
        divs = []
        for i, line in enumerate(self.lines):
            if self.kind[i] != "text":
                continue
            if DIV_OPEN_RE.match(line):
                divs.append(i + 1)
            elif DIV_CLOSE_RE.match(line):
                if divs:
                    divs.pop()
                else:
                    self.err(i + 1, "':::' closes a fenced div that was never opened")
        for line in divs:
            self.err(line, "fenced div ':::' is never closed")

    # -- prose ---------------------------------------------------------------

    def paragraphs(self):
        """Yield (first line number, text) for each run of prose lines."""
        start, buf = None, []
        for i, line in enumerate(self.lines + [""]):
            prose = i < len(self.lines) and self.kind[i] == "text" and line.strip()
            if prose:
                if start is None:
                    start = i + 1
                buf.append(line)
            elif buf:
                yield start, "\n".join(buf)
                start, buf = None, []

    def strip_inline_code(self, start, text):
        """Mask `code` spans with '_' (keeping offsets); report unclosed ones.

        '_' rather than ' ' so "and `x` and" is not seen as a repeated word.
        """
        out = list(text)
        spans = []
        i = 0
        while i < len(text):
            if text[i] != "`":
                i += 1
                continue
            j = i
            while j < len(text) and text[j] == "`":
                j += 1
            ticks = text[i:j]
            close = re.compile(r"(?<!`)" + ticks + r"(?!`)").search(text, j)
            if not close:
                self.err(start + text.count("\n", 0, i), f"unclosed inline code {ticks}")
                break
            spans.append(text[j : close.start()])
            for k in range(i, close.end()):
                if out[k] != "\n":
                    out[k] = "_"
            i = close.end()
        return "".join(out), spans

    def check_prose(self, index):
        fn_refs = {}
        for start, raw in self.paragraphs():
            text, spans = self.strip_inline_code(start, raw)
            text = COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)

            def line_of(pos):
                return start + text.count("\n", 0, pos)

            for span in spans:
                if CURLY_RE.search(span):
                    self.err(start, f"curly quote inside inline code `{span}`")
            for m in CITE_SPACE_RE.finditer(text):
                self.err(line_of(m.start()), "citation has a space: write [@ID], not [@ ID]")
            # Pandoc reads any '@id' not preceded by a word character as a
            # citation, bracketed or not, so a stray '@name' is one too.
            for m in CITE_RE.finditer(text):
                self.check_citation(m.group(1).rstrip(".:"), line_of(m.start()), index)
            for m in FOOTNOTE_REF_RE.finditer(text):
                fn_refs.setdefault(m.group(1), line_of(m.start()))
            for m in SPACED_LINK_RE.finditer(text):
                self.err(line_of(m.start()), "space between ']' and '(' breaks the link")
            for m in REPEATED_RE.finditer(text):
                if m.group(1).lower() not in REPEAT_OK:
                    self.err(line_of(m.start()), f"repeated word '{m.group(1)} {m.group(1)}'")

        fn_defs = {}
        for i, line in enumerate(self.lines):
            if self.kind[i] != "text":
                if self.kind[i] == "code" and CURLY_RE.search(line):
                    self.err(i + 1, "curly quote inside a code block")
                continue
            if BAD_HEADING_RE.match(line):
                self.err(i + 1, f"heading needs a space after '#': {line.strip()!r}")
            m = FOOTNOTE_DEF_RE.match(line)
            if m:
                fn_defs.setdefault(m.group(1), i + 1)
        for name, line in fn_refs.items():
            if name not in fn_defs:
                self.err(line, f"footnote [^{name}] is never defined")
        for name, line in fn_defs.items():
            if name not in fn_refs:
                self.err(line, f"footnote [^{name}] is defined but never used")

    def check_citation(self, cid, line, index):
        if cid in self.local_refs:
            return
        upper = cid.upper()
        if cid != upper and WG21_ID_RE.match(upper):
            self.err(line, f"citation [@{cid}] must be upper case: [@{upper}]")
            return
        if re.fullmatch(r"P\d+", cid):
            self.err(line, f"citation [@{cid}] needs a revision, e.g. [@{cid}R0]")
            return
        if index is not None:
            if cid not in index:
                self.err(line, f"citation [@{cid}] is not in the WG21 index or this paper's references")
        elif not WG21_ID_RE.match(cid):
            self.err(line, f"citation [@{cid}] is not a WG21 id and not in this paper's references")

    def find_line(self, needle, start):
        for i in range(start, len(self.lines)):
            if needle in self.lines[i]:
                return i + 1
        return start + 1

    def run(self, index):
        self.scan_code()
        self.scan_metadata()
        self.scan_divs()
        self.check_prose(index)
        return sorted(set(self.problems))


def load_index():
    if not INDEX.is_file():
        print(
            f"check_paper.py: note: {INDEX.relative_to(ROOT)} not built; "
            "citation ids are checked by pattern only (run `make` once to enable)",
            file=sys.stderr,
        )
        return None
    with INDEX.open(encoding="utf-8") as f:
        return {entry["id"] for entry in json.load(f) if "id" in entry}


def main(argv):
    paths = [pathlib.Path(a) for a in argv[1:]]
    if not paths:
        print(__doc__)
        return 2
    index = load_index()
    failed = False
    for path in paths:
        for line, msg in Paper(path).run(index):
            print(f"{path}:{line}: {msg}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
