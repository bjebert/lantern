"""Install Lantern's right-click actions into SumatraPDF's settings.

SumatraPDF's SelectionHandlers setting adds custom entries to the context
menu shown when text is selected (verified: also in -plugin embed mode —
OnWindowContextMenu has no plugin check). Each entry we write carries both
transports:

- URL + ${selection}: what the stable releases (3.4 – 3.6.1) use — opens
  the default browser with the selection URL-encoded into the query (the
  bus answers with a page that closes itself). These versions force http(s)
  and drop the fields below when they rewrite the file on exit; we
  re-install on every launch.
- Method = POST + Body with ${selectionjson}: prerelease builds only
  (checked src/Settings.h at 3.6rel / 3.6.1rel: no Method/Body there) —
  Sumatra itself POSTs the JSON to the bus (no browser at all) and shows
  the bus's short reply as an in-canvas notification.
- Key = Ctrl+Shift+C on the "Copy to chat" entry: 3.6's per-handler
  shortcut, which would take the browser route above. On every known
  version the shell's own keyboard hook swallows the chord first
  (lantern_shell.SUMATRA_COPY_CMD), so this only matters on a version the
  shell doesn't know.

The bus listens on a fixed port (bus.PREFERRED_PORT) precisely so these
static entries can find it. Standalone-check:
    python sumatra_settings.py            # dry run against the real file
    python sumatra_settings.py --apply
"""

import os
import re
import shutil

COPY_KEY = "Ctrl+Shift+C"
SETTINGS = os.path.expandvars(r"%LOCALAPPDATA%\SumatraPDF\SumatraPDF-settings.txt")
BOM = "﻿"

# (action, context-menu label) — action doubles as the bus /sel dispatch key.
# "copy" is also bound to Key = COPY_KEY (3.6+). These only show on Sumatra
# versions where the shell can't replace the context menu (see
# lantern_shell.SUMATRA_COPY_CMD); on 3.4 – 3.6 our own menu takes over.
from bookcontext import SELECTION_VERBS as ACTIONS  # noqa: E402


def _entries(port, nl):
    out = []
    for action, label in ACTIONS:
        out.append(nl.join([
            "\t[",
            f"\t\tURL = http://127.0.0.1:{port}/sel?a={action}&t=${{selection}}",
            f"\t\tName = {label}",
            "\t\tMethod = POST",
            f'\t\tBody = {{"a": "{action}", "text": "${{selectionjson}}"}}',
            "\t\tContentType = application/json",
        ] + ([f"\t\tKey = {COPY_KEY}"] if action == "copy" else []) + [
            "\t]",
        ]) + nl)
    return "".join(out)


def _is_ours(sub_block_lines):
    joined = "".join(sub_block_lines)
    return "127.0.0.1" in joined and "/sel" in joined


def ensure_selection_handlers(port, settings_path=SETTINGS):
    """Idempotently (re)write our handlers. Returns (changed, note).

    Foreign SelectionHandlers entries are preserved; previous Lantern
    entries (recognized by the 127.0.0.1/sel URL) are replaced, so a port
    change or field upgrade heals on the next launch.
    """
    if not os.path.isfile(settings_path):
        # Fresh Sumatra that has never run: give it a seed file — it merges
        # its defaults around unknown-but-valid settings on first save.
        text, nl, bom = "", "\r\n", BOM
    else:
        with open(settings_path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        bom = BOM if text.startswith(BOM) else ""
        text = text[len(bom):]
        nl = "\r\n" if "\r\n" in text else "\n"

    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines)
                  if re.match(r"^SelectionHandlers\s*\[\s*$", ln)), None)

    ours = _entries(port, nl)
    if start is None:
        body = text.rstrip("\r\n")
        new = (body + (nl * 2 if body else "")
               + "SelectionHandlers [" + nl + ours + "]" + nl)
    else:
        depth, end = 1, None
        for j in range(start + 1, len(lines)):
            s = lines[j].strip()
            if s.endswith("["):
                depth += 1
            elif s == "]":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            return False, "unbalanced SelectionHandlers block — left untouched"

        block, kept, k = lines[start + 1:end], [], 0
        while k < len(block):
            s = block[k].strip()
            if s == "[":
                d, m = 1, k + 1
                while m < len(block) and d:
                    t = block[m].strip()
                    if t.endswith("["):
                        d += 1
                    elif t == "]":
                        d -= 1
                    m += 1
                if not _is_ours(block[k:m]):
                    kept.extend(block[k:m])
                k = m
            else:
                if s:                        # stray non-entry line: keep it
                    kept.append(block[k])
                k += 1

        new_block = "".join(kept) + ours
        if "".join(block) == new_block:
            return False, "already installed"
        new = "".join(lines[:start + 1]) + new_block + "".join(lines[end:])

    if os.path.isfile(settings_path):
        bak = settings_path + ".lantern-bak"
        legacy = settings_path + ".readbuddy-bak"   # pre-rename backup
        if os.path.isfile(legacy) and not os.path.isfile(bak):
            os.replace(legacy, bak)
        if not os.path.isfile(bak):
            shutil.copyfile(settings_path, bak)
    with open(settings_path, "w", encoding="utf-8", newline="") as f:
        f.write(bom + new)
    return True, f"installed {len(ACTIONS)} handlers -> port {port}"


if __name__ == "__main__":
    import sys
    import bus
    if "--apply" in sys.argv:
        print(ensure_selection_handlers(bus.PREFERRED_PORT))
    else:
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), "rb-settings-dryrun.txt")
        if os.path.isfile(SETTINGS):
            shutil.copyfile(SETTINGS, tmp)
        elif os.path.isfile(tmp):
            os.remove(tmp)
        print(ensure_selection_handlers(bus.PREFERRED_PORT, tmp))
        print(f"dry run -> {tmp}")
