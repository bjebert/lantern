"""Library view: what Lantern shows when launched with no PDF.

One row per ingested book (books/*/meta.json via bookstore.library()) with
progress — page N of M, the section you were in, when it was last opened,
how much is in its journal. Double-click / Enter / Resume reopens a book in
the shell; "Open PDF…" browses for anything else (ingested or not). The ✕
at the end of a row (or the Delete key) removes that book's store folder
after confirmation — the book file itself is never touched.

choose_book() runs its own tk mainloop and returns the chosen PDF path, or
None if the window was closed.
"""

import os
import shutil
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import bookstore
import branding

BG = "#fafafa"
ACCENT = "#6d28d9"
MUTED = "#71717a"
REMOVE_GLYPH = "✕"        # ✕ — stays in the BMP, so Tk 8.6 renders it


def when(ts, now=None):
    """Compact relative time: 'today 14:09' / 'yesterday 09:12' /
    '3 days ago' / '2026-08-01'. Empty string for unknown."""
    if not ts:
        return ""
    t = time.localtime(ts)
    n = time.localtime(now or time.time())

    def midnight(lt):
        return time.mktime(lt[:3] + (0, 0, 0, 0, 0, -1))

    # calendar-day distance, not 24h buckets: last night is "yesterday"
    days = int(round((midnight(n) - midnight(t)) / 86400))
    if days <= 0:
        return time.strftime("today %H:%M", t)
    if days == 1:
        return time.strftime("yesterday %H:%M", t)
    if days < 14:
        return f"{days} days ago"
    return time.strftime("%Y-%m-%d", t)


def progress_text(b):
    """'p. 25 of 352 (book p. 11)  7%' / 'not started · 352 pages'"""
    n, last = b.get("n_pages"), b.get("last_page")
    if not last:
        return f"not started · {n} pages" if n else "not started"
    s = f"p. {last} of {n}" if n else f"p. {last}"
    if b.get("printed"):
        s += f" (book p. {b['printed']})"
    if n:
        s += f"  {100 * last // n}%"
    return s


def journal_text(b):
    n, size = b.get("journal_entries", 0), b.get("journal_bytes", 0)
    if not n:
        return "—"
    kb = size / 1024
    size_s = f"{kb:.0f} KB" if kb >= 1 else f"{size} B"
    return f"{n} note{'s' if n != 1 else ''} · {size_s}"


