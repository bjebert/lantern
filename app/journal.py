"""Per-book learning journal: append-only markdown in the book store.

books/<id>/journal.md collects saved highlights and right-click Q&A, each
entry stamped with a [§node-id] token so entries can be counted per section
(chapter check-ins) and grepped by the chat agent (it lives inside the
retrieval cwd).
"""

import os
import re
import time

FILENAME = "journal.md"
HEADER = ("# Learning journal\n\n"
          "Saved highlights and questions from reading sessions, newest "
          "last. Entries are tagged [§section-id] for lookup.\n")


def path(book_dir):
    return os.path.join(book_dir, FILENAME)


def append(book_dir, kind, passage, answer=None, node=None, page=None,
           printed=None):
    """Append one entry. kind: 'highlight' | 'eli5' | 'explain' | ...
    Returns the entry's header line (for notices)."""
    where = ""
    if node:
        where += f" [§{node['id']}] {node['title']}"
    if page:
        where += f" — PDF p.{page}"
        if printed:
            where += f" (book p. {printed})"
    head = f"## {time.strftime('%Y-%m-%d %H:%M')} — {kind}{where}"
    lines = [head, ""]
    if passage:
        lines += ["> " + ln for ln in passage.strip().splitlines()] + [""]
    if answer:
        lines += [f"**Claude:** {answer.strip()}", ""]
    p = path(book_dir)
    fresh = not os.path.isfile(p)
    with open(p, "a", encoding="utf-8") as f:
        if fresh:
            f.write(HEADER)
        f.write("\n" + "\n".join(lines))
    return head


HEAD_RE = re.compile(
    r"^## (?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) — (?P<kind>[\w-]+)"
    r"(?: \[§(?P<sec>[^\]]+)\] (?P<title>.*?))?"
    r"(?: — PDF p\.(?P<page>\d+)(?: \(book p\. (?P<printed>[^)]*)\))?)?\s*$")


def entries(book_dir):
    """Parse journal.md into dicts {when, kind, sec, title, page, printed,
    text} (text = the entry body, passage quote + answer). Best effort."""
    p = path(book_dir)
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out, cur, body = [], None, []

    def _close():
        if cur is not None:
            cur["text"] = "\n".join(body).strip()
            out.append(cur)

    for ln in lines:
        m = HEAD_RE.match(ln)
        if m:
            _close()
            cur = m.groupdict()
            cur["page"] = int(cur["page"]) if cur["page"] else None
            body = []
        elif cur is not None:
            body.append(ln)
    _close()
    return out


def entries_for_page(book_dir, page):
    """Journal entries located on this PDF page (for the sidebar's
    per-page notes line — replaces the reader's hover popup)."""
    if not page:
        return []
    return [e for e in entries(book_dir) if e.get("page") == page]


def count_for_section(book_dir, node_id):
    """How many journal entries are tagged with this section id."""
    p = path(book_dir)
    if not node_id or not os.path.isfile(p):
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().count(f"[§{node_id}]")
    except OSError:
        return 0
