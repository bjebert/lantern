"""Lantern v0.1 shell: SumatraPDF embedded via -plugin + Claude chat sidebar.

The chat pane is an Edge --app window (chat.html: markdown + KaTeX + streaming)
reparented into the sidebar, talking to the context bus (bus.py) which keeps
one persistent Claude session alive. Falls back to a plain tk chat if Edge
can't be embedded (--tkchat forces it).

Usage: pythonw lantern_shell.py [path-to-pdf] [--diag] [--tkchat]
                                  [--selftest "message"]
With no PDF the library view (library.py) lists ingested books with their
progress; pick one to resume, or browse for any PDF. The sidebar's Library
button closes the open book and returns to that view.
"""

import contextlib
import ctypes
import io
import logging
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

CREATE_NO_WINDOW = 0x08000000

# Under pythonw there is no console, so any console child (claude.exe via the
# SDK) would flash its own window — default every Popen to CREATE_NO_WINDOW.
if not kernel32.GetConsoleWindow():
    _orig_popen_init = subprocess.Popen.__init__

    def _noconsole_popen_init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
        _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _noconsole_popen_init

import bus  # noqa: E402  (same directory; after the Popen patch on purpose)
import branding  # noqa: E402

SUMATRA = os.path.expandvars(r"%LOCALAPPDATA%\SumatraPDF\SumatraPDF.exe")
EDGE_CANDIDATES = [
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]
EDGE_PROFILE = os.path.expandvars(r"%LOCALAPPDATA%\lantern\edge-profile")
# Filename stem -> real title, so Claude knows what book it's tutoring.
BOOK_ALIASES = {
    "RL": "Reinforcement Learning: An Introduction (Sutton & Barto, 2nd ed.)",
}
DIAG_LOG = os.path.join(os.path.expandvars("%TEMP%"), "lantern_diag.log")

SIDEBAR_WIDTH = 430
BG = "#fafafa"
ACCENT = "#6d28d9"

# win32 bits
GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
SW_RESTORE = 9
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_ASYNCWINDOWPOS = 0x4000
SMTO_ABORTIFHUNG = 0x0002
WM_NULL = 0
WM_GETTEXT = 0x000D
WM_SETTEXT = 0x000C
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN, VK_RWIN = 0x5B, 0x5C
WM_QUIT = 0x0012
WM_SYSKEYDOWN = 0x0104
WM_COMMAND = 0x0111
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_CANCELMODE = 0x001F
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_USER = 0x0400
TTM_GETTOOLCOUNT = WM_USER + 13
TTM_GETTEXTW = WM_USER + 56
TTM_ENUMTOOLSW = WM_USER + 58
LPSTR_TEXTCALLBACK = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1  # (LPWSTR)-1
TOOLTIPS_CLASS = "tooltips_class32"
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
MEM_COMMIT_RESERVE = 0x3000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
TIP_READ_CHARS = 512              # the chat ref sits in the note's first line
TIP_BUFFER_BYTES = 64 * 1024      # remote text buffer, see _tooltip_text
SW_HIDE = 0
GA_ROOT = 2
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
# Sumatra window classes. Canvas: src/SumatraPDF.h, unchanged 3.4 -> 3.6.
# Toasts ("Select content with Ctrl+left mouse button" & co.) are children
# of the canvas: 3.4 registers its own class (src/Notifications.cpp); 3.5+
# build them on wingui's Wnd, whose default class is shared by other custom
# windows (src/wingui/Wnd.cpp kDefaultClassName) — hence the parent check
# in is_notify_window.
SUMATRA_CANVAS_CLASS = "SUMATRA_PDF_CANVAS"
SUMATRA_NOTIFY_CLASSES = ("SUMATRA_PDF_NOTIFICATION_WINDOW",   # 3.4
                          "SumatraWgDefaultWinClass")          # 3.5 / 3.6
MENU_PREVIEW_CHARS = 48
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = (wintypes.POINT,)
user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = (wintypes.HWND,)
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.c_void_p)
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
user32.SetClipboardData.argtypes = (wintypes.UINT, ctypes.c_void_p)
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SendMessageTimeoutW.argtypes = (
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM,
                              wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE,
                                     wintypes.DWORD)
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM,
                                  wintypes.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
# Cross-process read of Sumatra's annotation tooltip (see _annotation_tip)
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.VirtualAllocEx.argtypes = (wintypes.HANDLE, ctypes.c_void_p,
                                    ctypes.c_size_t, wintypes.DWORD,
                                    wintypes.DWORD)
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualFreeEx.argtypes = (wintypes.HANDLE, ctypes.c_void_p,
                                   ctypes.c_size_t, wintypes.DWORD)
kernel32.WriteProcessMemory.argtypes = (wintypes.HANDLE, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_size_t,
                                        ctypes.POINTER(ctypes.c_size_t))
kernel32.ReadProcessMemory.argtypes = (wintypes.HANDLE, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t))
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
user32.EnumThreadWindows.argtypes = (wintypes.DWORD, WNDENUMPROC,
                                     wintypes.LPARAM)


class TOOLINFOW(ctypes.Structure):
    """commctrl.h TOOLINFOW, laid out for Sumatra's bitness (x64: 72 bytes;
    it must match the *target* process, not just ours)."""
    _fields_ = [("cbSize", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("hwnd", wintypes.HWND), ("uId", ctypes.c_size_t),
                ("rect", wintypes.RECT), ("hinst", wintypes.HINSTANCE),
                ("lpszText", ctypes.c_void_p), ("lParam", wintypes.LPARAM),
                ("lpReserved", ctypes.c_void_p)]
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetClipboardData.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)

# "Send selection to chat" hotkey. The shell installs a low-level keyboard
# hook that only acts while this window is in the foreground (Sumatra's own
# per-handler Key binding, 3.6+, would bounce through the browser instead);
# the copy itself is Sumatra's own Ctrl+C command (WM_COMMAND
# CmdCopySelection to its frame window), then the clipboard text goes down
# the same path as the right-click "Copy to chat".
SELECTION_HOTKEY_LABEL = "Ctrl+Shift+C"
SELECTION_HOTKEY_VK = 0x43                          # 'C'
# CmdCopySelection per Sumatra major.minor, from src/Commands.h at the
# release tags. 3.4/3.5: CmdFirst = 200 + position in the COMMANDS(V) list
# (3.4.6rel -> 228, 3.5.2rel -> 232). 3.6 numbers the enum explicitly
# (3.6rel and 3.6.1rel -> 234). Ids shift between releases, so an unknown
# version disables the hotkey and the menu instead of firing a random
# command — add the next release here after checking its Commands.h.
SUMATRA_COPY_CMD = {"3.4": 228, "3.5": 232, "3.6": 234}
# CmdReloadDocument, same derivation (3.4.6rel -> 209, 3.5.2rel -> 212,
# 3.6rel / 3.6.1rel -> 214). Sent after a fresh underline: the embedded
# reader never watches its file (see bus._reload_reader).
SUMATRA_RELOAD_CMD = {"3.4": 209, "3.5": 212, "3.6": 214}


class VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in (
        "dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS",
        "dwProductVersionMS", "dwProductVersionLS", "dwFileFlagsMask",
        "dwFileFlags", "dwFileOS", "dwFileType", "dwFileSubtype",
        "dwFileDateMS", "dwFileDateLS")]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


def window_class(hwnd):
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def is_notify_window(hwnd):
    """One of Sumatra's in-canvas toasts, on any supported version."""
    return (window_class(hwnd) in SUMATRA_NOTIFY_CLASSES
            and window_class(user32.GetParent(hwnd)) == SUMATRA_CANVAS_CLASS)


