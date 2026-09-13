"""Lantern ingestion: PDF / EPUB -> book store (books/<book_id>/).

The store is the retrieval surface the chat agent greps/reads:
  text/            one markdown file per TOC section (level <= 2), with [p.N]
                   anchor lines (N = PDF page number, matching what SumatraPDF
                   displays and the shell's page poll reports)
  toc.md, toc.json exact outline titles + page ranges + file per section
  meta.json        book metadata, extraction stats, toc_source, session_ids

Reflowable books (.epub / .mobi / .fb2) are first laid out and converted to a
fixed-layout PDF, books/<book_id>/book.pdf, so the reader, the [p.N] anchors,
click-to-jump and underline annotations all agree on one pagination. The
original file is left untouched; meta.original_path points back at it.

Usage: python app\\ingest.py <book.pdf|.epub> [--force] [--books-dir DIR]

Requires pymupdf. The shell runs ingest() itself the first time a book is
opened (auto-ingest); this CLI is for rebuilding (--force) or --summarize.
"""

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import tempfile
import unicodedata
import zipfile

import pymupdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOKS_DIR = os.path.join(ROOT, "books")

MIN_PAGE_CHARS = 25          # below this a page counts as "no real text"
SCANNED_FRAC = 0.30          # this many near-empty pages => scanned PDF
SUSPECT_MEDIAN_CHARS = 200
SUSPECT_ALNUM_RATIO = 0.60
PSEUDO_CHAPTER_PAGES = 20    # TOC-less fallback segment size
MAX_FILE_LINES = 1900        # stay under the Read tool's default 2000-line window

REFLOWABLE_EXTS = {".epub", ".mobi", ".fb2"}
REFLOW_PAGE = (432, 648)     # 6x9in fixed page for converted ebooks
REFLOW_FONT_SIZE = 11
CONVERTED_PDF = "book.pdf"   # inside the book dir


def book_id(pdf_path):
    h = hashlib.sha1()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    return f"{slug(stem)}-{h.hexdigest()[:8]}", h.hexdigest()


def slug(text, max_len=60):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "untitled"


def is_reflowable(path):
    return os.path.splitext(path)[1].lower() in REFLOWABLE_EXTS


_CSS_RULE = re.compile(r"([^{}]+)\{([^}]*)\}")
_VALIGN = re.compile(r"vertical-align\s*:[^;}\"]*;?", re.I)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>", re.I | re.S)


def _fix_css(css):
    """Drop `vertical-align` from every rule whose selector mentions img."""
    def fix(m):
        sel, body = m.group(1), m.group(2)
        if re.search(r"\bimg\b", sel, re.I):
            body = _VALIGN.sub("", body)
        return sel + "{" + body + "}"
    return _CSS_RULE.sub(fix, css)


