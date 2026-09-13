"""Per-book chat sessions: one JSON file per session in books/<id>/sessions/.

A record is the unit the bus keeps in memory:
  {id, title, chapter, style, challenge, quiz, run, nudges,
   sdk_session_id, created, updated, history}
history holds the sidebar transcript; only you/claude entries are persisted
(meta notices are ephemeral). The Claude-side conversation itself lives with
the CLI and is re-attached via sdk_session_id -> resume, so a session's
model memory and its visible transcript travel together across app runs.

Learning dials live on the record: `style` (explanation register — the old
`mode`; standard | plain | simple) and `challenge` (0..3). `quiz` holds the
pending quiz question, if any; `run` the multi-question protocol in progress
(bus.quiz_run: kind, total, asked, graded, wrong …), if any; `nudges` is the
unsolicited-offer budget. The
`intent` dial (understand | retain) was dropped 2026-09-13 and is ignored.

The ⚙ settings a user last had in front of them (dials + model) also live in
books/prefs.json, shared across books: every new chat — the "+" button, a
task chat, or the fresh chat each book open starts — begins from those
rather than from DEFAULT_DIALS.
"""

import json
import os
import re
import time

DIRNAME = "sessions"
PERSIST_ROLES = ("you", "claude")
PREFS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "books", "prefs.json")

DEFAULT_DIALS = {"style": "standard", "challenge": 1}
# pre-2026-09-13 style ids -> today's
LEGACY_STYLES = {"eli5": "simple", "eli7": "simple", "socratic": "standard",
                 "feynman": "standard"}


def _fresh_nudges():
    return {"last_t": 0, "last_page": 0, "count": 0}


def new_record(chapter=None, sdk_session_id=None, title=None, dials=None):
    """dials: {style?, challenge?} overriding DEFAULT_DIALS (already
    validated by the caller — see bus.ChatEngine._pref_dials)."""
    now = int(time.time())
    return {"id": f"s-{now}-{os.urandom(3).hex()}", "title": title,
            "chapter": chapter, **DEFAULT_DIALS, **(dials or {}),
            "quiz": None, "run": None, "nudges": _fresh_nudges(),
            "sdk_session_id": sdk_session_id, "created": now,
            "updated": now, "history": []}


def is_blank(rec):
    """No user turn yet — a chat that was opened but never used."""
    return not any(h.get("role") == "you" for h in rec.get("history", []))


def load_prefs(path=PREFS_FILE):
    """Last-used ⚙ settings ({style, challenge, model}), or {}."""
    try:
        with open(path, encoding="utf-8") as f:
            p = json.load(f)
        return p if isinstance(p, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_prefs(prefs, path=PREFS_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=1)
    os.replace(tmp, path)


def normalize(rec):
    """Bring an on-disk record (possibly from an older version) up to the
    current shape. Mutates and returns it."""
    rec.setdefault("history", [])
    if "style" not in rec:
        rec["style"] = rec.pop("mode", DEFAULT_DIALS["style"])
    else:
        rec.pop("mode", None)
    rec["style"] = LEGACY_STYLES.get(rec["style"], rec["style"])
    rec.pop("intent", None)
    for k, v in DEFAULT_DIALS.items():
        rec.setdefault(k, v)
    if not isinstance(rec.get("challenge"), int):
        rec["challenge"] = DEFAULT_DIALS["challenge"]
    rec.setdefault("quiz", None)
    if not isinstance(rec.get("run"), dict):
        rec["run"] = None
    if not isinstance(rec.get("nudges"), dict):
        rec["nudges"] = _fresh_nudges()
    return rec


def load_all(book_dir):
    """All session records, oldest first. Unreadable files are skipped."""
    d = os.path.join(book_dir, DIRNAME)
    recs = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    rec = json.load(f)
                recs.append(normalize(rec))
            except (OSError, json.JSONDecodeError):
                continue
    recs.sort(key=lambda r: r.get("updated", 0))
    return recs


def save(book_dir, rec, touch=True):
    """Write a record. touch=True stamps `updated` (real activity); pass
    False for bookkeeping saves so recency ordering isn't disturbed."""
    if touch:
        rec["updated"] = int(time.time())
    on_disk = dict(rec, history=[h for h in rec["history"]
                                 if h.get("role") in PERSIST_ROLES])
    d = os.path.join(book_dir, DIRNAME)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, rec["id"] + ".json"), "w",
              encoding="utf-8") as f:
        json.dump(on_disk, f, ensure_ascii=False, indent=1)


def dials(rec):
    return {"style": rec.get("style", "standard"),
            "challenge": rec.get("challenge", 1)}


def label(rec):
    """Dropdown text: title (or placeholder) + starting chapter, compactly."""
    t = rec.get("title") or "New chat"
    m = re.match(r"(\d+)", rec.get("chapter") or "")
    return f"{t} · ch.{m.group(1)}" if m else t