def set_clipboard_text(text):
    """Put plain text on the clipboard (the menu's "Copy text")."""
    data = text.encode("utf-16-le") + b"\0\0"
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    for _ in range(10):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
            finally:
                user32.CloseClipboard()
        time.sleep(0.02)
    return False

log = logging.getLogger("lantern.shell")


def async_move(hwnd, w, h, x=0, y=0, extra_flags=0):
    """Position a foreign child without ever blocking on its (busy) thread."""
    user32.SetWindowPos(hwnd, 0, x, y, w, h,
                        SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS
                        | extra_flags)


def hwnd_pid_tid(hwnd):
    pid = wintypes.DWORD()
    tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value, tid


def find_child_of_pid(parent_hwnd, pid):
    """First direct child of parent_hwnd owned by process `pid` (plan step 4:
    never trust FindWindowExW's first child — tk widgets get HWNDs too)."""
    child = 0
    while True:
        child = user32.FindWindowExW(parent_hwnd, child, None, None)
        if not child:
            return None, None
        cpid, ctid = hwnd_pid_tid(child)
        if cpid == pid:
            return child, ctid


def find_toplevel_of_pid(pid, wclass=None):
    hits = []

    def cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            cpid, _ = hwnd_pid_tid(hwnd)
            if cpid == pid:
                buf = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, buf, 64)
                if wclass is None or buf.value == wclass:
                    hits.append(hwnd)
                    return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return hits[0] if hits else None


def find_descendant_class(root_hwnd, wclass):
    hits = []

    def cb(hwnd, lparam):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value == wclass:
            hits.append(hwnd)
            return False
        return True

    user32.EnumChildWindows(root_hwnd, WNDENUMPROC(cb), 0)
    return hits[0] if hits else None