def _sanitize_epub(src, dst):
    """Copy an EPUB with `vertical-align` stripped from img CSS rules and
    inline img styles. MuPDF's layouter mis-sizes such images (the figure
    is drawn full width but the flow only reserves a text line for it, so
    captions and body text end up printed over the picture)."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            name = info.filename.lower()
            if name.endswith(".css"):
                data = _fix_css(data.decode("utf-8", "ignore")).encode("utf-8")
            elif name.endswith((".xhtml", ".html", ".htm")):
                t = data.decode("utf-8", "ignore")
                t = _STYLE_BLOCK.sub(
                    lambda m: m.group(0).replace(m.group(1),
                                                 _fix_css(m.group(1))), t)
                t = _IMG_TAG.sub(lambda m: _VALIGN.sub("", m.group(0)), t)
                data = t.encode("utf-8")
            comp = (zipfile.ZIP_STORED if info.filename == "mimetype"
                    else zipfile.ZIP_DEFLATED)
            zout.writestr(info, data, compress_type=comp)


def convert_reflowable(src_path, out_pdf):
    """Lay out an EPUB/MOBI/FB2 at a fixed page size and write it as a PDF
    (outline + title/author carried over). Returns the page count."""
    tmp = None
    if src_path.lower().endswith(".epub"):
        fd, tmp = tempfile.mkstemp(suffix=".epub",
                                   dir=os.path.dirname(out_pdf))
        os.close(fd)
        _sanitize_epub(src_path, tmp)
    try:
        doc = pymupdf.open(tmp or src_path)
        doc.layout(width=REFLOW_PAGE[0], height=REFLOW_PAGE[1],
                   fontsize=REFLOW_FONT_SIZE)
        toc = doc.get_toc(simple=True)
        info = doc.metadata or {}
        pdf = pymupdf.open("pdf", doc.convert_to_pdf())
        if toc:
            pdf.set_toc(toc)
        pdf.set_metadata({"title": info.get("title") or "",
                          "author": info.get("author") or "",
                          "creator": "Lantern ingest"})
        pdf.save(out_pdf, garbage=3, deflate=True)
        n = pdf.page_count
        pdf.close()
        doc.close()
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    return n


def extract_pages(doc):
    """Per-page text (dehyphenated) + printed page labels."""
    pages, labels = [], []
    for page in doc:
        pages.append(page.get_text("text", flags=pymupdf.TEXT_DEHYPHENATE
                                   | pymupdf.TEXT_MEDIABOX_CLIP).strip("\n"))
        try:
            labels.append(page.get_label() or "")
        except Exception:            # non-PDF documents have no label table
            labels.append("")
    if all(not lbl for lbl in labels):
        labels = None                      # no printed-label scheme in the PDF
    return pages, labels


def extraction_stats(pages, doc):
    lengths = [len(t.strip()) for t in pages]
    near_empty = [i for i, n in enumerate(lengths) if n < MIN_PAGE_CHARS]
    near_empty_with_image = sum(
        1 for i in near_empty if doc[i].get_images(full=False))
    text = "".join(pages)
    alnum = sum(c.isalnum() or c.isspace() for c in text)
    return {
        "n_pages": len(pages),
        "chars_median": int(statistics.median(lengths)) if lengths else 0,
        "chars_p10": int(sorted(lengths)[len(lengths) // 10]) if lengths else 0,
        "near_empty_pages": len(near_empty),
        "near_empty_with_image": near_empty_with_image,
        "alnum_ratio": round(alnum / len(text), 3) if text else 0.0,
    }


def looks_scanned(stats):
    frac = stats["near_empty_pages"] / max(1, stats["n_pages"])
    return frac > SCANNED_FRAC and stats["near_empty_with_image"] > 0


# ---------- TOC ----------

# node: {"id", "level", "title", "pdf_start", "pdf_end", "file"}

NUM_SECTION = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\b")
NUM_CHAPTER = re.compile(r"^\s*(?:Chapter\s+)?(\d{1,2})\b", re.IGNORECASE)


def toc_nodes(raw_toc, n_pages):
    """PyMuPDF get_toc() [[level, title, page1based], ...] -> section nodes.

    Numbering is parsed from the outline titles where present ("6.2 ..."),
    because level-1 entries also include unnumbered front/back matter
    (Preface, References) — positional counters would drift off the book's
    real chapter numbers. Unnumbered nodes get 00.NN (before chapter 1) or
    zz.NN (after) so filenames still sort in reading order.
    """
    entries = [(lvl, " ".join(t.split()), pg) for lvl, t, pg in raw_toc
               if lvl <= 2 and pg >= 1]
    nodes, seen_chapter, misc = [], False, 0
    cur_chapter = 0
    for lvl, title, pg in entries:
        m_sec = NUM_SECTION.match(title)
        m_ch = NUM_CHAPTER.match(title) if lvl == 1 else None
        if lvl == 1 and m_ch:
            cur_chapter, seen_chapter = int(m_ch.group(1)), True
            nid = f"{cur_chapter:02d}.00"
        elif lvl == 2 and m_sec:
            nid = f"{int(m_sec.group(1)):02d}.{int(m_sec.group(2)):02d}"
        elif lvl == 2 and seen_chapter:
            # unnumbered subsection (e.g. "Summary"): tack onto its chapter
            sub = sum(1 for n in nodes
                      if n["id"].startswith(f"{cur_chapter:02d}.")) or 1
            nid = f"{cur_chapter:02d}.{90 + (sub % 10):02d}"
        else:
            misc += 1
            nid = (f"00.{misc:02d}" if not seen_chapter else f"zz.{misc:02d}")
        nodes.append({"id": nid, "level": lvl, "title": title,
                      "pdf_start": min(pg, n_pages), "pdf_end": None,
                      "file": None})
    # page ranges: a node runs to the page before the next node's start
    # (same-page neighbours share the boundary page)
    for i, n in enumerate(nodes):
        nxt = nodes[i + 1]["pdf_start"] if i + 1 < len(nodes) else n_pages + 1
        n["pdf_end"] = max(n["pdf_start"], nxt - 1)
    if nodes and nodes[0]["pdf_start"] > 1:
        # cover/contents/preface pages before the first outline node — keep
        # them greppable (the printed TOC especially)
        nodes.insert(0, {"id": "00.00",
                         "title": "Front matter (cover, contents, preface)",
                         "level": 1, "pdf_start": 1,
                         "pdf_end": nodes[0]["pdf_start"] - 1, "file": None})
    for n in nodes:
        n["file"] = f"text/{n['id']}-{slug(strip_numbering(n['title']))}.md"
    return nodes


def strip_numbering(title):
    return re.sub(r"^\s*(Chapter\s+\d+[:.]?|\d+(\.\d+)*\.?)\s*", "", title,
                  flags=re.IGNORECASE) or title


DOT_LEADER = re.compile(r"(?:\.\s+){2,}\.?")
ROMAN = re.compile(r"^[ivxlcdm]+$", re.IGNORECASE)


def _norm(text):
    """Ligature/case/space-insensitive comparison key."""
    text = unicodedata.normalize("NFKD", text)
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def printed_contents_toc(pages):
    """Parse the book's own printed Contents pages into a TOC.

    Books without an embedded PDF outline usually still typeset a full
    contents list ("6.2 Advantages of TD Prediction Methods . . . 148").
    Printed page numbers are mapped to PDF pages by scoring a single global
    offset: the one that places the most chapter titles on their stated page.
    Returns (raw_toc, offset) in get_toc() shape, or None if parsing or the
    offset check fails.
    """
    start = next((i for i in range(min(20, len(pages)))
                  if any(l.strip().lower() == "contents"
                         for l in pages[i].splitlines()[:4])), None)
    if start is None:
        return None
    contents = []
    for i in range(start, min(start + 12, len(pages))):
        if i > start and len(DOT_LEADER.findall(pages[i])) < 3:
            break
        contents += pages[i].splitlines()

    entries, num, title_parts = [], None, []   # (num|None, title, printed_pg)
    for raw in contents:
        ln = " ".join(raw.split())
        if not ln or ln.lower() in ("contents",) or ROMAN.fullmatch(ln) \
                and not title_parts:
            continue
        stripped = DOT_LEADER.sub(" ", ln).strip()
        if not stripped:
            continue
        if title_parts and stripped.isdigit():          # bare page -> flush
            entries.append((num, " ".join(title_parts), int(stripped)))
            num, title_parts = None, []
            continue
        if title_parts and ROMAN.fullmatch(stripped):   # roman page: front
            num, title_parts = None, []                 # matter, drop entry
            continue
        m = re.fullmatch(r"(.+?)\s+(\d{1,4})", stripped)
        if m and DOT_LEADER.search(ln):                 # "title .... 279"
            body, pg = m.group(1), int(m.group(2))
            mnum = re.match(r"^[∗*]?(\d{1,2}(?:\.\d{1,2})?)\s+(.+)$", body)
            entries.append((mnum.group(1), mnum.group(2), pg) if mnum
                           else (num, body, pg))
            num, title_parts = None, []
            continue
        if re.fullmatch(r"[∗*]?\d{1,2}(\.\d{1,2})?", stripped):
            num = stripped.lstrip("∗*")                 # "1.1" on its own line
            continue
        mnum = re.match(r"^[∗*]?(\d{1,2}(?:\.\d{1,2})?)\s+(.+)$", stripped)
        if mnum and not title_parts:
            num, title_parts = mnum.group(1), [mnum.group(2)]
        else:
            title_parts.append(stripped)

    chapters = [(n, t, p) for n, t, p in entries if n and "." not in n]
    if len(chapters) < 3:
        return None
    best_off, best_hits = None, 0
    for off in range(0, 80):
        hits = sum(1 for n, t, p in chapters
                   if 1 <= p + off <= len(pages)
                   and _norm(t) in _norm(pages[p + off - 1]))
        if hits > best_hits:
            best_off, best_hits = off, hits
    if best_hits < max(2, len(chapters) // 2):
        return None

    raw_toc = []
    for n, t, p in entries:
        pdf = p + best_off
        if not 1 <= pdf <= len(pages):
            continue
        title = f"{n} {t}" if n else t
        raw_toc.append([2 if n and "." in n else 1, title, pdf])
    return raw_toc, best_off


def heuristic_toc(doc, pages):
    """No embedded outline: guess chapter starts from oversized heading spans;
    floor is fixed-size pseudo-chapters. Never blocks ingestion."""
    sizes = []
    for i in range(min(len(pages), 40)):
        for block in doc[i].get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                sizes += [s["size"] for s in line.get("spans", [])
                          if s.get("text", "").strip()]
    body = statistics.median(sizes) if sizes else 10.0
    heading_re = re.compile(r"^(Chapter\s+\d+|\d+(\.\d+)?\s+\S)")
    raw = []
    for i in range(len(pages)):
        for block in doc[i].get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = " ".join(s["text"] for s in spans).strip()
                if (spans and text and len(text) < 90
                        and max(s["size"] for s in spans) >= body + 2
                        and heading_re.match(text)):
                    lvl = 2 if NUM_SECTION.match(text) else 1
                    raw.append([lvl, text, i + 1])
    if len(raw) >= 3:
        return raw, "heuristic"
    n = len(pages)
    raw = [[1, f"Part {k + 1} (pages {s + 1}-{min(s + PSEUDO_CHAPTER_PAGES, n)})",
            s + 1] for k, s in enumerate(range(0, n, PSEUDO_CHAPTER_PAGES))]
    return raw, "pseudo"


# ---------- store writing ----------

def section_markdown(node, pages, labels):
    lo, hi = node["pdf_start"], node["pdf_end"]
    label = (f" — pp. {labels[lo - 1]}–{labels[hi - 1]}"
             if labels and labels[lo - 1] and labels[hi - 1] else "")
    out = [f"# {node['title']}{label} (PDF {lo}–{hi})", ""]
    for p in range(lo, hi + 1):
        out += [f"[p.{p}]", pages[p - 1], ""]
    return "\n".join(out)


def split_oversized(node, text):
    """Keep files under the Read tool's default window; split on page anchors."""
    lines = text.splitlines()
    if len(lines) <= MAX_FILE_LINES:
        return [(node, text)]
    parts, cur, letter = [], [lines[0], ""], 0
    for ln in lines[2:]:
        if ln.startswith("[p.") and len(cur) > MAX_FILE_LINES:
            parts.append(cur)
            cur = [lines[0] + f" (cont. {chr(98 + letter)})", ""]
            letter += 1
        cur.append(ln)
    parts.append(cur)
    out = []
    for k, chunk in enumerate(parts):
        sub = dict(node)
        sub["id"] = f"{node['id']}{chr(97 + k)}"
        sub["file"] = node["file"].replace(f"{node['id']}-", f"{sub['id']}-", 1)
        first = next((l for l in chunk if l.startswith("[p.")), None)
        last = next((l for l in reversed(chunk) if l.startswith("[p.")), None)
        if first and last:
            sub["pdf_start"] = int(first[3:-1])
            sub["pdf_end"] = int(last[3:-1])
        out.append((sub, "\n".join(chunk)))
    return out


