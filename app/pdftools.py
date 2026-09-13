"""Runtime PDF helpers: underline annotations and page-image renders.

pymupdf is imported lazily (only when a feature is actually used) so app
startup stays light and everything degrades gracefully when it's missing.
SumatraPDF neither locks the PDF nor minds it changing underneath (the LaTeX
workflow), so an incremental save while the book is open is safe. The
embedded (-plugin) reader does not watch the file, though: the shell sends
Sumatra's Reload after each write (bus._reload_reader) so the new
annotation appears in place.
"""

import importlib.util
import os
import shutil

HAVE_PYMUPDF = importlib.util.find_spec("pymupdf") is not None

ACCENT = (109 / 255, 40 / 255, 217 / 255)   # Lantern violet
MAX_CONTENT = 4000                          # popup text cap (chars)
SEARCH_HEAD = 64                            # fallback probe when the full
                                            # passage doesn't match


def _backup_once(pdf_path):
    """One-time pristine copy next to the PDF, before we ever write to it."""
    bak = pdf_path + ".lantern-orig"
    legacy = pdf_path + ".readbuddy-orig"      # pre-rename backups stay pristine
    if os.path.isfile(legacy) and not os.path.isfile(bak):
        os.replace(legacy, bak)
    if not os.path.isfile(bak):
        shutil.copyfile(pdf_path, bak)


def underline(pdf_path, pages, passage, content, title="Lantern"):
    """Underline `passage` on the first of `pages` (1-based) where it's found
    and attach `content` as the annotation's popup note. Returns the page hit,
    or None if pymupdf is missing, the text can't be located, or the file
    can't be saved incrementally (we never rewrite the whole PDF)."""
    if not HAVE_PYMUPDF:
        return None
    import pymupdf
    needle = " ".join(passage.split())
    if not needle:
        return None
    doc = pymupdf.open(pdf_path)
    try:
        if not doc.can_save_incrementally():
            return None
        for pno in pages:
            if not 1 <= pno <= doc.page_count:
                continue
            page = doc[pno - 1]
            quads = page.search_for(needle, quads=True)
            if not quads and len(needle) > SEARCH_HEAD:
                head = needle[:SEARCH_HEAD].rsplit(" ", 1)[0]
                quads = page.search_for(head, quads=True)
            if not quads:
                continue
            _backup_once(pdf_path)
            annot = page.add_underline_annot(quads)
            annot.set_colors(stroke=ACCENT)
            annot.set_info(title=title, content=content[:MAX_CONTENT])
            annot.update()
            doc.save(pdf_path, incremental=True,
                     encryption=pymupdf.PDF_ENCRYPT_KEEP)
            return pno
        return None
    finally:
        doc.close()


def render_page(pdf_path, pno, out_dir, max_px=1600):
    """Render PDF page `pno` (1-based) to out_dir/p-<n>.png at up to 2x zoom,
    longest side capped at max_px. Returns the file path."""
    if not HAVE_PYMUPDF:
        return None
    import pymupdf
    doc = pymupdf.open(pdf_path)
    try:
        if not 1 <= pno <= doc.page_count:
            return None
        page = doc[pno - 1]
        r = page.rect
        zoom = min(2.0, max_px / max(r.width, r.height, 1))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"p-{pno}.png")
        pix.save(path)
        return path
    finally:
        doc.close()