def file_version(path):
    """'major.minor.build.rev' from an exe's VERSIONINFO resource, or None."""
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return None
    ptr, length = ctypes.c_void_p(), wintypes.UINT()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr),
                              ctypes.byref(length)) or not ptr.value:
        return None
    ffi = ctypes.cast(ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    ms, ls = ffi.dwFileVersionMS, ffi.dwFileVersionLS
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def clipboard_text(tries=10):
    """CF_UNICODETEXT from the clipboard; retries while another process
    (Sumatra mid-copy) still holds it open. None if never obtainable."""
    for _ in range(tries):
        if user32.OpenClipboard(None):
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                p = kernel32.GlobalLock(h)
                if not p:
                    return ""
                try:
                    return ctypes.wstring_at(p)
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
        time.sleep(0.02)
    return None


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class LanternShell:
    def __init__(self, root, pdf_path, diag=False, tkchat=False, greet=True):
        self.root = root
        self.pdf_path = pdf_path
        self.diag = diag
        stem = os.path.splitext(os.path.basename(pdf_path))[0]
        self.book_name = BOOK_ALIASES.get(stem, stem)
        self.sumatra_proc = None
        self.sumatra_hwnd = None
        self.sumatra_tid = None
        self.edge_proc = None
        self.edge_hwnd = None
        self.sidebar_visible = True
        self.input = None                 # tk fallback input, if built
        self._mapped = False
        self._reader_size = None
        self._chat_size = None
        self._reader_job = None
        self._chat_job = None
        self._history_seen = 0
        self._edge_off = (0, 0, 0, 0)     # Edge chrome clipped outside the pane
        self._focus_wanted = 0            # bumped by /focus posts from chat.html
        self._focus_handled = 0
        self._pump_tick = 0
        self._root_hwnd = None            # top-level HWND, for the hotkey hook
        self._hook_tid = None
        self._hook_proc = None            # keep the ctypes callback alive
        self._copy_cmd = None             # Sumatra's CmdCopySelection, if known
        self._hotkey_last = 0.0
        self._mouse_proc = None           # WH_MOUSE_LL callback (kept alive)
        self._rdown = None                # screen point of a swallowed right-down
        self._ldown = None                # annotation note under a swallowed Ctrl+left-down
        self._reload_cmd = None           # Sumatra's CmdReloadDocument for its version
        self._menu = None                 # the open context menu, if any
        self._click_gen = 0               # bumped per right-click; stale probes drop out
        self._probe_lock = threading.Lock()   # one clipboard/toast probe at a time
        self.back_to_library = False      # set by go_library(); main() loops

        root.title(f"Lantern — {self.book_name}")
        root.geometry("1500x950")
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # grounded answers need the book store (app/ingest.py); build it on
        # first open — pure local text extraction, ~1s for a PDF, a few
        # seconds for an EPUB (which is converted to a fixed-layout PDF)
        store, ingest_note = None, None
        try:
            import bookstore
            store = bookstore.BookStore.find_for_pdf(pdf_path)
            if store is None:
                store, ingest_note = self._auto_ingest(pdf_path)
        except Exception:
            log.exception("book store lookup failed")
        self.store = store
        if store is not None:
            reader_path = store.meta.get("source_path") or pdf_path
            if os.path.isfile(reader_path):
                self.pdf_path = reader_path     # converted EPUB -> its PDF
            try:
                store.touch_opened()      # library recency (meta.last_opened)
            except OSError:
                log.exception("couldn't stamp last_opened")
        self.engine = bus.ChatEngine(
            self.book_name, greet=greet,
            book_dir=store.book_dir if store else None)
        if store is None:
            self.engine.history.append({
                "role": "meta",
                "text": (ingest_note or "book text not indexed") + " — "
                        "answers come from Claude's general knowledge, not "
                        "the book. To retry by hand:\n"
                        f"python app\\ingest.py \"{pdf_path}\""})
        else:
            log.info("book store: %s", store.book_dir)
            if ingest_note:
                self.engine.history.append({"role": "meta",
                                            "text": ingest_note})
        try:
            self.port = self.engine.start()
        except Exception as e:
            log.exception("context bus failed")
            self.port = None
            tkchat = True
            self._bus_error = str(e)
        self.engine.on_focus = self._request_edge_focus   # called from bus thread
        self.engine.on_goto = self.goto_page              # called from bus thread
        self.engine.on_reload = self.reload_reader        # called from bus threads
        self.tkchat = tkchat or not bus.HAVE_AIOHTTP

        self.paned = tk.PanedWindow(
            root, orient="horizontal", sashwidth=6, bd=0, bg="#d4d4d8"
        )
        self.paned.pack(fill="both", expand=True)

        # Reader host: Sumatra reparents itself into this frame's HWND.
        self.reader_host = tk.Frame(self.paned, bg="#3f3f46")
        self.paned.add(self.reader_host, stretch="always", minsize=300)
        self.reader_host.bind("<Configure>", self.on_reader_resize)

        # Floating button to restore a collapsed sidebar (hidden while visible).
        self.restore_btn = tk.Button(
            self.reader_host, text="❮ Chat", command=self.toggle_sidebar,
            bd=0, bg="#27272a", fg="#fafafa", activebackground="#52525b",
            activeforeground="#fafafa", padx=10, pady=4, cursor="hand2",
        )

        self.build_sidebar()

        if self.diag:
            self.start_diagnostics()

        # Launch children only once the window is actually mapped (plan step 7):
        # winfo_id() on an unmapped frame forces premature HWND creation.
        root.bind("<Map>", self._on_first_map)

    # ---------- sidebar ----------

    def build_sidebar(self):
        self.sidebar = tk.Frame(self.paned, bg=BG, width=SIDEBAR_WIDTH)

        header = tk.Frame(self.sidebar, bg=BG)
        header.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(
            header, text="Claude", bg=BG, fg="#18181b",
            font=("Segoe UI Semibold", 12),
        ).pack(side="left")
        tk.Button(
            header, text="❯", command=self.toggle_sidebar, bd=0, bg=BG,
            fg="#71717a", activebackground=BG, cursor="hand2",
            font=("Segoe UI", 11),
        ).pack(side="right")
        tk.Button(
            header, text="Library", command=self.go_library, bd=0,
            bg="#e4e4e7", fg="#18181b", activebackground="#d4d4d8",
            padx=8, pady=1, cursor="hand2", font=("Segoe UI", 9),
        ).pack(side="right", padx=(0, 10))

        # Edge (or the tk fallback chat) fills this frame.
        self.chat_host = tk.Frame(self.sidebar, bg="white")
        self.chat_host.pack(fill="both", expand=True, padx=(10, 10), pady=(0, 8))
        self.chat_host.bind("<Configure>", self.on_chat_resize)

        if self.tkchat:
            self.build_tk_chat()

        self.paned.add(self.sidebar, stretch="never", width=SIDEBAR_WIDTH,
                       minsize=0)

    def toggle_sidebar(self):
        if self.sidebar_visible:
            self.paned.forget(self.sidebar)
            self.restore_btn.place(relx=1.0, y=10, x=-10, anchor="ne")
        else:
            self.restore_btn.place_forget()
            self.paned.add(self.sidebar, stretch="never",
                           width=SIDEBAR_WIDTH, minsize=0)
        self.sidebar_visible = not self.sidebar_visible

    # ---------- startup of embedded children ----------

    def _on_first_map(self, event=None):
        if self._mapped:
            return
        self._mapped = True
        self.root.after_idle(self.launch_sumatra)
        if not self.tkchat:
            self.root.after_idle(self.launch_edge)

    # ---------- sumatra embedding ----------

    def launch_sumatra(self):
        # Fallback right-click actions ride Sumatra's SelectionHandlers
        # setting — (re)install them before Sumatra starts and reads its
        # settings file. On known versions (SUMATRA_COPY_CMD) they are never
        # seen (the mouse hook replaces the context menu wholesale); on
        # other versions they are the only route. Always pointed at the fixed port: if this instance
        # lost it to another Lantern, the menu items reach that one instead.
        try:
            import sumatra_settings
            changed, note = sumatra_settings.ensure_selection_handlers(
                bus.PREFERRED_PORT)
            log.info("selection handlers: %s", note)
        except Exception:
            log.exception("couldn't install selection handlers")
        if self.port and self.port != bus.PREFERRED_PORT:
            self.engine.history.append({
                "role": "meta",
                "text": "another Lantern owns port "
                        f"{bus.PREFERRED_PORT} — reader right-click actions "
                        "will land in that window's chat"})
        hwnd = self.reader_host.winfo_id()
        try:
            self.sumatra_proc = subprocess.Popen(
                [SUMATRA, "-plugin", str(hwnd), self.pdf_path]
            )
        except OSError as e:
            self.chat_meta(f"[error] couldn't launch SumatraPDF: {e}")
            return
        self.find_sumatra_child(hwnd, tries=0)

    def _auto_ingest(self, path):
        """First open of a book: build its store now. Returns (store, note);
        store is None when ingestion declined (scanned PDF) or failed."""
        import bookstore
        import ingest
        self.root.title(f"Lantern — indexing {self.book_name} …")
        self.root.update()
        t0 = time.time()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                book_dir = ingest.ingest(path)
        except Exception as e:
            log.exception("auto-ingest failed")
            for line in out.getvalue().splitlines():
                log.info("ingest: %s", line)
            return None, f"couldn't index the book ({e.__class__.__name__}: {e})"
        for line in out.getvalue().splitlines():
            log.info("ingest: %s", line)
        self.root.title(f"Lantern — {self.book_name}")
        if book_dir is None:
            return None, ("this PDF looks scanned (no text layer) — run OCR "
                          "on it first, e.g. ocrmypdf, then reopen")
        store = bookstore.BookStore.open(book_dir)
        dt = time.time() - t0
        n_sections = len(store.toc)
        if store.meta.get("original_path"):
            note = (f"converted the {store.meta['original_format'].upper()} "
                    f"to a fixed-layout PDF ({store.n_pages} pages) and "
                    f"indexed it in {dt:.1f}s — {n_sections} sections. "
                    "Highlights and annotations go into that PDF (in the "
                    "book store), the original file is untouched")
        else:
            note = (f"indexed the book in {dt:.1f}s — {n_sections} sections, "
                    f"{store.n_pages} pages")
        log.info("auto-ingest: %s", note)
        return store, note

    def find_sumatra_child(self, parent_hwnd, tries):
        child, tid = find_child_of_pid(parent_hwnd, self.sumatra_proc.pid)
        if child:
            self.sumatra_hwnd, self.sumatra_tid = child, tid
            log.info("sumatra child hwnd=%#x tid=%d after %d tries",
                     child, tid, tries)
            self.resize_sumatra()
            if self.input is not None:
                self.start_focus_retry()
            threading.Thread(target=self._page_poll, daemon=True,
                             name="sumatra-page-poll").start()
            self.start_selection_hotkey()
        elif tries < 100:
            self.root.after(100, self.find_sumatra_child, parent_hwnd, tries + 1)
        else:
            self.chat_meta("[error] SumatraPDF window never appeared (10s).")

    def _read_ctrl_text(self, hwnd):
        """WM_GETTEXT with a short timeout — never blocks on a busy Sumatra."""
        buf = ctypes.create_unicode_buffer(64)
        res = ctypes.c_size_t()
        ok = user32.SendMessageTimeoutW(
            hwnd, WM_GETTEXT, 63, ctypes.cast(buf, ctypes.c_void_p).value,
            SMTO_ABORTIFHUNG, 100, ctypes.byref(res))
        return buf.value.strip() if ok else None

    def _page_poll(self):
        """Daemon thread: read the live page number from Sumatra's toolbar
        page box (a plain Win32 Edit control — kept even in -plugin mode) and
        feed it to the context bus. Total pages comes from the ' / N' static."""
        import re
        last, total, edit_missing_logged = None, None, False
        # relaunch memory: drive back to where the reader left off (stored
        # by the bus once the page holds still). Attempted twice, then let
        # whatever page Sumatra shows stand.
        restore = self.store.meta.get("last_page") if self.store else None
        restore_tries = 0
        while True:
            time.sleep(0.5)
            hwnd = self.sumatra_hwnd
            if not hwnd or not user32.IsWindow(hwnd):
                continue
            edit = find_descendant_class(hwnd, "Edit")
            if not edit:
                if not edit_missing_logged:
                    log.warning("page poll: no Edit control in Sumatra toolbar")
                    edit_missing_logged = True
                continue
            text = self._read_ctrl_text(edit)
            if not text or not text.isdigit():
                continue
            page = int(text)
            if restore is not None:
                if page == restore or restore_tries >= 2:
                    restore = None            # arrived (or gave up)
                else:
                    restore_tries += 1
                    log.info("restoring last page %d (attempt %d)",
                             restore, restore_tries)
                    self.goto_page(restore)
                    continue                  # don't report transit pages
            if total is None:
                for static in self._sumatra_statics(hwnd):
                    m = re.search(r"(?:/|of)\s*(\d+)", static)
                    if m:
                        total = int(m.group(1))
                        log.info("page poll: total pages = %d", total)
                        break
            if page != last:
                last = page
                log.info("page poll: page %s%s", page,
                         f"/{total}" if total else "")
                try:
                    self.engine.set_page(page, total)
                except Exception:
                    pass

    def reload_reader(self):
        """Sumatra's own Reload (CmdReloadDocument: same page, zoom and
        scroll), so a just-written underline shows up with its hover note
        and Ctrl+click. Thread-safe: one posted message."""
        hwnd = self.sumatra_hwnd
        if not (hwnd and user32.IsWindow(hwnd) and self._reload_cmd):
            log.info("reload: skipped (hwnd=%s cmd=%s)", hwnd, self._reload_cmd)
            return
        user32.PostMessageW(hwnd, WM_COMMAND, self._reload_cmd, 0)
        log.info("reload: WM_COMMAND %d -> Sumatra", self._reload_cmd)

    def goto_page(self, page):
        """Drive the reader to a PDF page: type it into Sumatra's toolbar
        page box and press Enter (the box's WndProc handles VK_RETURN as
        goto). Same proven Edit-control handle the page poll reads; DDE is
        not needed. Thread-safe: only posts/sends window messages."""
        hwnd = self.sumatra_hwnd
        if not hwnd or not user32.IsWindow(hwnd):
            return
        edit = find_descendant_class(hwnd, "Edit")
        if not edit:
            log.warning("goto_page: no Edit control in Sumatra toolbar")
            return
        buf = ctypes.create_unicode_buffer(str(int(page)))
        res = ctypes.c_size_t()
        ok = user32.SendMessageTimeoutW(
            edit, WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p).value,
            SMTO_ABORTIFHUNG, 200, ctypes.byref(res))
        if not ok:
            log.warning("goto_page: WM_SETTEXT timed out")
            return
        user32.PostMessageW(edit, WM_KEYDOWN, VK_RETURN, 0)
        user32.PostMessageW(edit, WM_KEYUP, VK_RETURN, 0xC0000001)
        log.info("goto_page -> %s", page)

    def _sumatra_statics(self, root_hwnd):
        texts = []

        def cb(hwnd, lparam):
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buf, 64)
            if buf.value == "Static":
                t = self._read_ctrl_text(hwnd)
                if t:
                    texts.append(t)
            return True

        user32.EnumChildWindows(root_hwnd, WNDENUMPROC(cb), 0)
        return texts

    # ---------- send-selection-to-chat hotkey ----------

    def start_selection_hotkey(self):
        ver = None
        try:
            ver = file_version(SUMATRA)
        except Exception:
            log.exception("couldn't read Sumatra's version")
        mm = ".".join((ver or "").split(".")[:2])
        self._reload_cmd = SUMATRA_RELOAD_CMD.get(mm)
        self._copy_cmd = SUMATRA_COPY_CMD.get(mm)
        if not self._copy_cmd:
            log.warning("hotkey: no CmdCopySelection id for Sumatra %s — "
                        "%s and the Lantern right-click menu disabled",
                        ver, SELECTION_HOTKEY_LABEL)
            try:
                self.engine._note_threadsafe(
                    f"{SELECTION_HOTKEY_LABEL} unavailable: unknown "
                    f"SumatraPDF version {ver} — use the reader's own "
                    "right-click menu (Lantern entries at the bottom)")
            except Exception:               # bus never came up
                pass
            return
        self._root_hwnd = user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
        self._hook_proc = HOOKPROC(self._ll_keyboard)
        self._mouse_proc = HOOKPROC(self._ll_mouse)
        threading.Thread(target=self._hotkey_thread, daemon=True,
                         name="selection-hotkey").start()
        log.info("hotkey: %s -> WM_COMMAND %d (Sumatra %s); right-click "
                 "menu replaced", SELECTION_HOTKEY_LABEL, self._copy_cmd, ver)

    def _hotkey_thread(self):
        """Owns the WH_KEYBOARD_LL + WH_MOUSE_LL hooks: they need a pumping
        message loop on the installing thread, and tk's loop is not ours to
        hook into."""
        self._hook_tid = kernel32.GetCurrentThreadId()
        hooks = []
        for kind, proc in ((WH_KEYBOARD_LL, self._hook_proc),
                           (WH_MOUSE_LL, self._mouse_proc)):
            hook = user32.SetWindowsHookExW(kind, proc, None, 0)
            if hook:
                hooks.append(hook)
            else:
                log.warning("hook %d: SetWindowsHookEx failed (err %d)",
                            kind, ctypes.get_last_error())
        if not hooks:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        for hook in hooks:
            user32.UnhookWindowsHookEx(hook)

    # ---------- right-click menu (replaces Sumatra's context menu) ----------

    def _ll_mouse(self, code, wparam, lparam):
        # Hook thread, every mouse event system-wide: decide fast. A right
        # click on Sumatra's canvas inside our window never reaches Sumatra
        # (no "Translate with Bing"); the menu is ours, shown on button-up
        # unless the mouse moved (a right-drag), Windows-style.
        if code == 0 and wparam in (WM_RBUTTONDOWN, WM_RBUTTONUP):
            pt = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents.pt
            if wparam == WM_RBUTTONDOWN:
                if self._on_canvas(pt, menu_ok=True):
                    self._rdown = (pt.x, pt.y)
                    if self._menu is not None:
                        # Our menu is up and the click is on the canvas:
                        # close the menu ourselves and let the button-up
                        # below re-probe at the new spot. Passing the click
                        # on (so the menu loop dismisses on it) is what let
                        # Sumatra's own menu through: a down that landed
                        # while tk was still building the menu started a
                        # Sumatra right-drag, and its up showed its menu.
                        self._cancel_menu()
                    return 1
                self._rdown = None
            elif self._rdown is not None:
                x0, y0 = self._rdown
                self._rdown = None
                if abs(pt.x - x0) <= 6 and abs(pt.y - y0) <= 6:
                    self._click_gen += 1
                    threading.Thread(target=self._context_probe,
                                     args=(pt.x, pt.y, self._click_gen),
                                     daemon=True).start()
                return 1
        # Ctrl+left on a Lantern underline: open the chat behind it. Only
        # swallowed when the note under the cursor is ours, so Sumatra's
        # Ctrl+drag (rectangle selection) elsewhere is untouched.
        elif code == 0 and wparam in (WM_LBUTTONDOWN, WM_LBUTTONUP):
            pt = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents.pt
            if wparam == WM_LBUTTONDOWN:
                self._ldown = None
                if (key_down(VK_CONTROL) and not key_down(VK_SHIFT)
                        and not key_down(VK_MENU) and self._on_canvas(pt)):
                    self._ldown = self._annotation_tip(pt)
                    if self._ldown:
                        return 1
            elif self._ldown is not None:
                note, self._ldown = self._ldown, None
                threading.Thread(target=self._open_annotation, args=(note,),
                                 daemon=True).start()
                return 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_canvas(self, pt, menu_ok=False):
        if (self._menu is not None and not menu_ok) or not self.sumatra_hwnd:
            return False
        hwnd = user32.WindowFromPoint(pt)
        return bool(hwnd) and (user32.GetAncestor(hwnd, GA_ROOT)
                               == self._root_hwnd) \
            and (window_class(hwnd) == SUMATRA_CANVAS_CLASS
                 or is_notify_window(hwnd))

    def _cancel_menu(self):
        """Hook thread: end the tk thread's popup menu. WM_CANCELMODE to a
        window makes DefWindowProc abandon its menu modal loop, so post it
        to every top-level window of the tk thread (the menu's owner is
        tk's hidden menu window, not our root)."""
        tid = user32.GetWindowThreadProcessId(self._root_hwnd, None)

        def cb(hwnd, lparam):
            user32.PostMessageW(hwnd, WM_CANCELMODE, 0, 0)
            return True

        user32.EnumThreadWindows(tid, WNDENUMPROC(cb), 0)

    # ---------- Ctrl+click on an annotation -> its chat ----------

    def _annotation_tip(self, pt):
        """The Lantern note under the cursor, or None. Sumatra hit-tests for
        us: whenever the mouse rests on a page element it registers that
        element's text (an annotation's popup note) as the single tool of
        its infotip window (src/Canvas.cpp OnSetCursorMouseIdle ->
        TooltipCtrl::ShowOrUpdate, TTF_SUBCLASS on the canvas, uId 0) —
        before the tip is even visible. TTM_GETTEXT reads it back; the
        TOOLINFO has to live in Sumatra's address space, hence the remote
        alloc. Hook thread: bounded by SendMessageTimeout."""
        canvas = user32.WindowFromPoint(pt)
        if not canvas or window_class(canvas) != SUMATRA_CANVAS_CLASS:
            return None
        t0 = time.monotonic()
        try:
            for tip in self._tooltip_windows():
                text = self._tooltip_text(tip, canvas)
                if text and text.startswith("Lantern"):
                    log.info("ctrl+click: note %r (%.0fms)", text[:80],
                             (time.monotonic() - t0) * 1000)
                    return text
        except Exception:
            log.exception("annotation tip read failed")
        return None

    def _tooltip_windows(self):
        hits = []

        def cb(hwnd, lparam):
            if window_class(hwnd) == TOOLTIPS_CLASS:
                hits.append(hwnd)
            return True

        if self.sumatra_tid:
            user32.EnumThreadWindows(self.sumatra_tid, WNDENUMPROC(cb), 0)
        return hits

    @staticmethod
    def _tooltip_text(tip, tool_hwnd):
        """Text of the tool that tooltip window `tip` holds for `tool_hwnd`,
        or None. TTM_ENUMTOOLS (rather than TTM_GETTEXT) because the id the
        control files the tool under isn't ours to guess (uId 1 on 3.4.6,
        not the 0 Sumatra passes); it hands back the control's own copy of
        the text, which ReadProcessMemory then pulls across."""
        res = ctypes.c_size_t()
        if not user32.SendMessageTimeoutW(tip, TTM_GETTOOLCOUNT, 0, 0,
                                          SMTO_ABORTIFHUNG, 60,
                                          ctypes.byref(res)) or not res.value:
            return None
        n_tools = min(res.value, 4)     # the infotip holds one; toolbars many
        pid, _ = hwnd_pid_tid(tip)
        proc = kernel32.OpenProcess(PROCESS_VM_OPERATION | PROCESS_VM_READ
                                    | PROCESS_VM_WRITE, False, pid)
        if not proc:
            log.warning("tooltip: OpenProcess(%d) failed (err %d)", pid,
                        ctypes.get_last_error())
            return None
        remote = None
        try:
            ti_size = ctypes.sizeof(TOOLINFOW)
            # TOOLINFO followed by the text buffer. TTM_GETTEXT (comctl32
            # v6) honours the char count we pass, but the buffer is sized
            # for an unbounded copy anyway: an overrun here would fault
            # Sumatra's UI thread, not ours.
            remote = kernel32.VirtualAllocEx(proc, None,
                                             ti_size + TIP_BUFFER_BYTES,
                                             MEM_COMMIT_RESERVE, PAGE_READWRITE)
            if not remote:
                return None
            ti = TOOLINFOW()
            for i in range(n_tools):
                # pass 1: which window / id is tool i registered for?
                # (lpszText NULL: the control fills nothing in for it)
                ctypes.memset(ctypes.byref(ti), 0, ti_size)
                ti.cbSize = ti_size
                if not kernel32.WriteProcessMemory(proc, remote,
                                                   ctypes.byref(ti), ti_size,
                                                   None):
                    return None
                if not user32.SendMessageTimeoutW(tip, TTM_ENUMTOOLSW, i,
                                                  remote, SMTO_ABORTIFHUNG, 80,
                                                  ctypes.byref(res)):
                    log.info("tooltip: TTM_ENUMTOOLS timed out")
                    return None
                if not kernel32.ReadProcessMemory(proc, remote,
                                                  ctypes.byref(ti), ti_size,
                                                  None):
                    return None
                if ti.hwnd != tool_hwnd or ti.lpszText == LPSTR_TEXTCALLBACK:
                    continue
                # pass 2: its text, copied into our remote buffer
                ti.lpszText = remote + ti_size
                if not kernel32.WriteProcessMemory(proc, remote,
                                                   ctypes.byref(ti), ti_size,
                                                   None):
                    return None
                if not user32.SendMessageTimeoutW(tip, TTM_GETTEXTW,
                                                  TIP_READ_CHARS - 1, remote,
                                                  SMTO_ABORTIFHUNG, 80,
                                                  ctypes.byref(res)):
                    log.info("tooltip: TTM_GETTEXT timed out")
                    return None
                out = ctypes.create_unicode_buffer(TIP_READ_CHARS)
                if not kernel32.ReadProcessMemory(proc, remote + ti_size, out,
                                                  TIP_READ_CHARS * 2, None):
                    return None
                return out.value or None
            return None
        finally:
            if remote:
                kernel32.VirtualFreeEx(proc, remote, 0, MEM_RELEASE)
            kernel32.CloseHandle(proc)

    def _open_annotation(self, note):
        """Worker: hand the note to the bus, which resolves its chat ref."""
        if self.input is not None:     # tk fallback chat has no sessions UI
            self.root.after(0, self._tk_append, "meta", "note", note)
            return
        self.engine.open_annotation(note)

    def _context_probe(self, x, y, gen):
        """Worker: is there a selection? Sumatra's copy command tells us —
        the clipboard changes when there is, its "Select content…" toast
        appears when there isn't (hidden before it paints, see
        _grab_selection). Then the menu, on the tk thread. Probes run one
        at a time and only the newest click's menu is shown, so a burst of
        right-clicks ends with one menu at the last spot, not a queue."""
        with self._probe_lock:
            if gen != self._click_gen:
                return
            t0 = time.monotonic()
            text = self._grab_selection(timeout=0.35, quiet=True)
            log.info("menu: %s after %.0fms",
                     f"selection of {len(text)} chars" if text else "no selection",
                     (time.monotonic() - t0) * 1000)
        self.root.after(0, self._show_context_menu, x, y, text, gen)

    def _show_context_menu(self, x, y, text, gen):
        if self._menu is not None or gen != self._click_gen:
            return
        # The one home for verbs: passage verbs on the selection under the
        # cursor, then cascades for the page / section / chapter around it.
        m = tk.Menu(self.root, tearoff=0)
        loc = self.engine.location()
        if text:
            preview = text.replace("\n", " ")
            if len(preview) > MENU_PREVIEW_CHARS:
                preview = preview[:MENU_PREVIEW_CHARS - 1].rstrip() + "…"
            m.add_command(label=f"“{preview}”", state="disabled")
            import bookcontext
            for action, label in bookcontext.SELECTION_VERBS:
                acc = SELECTION_HOTKEY_LABEL if action == "copy" else None
                m.add_command(label=label, accelerator=acc,
                              command=lambda a=action: self._menu_selection(a, text))
            m.add_separator()
            m.add_command(label="Copy text",
                          command=lambda: set_clipboard_text(text))
            if self.store and loc.get("page"):
                m.add_separator()
        scopes = self._scope_menus(m, loc)
        if not text and not scopes:
            hint = ("Select text (drag over it), then right-click"
                    if self.store else
                    "Select text, then right-click — page/section/chapter "
                    "actions need an indexed book")
            m.add_command(label=hint, state="disabled")
        self._menu = m
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()
            self._menu = None

    def _scope_menus(self, menu, loc):
        """Cascades for the page / section / chapter the reader is on."""
        import bookcontext
        entries = []
        if loc.get("page") and self.store:
            pr = f" (book p. {loc['printed']})" if loc.get("printed") else ""
            entries.append(("page", f"This page — PDF p. {loc['page']}{pr}"))
            if loc.get("section"):
                entries.append(("section", "Section " + loc["section"]["title"]))
            if loc.get("chapter"):
                entries.append(("chapter", "Chapter " + loc["chapter"]["title"]))
        for scope, label in entries:
            sub = tk.Menu(menu, tearoff=0)
            for verb, vlabel, scopes in bookcontext.SCOPE_VERBS:
                if scope in scopes:
                    sub.add_command(
                        label=vlabel,
                        command=lambda v=verb, s=scope: self._menu_scope(v, s))
            menu.add_cascade(label=label, menu=sub)
        return entries

    def _menu_selection(self, action, text):
        log.info("menu: %s (%d chars)", action, len(text))
        if action == "copy" and self.input is not None:
            self._tk_compose(text)
        else:
            self.engine.send_selection(action, text)

    def _menu_scope(self, verb, scope):
        log.info("menu: %s on %s", verb, scope)
        self.engine.send_scope(verb, scope)

    def _grab_selection(self, timeout=0.8, quiet=False):
        """Make Sumatra copy its selection (its own Ctrl+C command) and
        return the reflowed text; None when nothing is selected. With
        quiet=True the "Select content with Ctrl+left mouse button" toast
        Sumatra shows for an empty selection is hidden as soon as its window
        exists — before Sumatra's thread gets to paint it."""
        hwnd = self.sumatra_hwnd
        if not (hwnd and user32.IsWindow(hwnd) and self._copy_cmd):
            return None
        seen = set()
        if quiet:
            seen = set(self._notify_windows(hwnd))
        seq0 = user32.GetClipboardSequenceNumber()
        user32.PostMessageW(hwnd, WM_COMMAND, self._copy_cmd, 0)
        deadline = time.monotonic() + timeout
        while user32.GetClipboardSequenceNumber() == seq0:
            if quiet:
                new = [n for n in self._notify_windows(hwnd) if n not in seen]
                if new:
                    for n in new:
                        user32.ShowWindowAsync(n, SW_HIDE)
                    return None
            if time.monotonic() > deadline:
                log.info("selection: clipboard unchanged — nothing selected?")
                return None
            time.sleep(0.002 if quiet else 0.02)
        time.sleep(0.05)               # Sumatra also puts a bitmap on it
        text = clipboard_text()
        if text is None:
            log.warning("selection: clipboard stayed locked")
            return None
        text = bus.reflow_selection(text)
        if not text:
            log.info("selection had no text (image only?)")
        return text or None

    @staticmethod
    def _notify_windows(root_hwnd):
        hits = []

        def cb(hwnd, lparam):
            if is_notify_window(hwnd):
                hits.append(hwnd)
            return True

        user32.EnumChildWindows(root_hwnd, WNDENUMPROC(cb), 0)
        return hits

    def _ll_keyboard(self, code, wparam, lparam):
        # Runs on the hook thread for every keystroke system-wide: decide
        # fast, do the work elsewhere. Swallow only our chord, only while
        # this window (Sumatra or the chat inside it) is in the foreground.
        if code == 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if (kb.vkCode == SELECTION_HOTKEY_VK
                    and key_down(VK_CONTROL) and key_down(VK_SHIFT)
                    and not (key_down(VK_MENU) or key_down(VK_LWIN)
                             or key_down(VK_RWIN))
                    and self._foreground_is_ours()):
                now = time.monotonic()
                if now - self._hotkey_last > 0.4:      # key auto-repeat
                    self._hotkey_last = now
                    threading.Thread(target=self._selection_to_chat,
                                     daemon=True).start()
                return 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _foreground_is_ours(self):
        fg = user32.GetForegroundWindow()
        return bool(fg) and user32.GetAncestor(fg, GA_ROOT) == self._root_hwnd

    def _selection_to_chat(self):
        """Worker: make Sumatra copy its selection (its own Ctrl+C command),
        then hand the clipboard text to the chat like "Copy to chat" does."""
        # Nothing selected: Sumatra's own "Select content with Ctrl+left
        # mouse button" hint shows in the reader and we do nothing.
        text = self._grab_selection()
        if not text:
            return
        log.info("hotkey: %d chars -> chat", len(text))
        if self.input is not None:     # tk fallback chat: no SSE to receive it
            self.root.after(0, self._tk_compose, text)
        else:
            self.engine.send_selection("copy", text)

    def _tk_compose(self, text):
        quoted = "\n".join("> " + ln for ln in text.splitlines())
        base = self.input.get("1.0", "end").strip()
        self.input.delete("1.0", "end")
        self.input.insert("end", (base + "\n\n" if base else "") + quoted
                          + "\n\n")
        self.input.see("end")
        self.input.focus_set()

    def on_reader_resize(self, event=None):
        # Debounced trailing edge: a drag storm becomes one final resize.
        if self._reader_job:
            self.root.after_cancel(self._reader_job)
        self._reader_job = self.root.after(40, self._do_reader_resize)

    def _do_reader_resize(self):
        self._reader_job = None
        self.resize_sumatra()

    def resize_sumatra(self):
        if not self.sumatra_hwnd:
            return
        w = self.reader_host.winfo_width()
        h = self.reader_host.winfo_height()
        if (w, h) == self._reader_size:
            return
        self._reader_size = (w, h)
        t0 = time.perf_counter()
        async_move(self.sumatra_hwnd, w, h)
        dt = (time.perf_counter() - t0) * 1000
        if dt > 20:
            log.warning("resize_sumatra SetWindowPos took %.0fms", dt)

    # ---------- edge chat pane ----------

    def launch_edge(self, profile=EDGE_PROFILE):
        exe = next((p for p in EDGE_CANDIDATES if os.path.isfile(p)), None)
        if not exe or not self.port:
            self.chat_meta("[meta] Edge not found — using plain-text chat.")
            self._fallback_to_tk_chat()
            return
        os.makedirs(profile, exist_ok=True)
        self._edge_profile = profile
        # Explicit size overrides Edge's remembered (possibly maximized)
        # app-window bounds, which would otherwise fight our geometry.
        w = max(self.chat_host.winfo_width(), 200)
        h = max(self.chat_host.winfo_height(), 200)
        # Edge keeps a heuristically cached chat.html per URL across
        # restarts (UI changes silently didn't show); a version query keyed
        # on the file's mtime makes every edit a fresh URL.
        try:
            ver = int(os.path.getmtime(bus.CHAT_HTML))
        except OSError:
            ver = 0
        try:
            self.edge_proc = subprocess.Popen([
                exe, f"--app=http://127.0.0.1:{self.port}/?v={ver}",
                f"--user-data-dir={profile}",
                f"--window-size={w},{h}", "--window-position=0,0",
                "--no-first-run", "--no-default-browser-check",
            ])
        except OSError as e:
            self.chat_meta(f"[error] couldn't launch Edge: {e}")
            self._fallback_to_tk_chat()
            return
        self.find_edge_window(tries=0)

    def find_edge_window(self, tries):
        if self.edge_proc.poll() is not None:
            # Edge handed off to an existing browser process — the profile is
            # in use by another Lantern instance. Retry once on a spare one.
            if self._edge_profile == EDGE_PROFILE:
                log.info("edge delegated (profile busy); retrying with spare")
                self.launch_edge(profile=EDGE_PROFILE + "-b")
            else:
                log.warning("edge exited immediately; falling back to tk chat")
                self._fallback_to_tk_chat()
            return
        hwnd = find_toplevel_of_pid(self.edge_proc.pid, "Chrome_WidgetWin_1")
        if hwnd:
            self.embed_edge(hwnd)
        elif tries < 60:                      # 15s
            self.root.after(250, self.find_edge_window, tries + 1)
        else:
            log.warning("edge window never appeared; falling back to tk chat")
            self._fallback_to_tk_chat()

    def embed_edge(self, hwnd):
        self.edge_hwnd = hwnd
        style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        style = (style & ~(WS_CAPTION | WS_THICKFRAME | WS_POPUP | WS_SYSMENU
                           | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)) | WS_CHILD
        user32.SetWindowLongW(hwnd, GWL_STYLE, ctypes.c_long(style & 0xFFFFFFFF))
        user32.SetParent(hwnd, self.chat_host.winfo_id())
        self._chat_size = None
        self.resize_edge(force_frame=True)
        log.info("edge embedded hwnd=%#x", hwnd)
        # Edge app windows draw their own title bar (with min/max/close) inside
        # the client area; measure its height off the render-host child and
        # shift the window up so the bar is clipped away by the pane.
        self.root.after(600, self._measure_edge_chrome, 0)
        # Chrome can change after the WS_CHILD conversion settles — re-check.
        for delay in (2000, 5000, 10000):
            self.root.after(delay, self._measure_edge_chrome, 20)
        self.root.after(800, self._request_edge_focus)
        self._focus_pump()

    def _measure_edge_chrome(self, tries):
        if not self.edge_hwnd:
            return
        rh = find_descendant_class(self.edge_hwnd, "Chrome_RenderWidgetHostHWND")
        if not rh:
            if tries < 20:
                self.root.after(300, self._measure_edge_chrome, tries + 1)
            return
        wr, cr = wintypes.RECT(), wintypes.RECT()
        user32.GetWindowRect(self.edge_hwnd, ctypes.byref(wr))
        user32.GetWindowRect(rh, ctypes.byref(cr))
        off = (max(0, cr.left - wr.left), max(0, cr.top - wr.top),
               max(0, wr.right - cr.right), max(0, wr.bottom - cr.bottom))
        if off != self._edge_off:
            self._edge_off = off
            log.info("edge chrome offsets l,t,r,b=%s", off)
            self.resize_edge(force_frame=True)

    def on_chat_resize(self, event=None):
        if self._chat_job:
            self.root.after_cancel(self._chat_job)
        self._chat_job = self.root.after(40, self._do_chat_resize)

    def _do_chat_resize(self):
        self._chat_job = None
        if self.edge_hwnd:
            self._measure_edge_chrome(20)   # no retries; no-op if unchanged
        self.resize_edge()

    def resize_edge(self, force_frame=False):
        if not self.edge_hwnd:
            return
        w = self.chat_host.winfo_width()
        h = self.chat_host.winfo_height()
        key = (w, h, self._edge_off)
        if not force_frame and key == self._chat_size:
            return
        self._chat_size = key
        left, top, right, bottom = self._edge_off
        async_move(self.edge_hwnd, w + left + right, h + top + bottom,
                   x=-left, y=-top,
                   extra_flags=SWP_FRAMECHANGED if force_frame else 0)

    # The web page can't move win32 keyboard focus to a cross-process child on
    # its own (Chromium never sees a proper activation), so chat.html POSTs
    # /focus on clicks and the tk thread hands focus over explicitly.
    def _request_edge_focus(self):
        self._focus_wanted += 1

    def _focus_pump(self):
        if self.edge_hwnd:
            if user32.IsIconic(self.edge_hwnd) or user32.IsZoomed(self.edge_hwnd):
                log.info("edge iconic/zoomed — restoring")
                user32.ShowWindowAsync(self.edge_hwnd, SW_RESTORE)
                # Restore is async and reapplies Edge's remembered bounds;
                # reassert our geometry after it has actually happened.
                self.root.after(250, lambda: self.resize_edge(force_frame=True))
            self._pump_tick += 1
            if self._pump_tick % 40 == 0:     # every ~2s: self-heal drift
                self._verify_edge_geometry()
            if self._focus_wanted != self._focus_handled:
                self._focus_handled = self._focus_wanted
                self._focus_edge()
        self.root.after(50, self._focus_pump)

    def _verify_edge_geometry(self):
        r = wintypes.RECT()
        if not user32.GetWindowRect(self.edge_hwnd, ctypes.byref(r)):
            return
        left, top, right, bottom = self._edge_off
        want = (self.chat_host.winfo_width() + left + right,
                self.chat_host.winfo_height() + top + bottom)
        got = (r.right - r.left, r.bottom - r.top)
        if got != want:
            log.info("edge geometry drifted %s vs %s — reasserting", got, want)
            self.resize_edge(force_frame=True)

    def _focus_edge(self):
        target = (find_descendant_class(self.edge_hwnd,
                                        "Chrome_RenderWidgetHostHWND")
                  or self.edge_hwnd)
        _, target_tid = hwnd_pid_tid(target)
        tk_tid = kernel32.GetCurrentThreadId()
        user32.SetFocus(target)
        if user32.GetFocus() != target:
            user32.AttachThreadInput(tk_tid, target_tid, True)
            user32.SetFocus(target)
            user32.AttachThreadInput(tk_tid, target_tid, False)
        if self.diag:
            log.info("focus_edge target=%#x now=%#x", target,
                     user32.GetFocus() or 0)

    def _fallback_to_tk_chat(self):
        if self.input is None:
            self.tkchat = True
            self.build_tk_chat()

    # ---------- tk fallback chat (no Edge): plain text, no streaming ----------

    def build_tk_chat(self):
        self.history = tk.Text(
            self.chat_host, wrap="word", state="disabled", bd=0, bg="white",
            fg="#27272a", font=("Segoe UI", 10), padx=12, pady=10,
            spacing1=2, spacing3=8,
        )
        scroll = tk.Scrollbar(self.chat_host, command=self.history.yview)
        self.history.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.history.pack(fill="both", expand=True)
        self.history.tag_configure("you", foreground=ACCENT,
                                   font=("Segoe UI Semibold", 10))
        self.history.tag_configure("claude", foreground="#18181b",
                                   font=("Segoe UI Semibold", 10))
        self.history.tag_configure("meta", foreground="#a1a1aa",
                                   font=("Segoe UI", 9, "italic"))

        input_row = tk.Frame(self.sidebar, bg=BG)
        input_row.pack(fill="x", padx=10, pady=(0, 4))
        self.input = tk.Text(
            input_row, height=3, wrap="word", bd=1, relief="solid",
            font=("Segoe UI", 10), padx=8, pady=6,
            highlightthickness=1, highlightcolor=ACCENT,
        )
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Return>", self.on_enter)
        self.input.bind("<Shift-Return>", lambda e: None)
        self.send_btn = tk.Button(
            input_row, text="▶", command=self.send, bd=0, bg=ACCENT,
            fg="white", activebackground="#5b21b6", activeforeground="white",
            width=3, cursor="hand2",
        )
        self.send_btn.pack(side="left", fill="y", padx=(6, 0))

        self.status = tk.Label(
            self.sidebar, text="ready", bg=BG, fg="#a1a1aa",
            font=("Segoe UI", 8), anchor="w",
        )
        self.status.pack(fill="x", padx=12, pady=(0, 6))

        if self.diag:
            self.input.bind("<Button-1>",
                            lambda e: log.info("input <Button-1>"), add=True)
            self.input.bind("<FocusIn>",
                            lambda e: log.info("input <FocusIn>"), add=True)

        self.chat_meta(f"Reading: {self.book_name}\n"
                       "Plain-text chat (Edge pane unavailable). Learning "
                       "controls (dials, quiz ratings, review card) need "
                       "the Edge pane; dials stay at their saved values.")
        if getattr(self, "_bus_error", None):
            self.chat_meta(f"[error] context bus: {self._bus_error}")
        self.root.after(300, self._poll_engine)
        self.input.focus_set()

    def on_enter(self, event):
        self.send()
        return "break"

    def send(self):
        msg = self.input.get("1.0", "end").strip()
        if not msg:
            return
        self.input.delete("1.0", "end")
        self.engine.ask(msg)

    def _poll_engine(self):
        """Mirror engine history into the tk chat (fallback mode only)."""
        hist = self.engine.history
        while self._history_seen < len(hist):
            entry = hist[self._history_seen]
            self._history_seen += 1
            if entry["role"] == "you":
                self._tk_append("you", "You", entry["text"])
            elif entry["role"] == "claude":
                self._tk_append("claude", "Claude", entry["text"])
            else:
                self.chat_meta(entry["text"])
        self.status.configure(text="thinking…" if self.engine.busy
                              else "ready")
        self.send_btn.configure(state="disabled" if self.engine.busy
                                else "normal")
        self.root.after(300, self._poll_engine)

    def _tk_append(self, tag, label, text):
        self.history.configure(state="normal")
        self.history.insert("end", f"{label}\n", tag)
        self.history.insert("end", f"{text}\n\n")
        self.history.configure(state="disabled")
        self.history.see("end")

    def chat_meta(self, text):
        log.info("meta: %s", text)
        if self.input is not None:
            self.history.configure(state="normal")
            self.history.insert("end", f"{text}\n\n", "meta")
            self.history.configure(state="disabled")
            self.history.see("end")

    # ---------- focus retry (plan step 5, tk chat only) ----------

    def start_focus_retry(self):
        self._focus_deadline = time.monotonic() + 15
        self._user_clicked = False
        self.root.bind_all("<Button-1>", self._note_user_click, add=True)
        self._focus_retry()

    def _note_user_click(self, event):
        self._user_clicked = True

    def _focus_retry(self):
        if self._user_clicked or time.monotonic() > self._focus_deadline:
            return
        if self.input is None or user32.GetFocus() == self.input.winfo_id():
            return
        self.input.focus_force()
        self.root.after(500, self._focus_retry)

    # ---------- diagnostics (--diag) ----------

    def start_diagnostics(self):
        self._hb_last = time.perf_counter()
        self._focus_last = (None, None)
        self.root.after(50, self._heartbeat)
        self.root.after(250, self._focus_watch)
        threading.Thread(target=self._busy_probe, daemon=True,
                         name="sumatra-busy-probe").start()
        log.info("diagnostics on -> %s", DIAG_LOG)

    def _heartbeat(self):
        now = time.perf_counter()
        gap = now - self._hb_last
        if gap > 0.2:
            log.warning("mainloop stall %.0fms", gap * 1000)
        self._hb_last = now
        self.root.after(50, self._heartbeat)

    def _focus_watch(self):
        f, fg = user32.GetFocus(), user32.GetForegroundWindow()
        if (f, fg) != self._focus_last:
            self._focus_last = (f, fg)
            fp = hwnd_pid_tid(f)[0] if f else 0
            fgp = hwnd_pid_tid(fg)[0] if fg else 0
            note = ""
            if self.sumatra_proc and fp == self.sumatra_proc.pid:
                note = " <-- foreign GetFocus: input queues ATTACHED (H1)"
            log.info("focus hwnd=%#x pid=%d | fg hwnd=%#x pid=%d%s",
                     f or 0, fp, fg or 0, fgp, note)
        self.root.after(250, self._focus_watch)

    def _busy_probe(self):
        responsive = None
        res = ctypes.c_size_t()
        while True:
            time.sleep(0.5)
            hwnd = self.sumatra_hwnd
            if not hwnd:
                continue
            ok = user32.SendMessageTimeoutW(hwnd, WM_NULL, 0, 0,
                                            SMTO_ABORTIFHUNG, 100,
                                            ctypes.byref(res))
            state = bool(ok)
            if state != responsive:
                responsive = state
                log.info("sumatra %s", "responsive" if state
                         else "UNRESPONSIVE (busy UI thread)")

    # ---------- shutdown ----------

    def go_library(self):
        """Close this book and drop back to the library (main() reopens it)."""
        self.back_to_library = True
        self.on_close()

    def on_close(self):
        if self._hook_tid:
            user32.PostThreadMessageW(self._hook_tid, WM_QUIT, 0, 0)
        for proc in (self.sumatra_proc, self.edge_proc):
            if proc and proc.poll() is None:
                proc.terminate()
        try:
            self.engine.shutdown()
        except Exception:
            pass
        self.root.destroy()