def toc_markdown(nodes, labels, title):
    lines = [f"# Table of contents — {title}", ""]
    for n in nodes:
        printed = (f"p. {labels[n['pdf_start'] - 1]} "
                   if labels and labels[n["pdf_start"] - 1] else "")
        indent = "  " * (n["level"] - 1)
        lines.append(f"{indent}- {n['title']} — {printed}"
                     f"(PDF {n['pdf_start']}–{n['pdf_end']}) -> {n['file']}")
    return "\n".join(lines) + "\n"


def ingest(src_path, books_dir=BOOKS_DIR, force=False, title=None):
    """Build (or rebuild with force) the store for a PDF or reflowable
    ebook. Returns the book dir, or None when the PDF looks scanned."""
    src_path = os.path.abspath(src_path)
    bid, sha = book_id(src_path)          # hash of the file the user has
    book_dir = os.path.join(books_dir, bid)
    reflow = is_reflowable(src_path)
    pdf_path = os.path.join(book_dir, CONVERTED_PDF) if reflow else src_path
    if (os.path.isdir(os.path.join(book_dir, "text"))
            and os.path.isfile(pdf_path) and not force):
        print(f"already ingested: {book_dir}  (--force to rebuild)")
        return book_dir

    if reflow:
        os.makedirs(book_dir, exist_ok=True)
        if not os.path.isfile(pdf_path):   # --force keeps an existing PDF:
            print(f"converting {src_path} -> {pdf_path} ...")  # annotations
            n = convert_reflowable(src_path, pdf_path)
            print(f"  {n} pages at {REFLOW_PAGE[0]}x{REFLOW_PAGE[1]}pt, "
                  f"{REFLOW_FONT_SIZE}pt body text")

    doc = pymupdf.open(pdf_path)
    title = (title or (doc.metadata or {}).get("title")
             or os.path.splitext(os.path.basename(src_path))[0])
    print(f"extracting {doc.page_count} pages from {pdf_path} ...")
    pages, labels = extract_pages(doc)
    stats = extraction_stats(pages, doc)

    os.makedirs(book_dir, exist_ok=True)
    meta = {
        "title": title,
        "source_path": pdf_path,             # what the reader opens
        "source_sha1": sha,
        **({"original_path": src_path,       # the ebook it was converted from
            "original_format": os.path.splitext(src_path)[1][1:].lower()}
           if reflow else {}),
        "n_pages": doc.page_count,
        "page_labels": labels,
        "extraction_stats": stats,
        "needs_ocr": looks_scanned(stats),
        "extraction_suspect": (stats["chars_median"] < SUSPECT_MEDIAN_CHARS
                               or stats["alnum_ratio"] < SUSPECT_ALNUM_RATIO),
        "toc_source": None,
        "session_ids": {},
    }
    if meta["needs_ocr"]:
        with open(os.path.join(book_dir, "meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        print("\n*** This PDF looks SCANNED (little extractable text). ***\n"
              f"*** {stats['near_empty_pages']}/{stats['n_pages']} pages have "
              "no real text layer. Run OCR first (e.g. ocrmypdf), then "
              "re-ingest. No text store was written. ***")
        return None

    raw_toc = doc.get_toc(simple=True)
    if raw_toc:
        meta["toc_source"] = "embedded"
    elif (pc := printed_contents_toc(pages)) is not None:
        raw_toc, offset = pc
        meta["toc_source"] = "printed-contents"
        meta["printed_offset"] = offset
        if labels is None:
            # printed page N = PDF page N+offset; front matter labels unknown
            labels = ["" if p <= offset else str(p - offset)
                      for p in range(1, doc.page_count + 1)]
            meta["page_labels"] = labels
        print(f"no embedded outline — parsed the printed Contents pages "
              f"({len(raw_toc)} entries, printed->PDF offset {offset})")
    else:
        raw_toc, meta["toc_source"] = heuristic_toc(doc, pages)
        print(f"no embedded outline or parseable Contents — using "
              f"{meta['toc_source']} TOC ({len(raw_toc)} entries); eyeball "
              "toc.md and hand-edit toc.json if it's off")
    nodes = toc_nodes(raw_toc, doc.page_count)

    text_dir = os.path.join(book_dir, "text")
    os.makedirs(text_dir, exist_ok=True)
    for old in os.listdir(text_dir):
        os.remove(os.path.join(text_dir, old))
    final_nodes = []
    for node in nodes:
        for sub, text in split_oversized(node, section_markdown(node, pages,
                                                                labels)):
            with open(os.path.join(book_dir, sub["file"]), "w",
                      encoding="utf-8") as f:
                f.write(text + "\n")
            final_nodes.append(sub)

    with open(os.path.join(book_dir, "toc.json"), "w", encoding="utf-8") as f:
        json.dump(final_nodes, f, indent=1)
    with open(os.path.join(book_dir, "toc.md"), "w", encoding="utf-8") as f:
        f.write(toc_markdown(final_nodes, labels, title))
    with open(os.path.join(book_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f"book store written: {book_dir}")
    print(f"  {len(final_nodes)} section files, toc_source={meta['toc_source']}"
          f", median {stats['chars_median']} chars/page"
          + (", EXTRACTION SUSPECT (garbled text?)"
             if meta["extraction_suspect"] else ""))
    return book_dir


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", metavar="BOOK", help="a .pdf, .epub, .mobi or .fb2")
    ap.add_argument("--force", action="store_true",
                    help="rebuild the text store (a converted ebook PDF is kept)")
    ap.add_argument("--books-dir", default=BOOKS_DIR)
    ap.add_argument("--title", help="book title for prompts/citations "
                    "(default: PDF metadata, else filename)")
    ap.add_argument("--summarize", action="store_true",
                    help="also write per-chapter summaries (one Haiku call "
                    "per chapter; needs the claude CLI login)")
    args = ap.parse_args()
    if not os.path.isfile(args.pdf):
        sys.exit(f"not found: {args.pdf}")
    book_dir = ingest(args.pdf, args.books_dir, args.force, args.title)
    if book_dir is None:
        sys.exit(2)
    if args.summarize:
        import asyncio
        import summaries
        from bookstore import BookStore
        from bus import one_shot
        store = BookStore.open(book_dir)
        asyncio.run(summaries.summarize_all(store, one_shot))


if __name__ == "__main__":
    main()
