"""Book store access: toc/meta loading, page/snippet -> section mapping.

A store is what app/ingest.py writes under books/<book_id>/. This module is
import-light (stdlib only) so bus.py and the shell can use it at runtime.
"""

import bisect
import json
import os
import re
import time
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOKS_DIR = os.path.join(ROOT, "books")

ANCHOR = re.compile(r"^\[p\.(\d+)\]$", re.MULTILINE)
MIN_SNIPPET_CHARS = 20   # normalized; shorter snippets are too ambiguous


def norm(text):
    """Ligature/case/whitespace-insensitive comparison key."""
    text = unicodedata.normalize("NFKD", text)
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


class BookStore:
    def __init__(self, book_dir, meta, toc):
        self.book_dir = book_dir
        self.meta = meta
        self.toc = toc                       # reading order, as ingested
        self._starts = [n["pdf_start"] for n in toc]
        self._page_index = None              # lazy: [(file, page, norm_text)]

    @classmethod
    def open(cls, book_dir):
        book_dir = os.path.abspath(book_dir)
        with open(os.path.join(book_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        with open(os.path.join(book_dir, "toc.json"), encoding="utf-8") as f:
            toc = json.load(f)
        return cls(book_dir, meta, toc)

    @classmethod
    def find_for_pdf(cls, pdf_path, books_dir=BOOKS_DIR):
        """Locate an existing store for this file — the PDF the reader opens
        (meta.source_path) or the EPUB it was converted from
        (meta.original_path). Path match only; the store name embeds a
        content hash we don't recompute here. Returns a BookStore or None."""
        if not os.path.isdir(books_dir):
            return None
        pdf_path = os.path.normcase(os.path.abspath(pdf_path))
        for entry in sorted(os.listdir(books_dir)):
            meta_path = os.path.join(books_dir, entry, "meta.json")
            toc_path = os.path.join(books_dir, entry, "toc.json")
            if not (os.path.isfile(meta_path) and os.path.isfile(toc_path)):
                continue
            try:
                store = cls.open(os.path.join(books_dir, entry))
            except (OSError, json.JSONDecodeError):
                continue
            for key in ("source_path", "original_path"):
                if os.path.normcase(store.meta.get(key, "")) == pdf_path:
                    return store
        return None

    def save_meta(self):
        with open(os.path.join(self.book_dir, "meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.meta, f, indent=2)

    def touch_opened(self):
        """Stamp meta.last_opened = now (library recency) and persist."""
        self.meta["last_opened"] = int(time.time())
        self.save_meta()

    @property
    def title(self):
        return self.meta.get("title", os.path.basename(self.book_dir))

    @property
    def n_pages(self):
        return self.meta.get("n_pages")

    def printed_label(self, pdf_page):
        labels = self.meta.get("page_labels")
        if labels and 1 <= pdf_page <= len(labels) and labels[pdf_page - 1]:
            return labels[pdf_page - 1]
        return None

    def locate_page(self, pdf_page):
        """Deepest TOC node containing this PDF page (nodes partition the
        book linearly), or None outside the book."""
        if not self.toc or not 1 <= pdf_page <= (self.n_pages or 1 << 30):
            return None
        i = bisect.bisect_right(self._starts, pdf_page) - 1
        return self.toc[i] if i >= 0 else None

    def chapter_of(self, node):
        """The level-1 node this node belongs to (itself if level 1)."""
        if node is None:
            return None
        i = self.toc.index(node)
        for n in reversed(self.toc[:i + 1]):
            if n["level"] == 1:
                return n
        return None

    def node_by_id(self, node_id):
        return next((n for n in self.toc if n["id"] == node_id), None)

    def sections_of(self, chapter):
        """The chapter node plus its level-2 children, in reading order."""
        if chapter is None:
            return []
        i = self.toc.index(chapter)
        out = [chapter]
        for n in self.toc[i + 1:]:
            if n["level"] == 1:
                break
            out.append(n)
        return out

    def previous_chapter(self, chapter):
        if chapter is None:
            return None
        i = self.toc.index(chapter)
        for n in reversed(self.toc[:i]):
            if n["level"] == 1:
                return n
        return None

    # ---------- snippet -> position (clipboard-watcher path) ----------

    def _index(self):
        if self._page_index is None:
            idx = []
            for node in self.toc:
                try:
                    with open(os.path.join(self.book_dir, node["file"]),
                              encoding="utf-8") as f:
                        content = f.read()
                except OSError:
                    continue
                parts = ANCHOR.split(content)
                # parts = [header, page1, text1, page2, text2, ...]
                for k in range(1, len(parts) - 1, 2):
                    idx.append((node, int(parts[k]), norm(parts[k + 1])))
            self._page_index = idx
        return self._page_index

    def locate_snippet(self, snippet):
        """Find copied text in the book -> (node, pdf_page), or None.
        Exact match on normalized text; snippets may span a page boundary."""
        needle = norm(snippet)
        if len(needle) < MIN_SNIPPET_CHARS:
            return None
        idx = self._index()
        prev = None
        for node, page, text in idx:
            if needle in text:
                return node, page
            if prev is not None:
                # boundary case: snippet straddles two consecutive pages
                joined = prev[2][-len(needle):] + " " + text[:len(needle)]
                if needle in joined:
                    return prev[0], prev[1]
            prev = (node, page, text)
        return None


# ---------- library: every store, with progress ----------

def _journal_stats(book_dir):
    """(entry count, bytes) of journal.md — entries are '## ' headers."""
    p = os.path.join(book_dir, "journal.md")
    try:
        with open(p, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return 0, 0
    n = sum(1 for ln in text.splitlines() if ln.startswith("## "))
    return n, len(text.encode("utf-8"))


def _last_opened(store):
    """meta.last_opened (stamped by the shell); older stores fall back to
    the newest session's activity, then meta.json's mtime."""
    ts = store.meta.get("last_opened")
    if ts:
        return int(ts)
    newest = 0
    sdir = os.path.join(store.book_dir, "sessions")
    if os.path.isdir(sdir):
        for name in os.listdir(sdir):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(sdir, name), encoding="utf-8") as f:
                    newest = max(newest, int(json.load(f).get("updated", 0)))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
    if newest:
        return newest
    try:
        return int(os.path.getmtime(os.path.join(store.book_dir, "meta.json")))
    except OSError:
        return 0


def library(books_dir=BOOKS_DIR):
    """One summary dict per ingested book, most recently opened first:
    {book_dir, title, source_path, source_exists, n_pages, last_page,
     printed, section, last_opened, journal_entries, journal_bytes,
     n_sessions}. Broken stores are skipped."""
    out = []
    if not os.path.isdir(books_dir):
        return out
    for entry in sorted(os.listdir(books_dir)):
        d = os.path.join(books_dir, entry)
        if not (os.path.isfile(os.path.join(d, "meta.json"))
                and os.path.isfile(os.path.join(d, "toc.json"))):
            continue
        try:
            store = BookStore.open(d)
        except (OSError, json.JSONDecodeError):
            continue
        last_page = store.meta.get("last_page")
        node = store.locate_page(last_page) if last_page else None
        n_entries, n_bytes = _journal_stats(d)
        sdir = os.path.join(d, "sessions")
        n_sessions = (len([n for n in os.listdir(sdir) if n.endswith(".json")])
                      if os.path.isdir(sdir) else 0)
        src = store.meta.get("source_path", "")
        out.append({
            "book_dir": d,
            "title": store.title,
            "source_path": src,
            "original_path": store.meta.get("original_path"),
            "source_exists": bool(src) and os.path.isfile(src),
            "n_pages": store.n_pages,
            "last_page": last_page,
            "printed": store.printed_label(last_page) if last_page else None,
            "section": node["title"] if node else None,
            "last_opened": _last_opened(store),
            "journal_entries": n_entries,
            "journal_bytes": n_bytes,
            "n_sessions": n_sessions,
        })
    out.sort(key=lambda b: b["last_opened"], reverse=True)
    return out
