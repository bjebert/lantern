"""App identity: name, window icon, taskbar grouping.

The icon is the Lantern relic from Slay the Spire (app/lantern.ico, with
app/lantern.png as the source render). Both windows (shell + library) call
apply_icon(); main() calls set_app_id() once so Windows groups the taskbar
button under "Lantern" with our icon rather than pythonw's.
"""
import ctypes
import os

APP_NAME = "Lantern"
APP_ID = "blake.lantern"
_HERE = os.path.dirname(os.path.abspath(__file__))
ICON_ICO = os.path.join(_HERE, "lantern.ico")
ICON_PNG = os.path.join(_HERE, "lantern.png")


def set_app_id():
    """Give the process its own AppUserModelID (before any window exists)."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def apply_icon(root):
    """Set the window/taskbar icon on a tk root; silently keeps tk's default
    if the icon files are missing."""
    try:
        if os.path.isfile(ICON_ICO):
            root.iconbitmap(default=ICON_ICO)
            return
    except Exception:
        pass
    try:
        if os.path.isfile(ICON_PNG):
            import tkinter as tk
            img = tk.PhotoImage(file=ICON_PNG)
            root.iconphoto(True, img)
            root._lantern_icon = img          # keep a ref; tk won't
    except Exception:
        pass