class LibraryWindow:
    def __init__(self, root, books):
        self.root = root
        self.books = books
        self.choice = None

        root.title("Lantern — Library")
        root.configure(bg=BG)
        root.geometry("1200x460")
        root.minsize(760, 300)

        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(head, text="Library", bg=BG, fg="#18181b",
                 font=("Segoe UI Semibold", 15)).pack(side="left")
        self.count = tk.Label(head, bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.count.pack(side="left", padx=(12, 0), pady=(5, 0))
        self._show_count(len(books))

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Lib.Treeview", background="white",
                        fieldbackground="white", rowheight=30,
                        font=("Segoe UI", 10), borderwidth=0)
        style.configure("Lib.Treeview.Heading", font=("Segoe UI Semibold", 9),
                        background="#f4f4f5", relief="flat")
        style.map("Lib.Treeview", background=[("selected", "#ede9fe")],
                  foreground=[("selected", "#18181b")])

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=16)
        cols = ("progress", "section", "opened", "journal", "remove")
        self.tree = ttk.Treeview(body, columns=cols, show="tree headings",
                                 style="Lib.Treeview", selectmode="browse")
        self.tree.heading("#0", text="Book", anchor="w")
        self.tree.heading("progress", text="Progress", anchor="w")
        self.tree.heading("section", text="Section", anchor="w")
        self.tree.heading("opened", text="Last opened", anchor="w")
        self.tree.heading("journal", text="Journal", anchor="w")
        self.tree.heading("remove", text="")
        # widths must sum below the window's default width: ttk clips the last
        # column rather than shrinking the stretch ones on first layout
        self.tree.column("#0", width=380, minwidth=200, stretch=True)
        self.tree.column("progress", width=200, minwidth=150, stretch=False)
        self.tree.column("section", width=220, minwidth=120, stretch=True)
        self.tree.column("opened", width=130, minwidth=100, stretch=False)
        self.tree.column("journal", width=130, minwidth=110, stretch=False)
        # per-row remove glyph; clicks on this column are handled by _on_click
        self.tree.column("remove", width=34, minwidth=34, stretch=False,
                         anchor="center")
        self.remove_col = f"#{len(cols)}"
        self.tree.tag_configure("missing", foreground="#a1a1aa")
        scroll = ttk.Scrollbar(body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        for i, b in enumerate(books):
            title = b["title"]
            tags = ()
            if not b["source_exists"]:
                title += "   (PDF missing)"
                tags = ("missing",)
            self.tree.insert("", "end", iid=str(i), text=title, tags=tags,
                             values=(progress_text(b), b.get("section") or "",
                                     when(b["last_opened"]), journal_text(b),
                                     REMOVE_GLYPH))
        if books:
            self.tree.selection_set("0")
            self.tree.focus("0")
        else:
            self._insert_empty_row()

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Return>", lambda e: self.open_selected())
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        root.bind("<Escape>", lambda e: root.destroy())

        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=16, pady=10)
        self.hint = tk.Label(foot, text="", bg=BG, fg=MUTED,
                             font=("Segoe UI", 9), anchor="w")
        self.hint.pack(side="left", fill="x", expand=True)
        tk.Button(foot, text="Open PDF…", command=self.browse, bd=0,
                  bg="#e4e4e7", fg="#18181b", activebackground="#d4d4d8",
                  padx=12, pady=5, cursor="hand2").pack(side="right")
        tk.Button(foot, text="Resume", command=self.open_selected, bd=0,
                  bg=ACCENT, fg="white", activebackground="#5b21b6",
                  activeforeground="white", padx=14, pady=5,
                  cursor="hand2").pack(side="right", padx=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", self._show_path)
        self._show_path()
        self.tree.focus_set()

    def _selected(self):
        sel = self.tree.selection()
        if not sel or sel[0] == "empty":
            return None
        return self.books[int(sel[0])]

    def _show_count(self, n):
        self.count.configure(
            text=f"{n} book{'s' if n != 1 else ''} in the store — "
                 "double-click to resume")

    def _insert_empty_row(self):
        self.tree.insert("", "end", iid="empty", tags=("missing",),
                         text="No ingested books yet — run "
                              "python app\\ingest.py <pdf>, or Open PDF…")

    def _hit_remove(self, event):
        """Row iid if the pointer is on the remove glyph of a book row."""
        if self.tree.identify_column(event.x) != self.remove_col:
            return None
        iid = self.tree.identify_row(event.y)
        return iid if iid and iid != "empty" else None

    def _on_motion(self, event):
        self.tree.configure(
            cursor="hand2" if self._hit_remove(event) else "")

    def _on_click(self, event):
        iid = self._hit_remove(event)
        if iid is None:
            return None
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.remove_selected()
        return "break"                    # no select/drag from the glyph

    def _on_double_click(self, event):
        if self._hit_remove(event):
            return "break"                # second click of a remove, not open
        self.open_selected()
        return None

    def remove_selected(self):
        """Delete the selected book's store folder (notes, journal, learning
        record, sessions) after confirmation. The PDF itself is untouched."""
        sel = self.tree.selection()
        b = self._selected()
        if not b:
            return
        ok = messagebox.askyesno(
            "Remove from library",
            f"Remove “{b['title']}” from the library?\n\n"
            "This deletes its journal, notes, learning record and chat "
            "sessions in\n" + b["book_dir"] + "\n\nThe book file itself is "
            "not touched. Close it in Lantern first if it is open.",
            icon="warning", default="no", parent=self.root)
        if not ok:
            return
        try:
            shutil.rmtree(b["book_dir"])
        except OSError as e:
            self.hint.configure(text=f"couldn't remove: {e}")
            return
        iid = sel[0]
        nxt = self.tree.next(iid) or self.tree.prev(iid)
        self.tree.delete(iid)
        rows = self.tree.get_children()
        self._show_count(len(rows))
        if rows:
            self.tree.selection_set(nxt or rows[0])
            self.tree.focus(nxt or rows[0])
        else:
            self._insert_empty_row()
        self._show_path()
        self.hint.configure(text=f"removed {b['title']}")

    def _show_path(self, event=None):
        b = self._selected()
        self.hint.configure(
            text=(b.get("original_path") or b["source_path"]) if b else "")

    def open_selected(self):
        b = self._selected()
        if not b:
            return
        if not b["source_exists"]:
            self.hint.configure(
                text=f"PDF not found: {b['source_path']} — use Open PDF… "
                     "to point at its new location")
            return
        self.choice = b["source_path"]
        self.root.destroy()

    def browse(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="Open a book",
            filetypes=[("Books", "*.pdf *.epub *.mobi *.fb2"),
                       ("PDF", "*.pdf"), ("EPUB", "*.epub"),
                       ("All files", "*.*")])
        if path:
            self.choice = os.path.normpath(path)
            self.root.destroy()


def choose_book():
    """Show the library; return the chosen PDF path or None (closed)."""
    books = bookstore.library()
    root = tk.Tk()
    branding.apply_icon(root)
    win = LibraryWindow(root, books)
    root.mainloop()
    return win.choice