def main():
    args = sys.argv[1:]
    diag = "--diag" in args
    tkchat = "--tkchat" in args
    args = [a for a in args if a not in ("--diag", "--tkchat")]
    selftest_msg = None
    if "--selftest" in args:
        i = args.index("--selftest")
        selftest_msg = args[i + 1]
        args = args[:i] + args[i + 2:]

    branding.set_app_id()
    handlers = [logging.FileHandler(DIAG_LOG, encoding="utf-8")]
    if kernel32.GetConsoleWindow():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("=== Lantern start (diag=%s tkchat=%s) ===", diag, tkchat)

    exit_code = [0]
    while True:
        if args:
            pdf, args = args[0], []          # next loop -> library
        else:
            import library
            pdf = library.choose_book()
            if pdf is None:
                log.info("library closed without a choice")
                break
            log.info("library choice: %s", pdf)
        if not os.path.isfile(pdf):
            ctypes.windll.user32.MessageBoxW(0, f"PDF not found:\n{pdf}",
                                             "Lantern", 0x10)
            exit_code[0] = 1
            break

        root = tk.Tk()
        branding.apply_icon(root)
        shell = LanternShell(root, pdf, diag=diag, tkchat=tkchat,
                               greet=selftest_msg is None)

        if selftest_msg:
            def run_selftest():
                log.info("selftest: asking %r", selftest_msg)
                shell._selftest_t0 = time.monotonic()
                shell._selftest_base = len(shell.engine.history)
                shell.engine.ask(selftest_msg)
                root.after(500, check_selftest)

            def check_selftest():
                hist = shell.engine.history
                done = [h for h in hist[shell._selftest_base:]
                        if h["role"] in ("claude", "meta")]
                if done and not shell.engine.busy:
                    elapsed = time.monotonic() - shell._selftest_t0
                    ok = done[-1]["role"] == "claude"
                    log.info("selftest %s in %.1fs: %.200s",
                             "OK" if ok else "FAIL", elapsed, done[-1]["text"])
                    exit_code[0] = 0 if ok else 1
                    root.after(1500, shell.on_close)
                elif time.monotonic() - shell._selftest_t0 > 320:
                    log.error("selftest TIMEOUT")
                    exit_code[0] = 1
                    shell.on_close()
                else:
                    root.after(500, check_selftest)

            root.after(3000, run_selftest)

        root.mainloop()
        if not shell.back_to_library:
            break
        log.info("back to library")
    sys.exit(exit_code[0])


if __name__ == "__main__":
    main()
