"""Per-book learning record: typed learning events + spaced-review state.

Two files in the book store:
  learning.jsonl  append-only raw events (source of truth; one JSON per line)
  learning.json   derived state — concepts with a review schedule, exercises
                  with hint history — rewritten whole, rebuildable from the
                  jsonl with `rebuild()`.

Events come from Claude as a hidden trailer on the last line of a reply:
  <!--rb {"ev":"quiz","concept":"Bellman equation","sec":"03.07","result":"partial"}-->
`parse_trailer` strips and parses it after the turn; `TrailerFilter` strips
it from the live delta stream so it never flashes in the sidebar.

Scheduling is successive relearning on a fixed ladder (1/3/7/14/30 days):
a concept advances one box per correct recall on a new day, drops to box 0
on a miss, and counts as mastered after 3 correct recalls once it has
survived a week-long gap. A miss given with high confidence is flagged
`priority` (hypercorrection: those are the most valuable to revisit).

stdlib only; every disk write is executor-friendly and guarded by a per-book
lock. CLI: python app\\learning.py show|export|rebuild <book_dir> [--out f]
[--all]
"""

import hashlib
import json
import logging
import os
import re
import threading
import time

from bookstore import norm

log = logging.getLogger("lantern.learning")

EVENTS_FILE = "learning.jsonl"
STATE_FILE = "learning.json"
VERSION = 1

LADDER_DAYS = [1, 3, 7, 14, 30]
MASTER_STREAK = 3          # correct recalls on distinct days …
MASTER_MIN_BOX = 3         # … having reached the 14-day box
DAY = 86400

EVENT_KINDS = ("quiz", "ask", "pretest", "hint", "explain", "misconception")
RESULTS = ("correct", "partial", "wrong")
CONFIDENCES = ("guess", "sure", "certain")
GUESSES = ("correct", "partial", "wrong", "none")
STAGES = ("numbers", "words", "analogy", "symbols")

TRAILER_RE = re.compile(r"<!--\s*rb\b\s*(\{.*?\}|\[.*?\])\s*-->", re.S)
TRAILER_HEAD = re.compile(r"<!--\s*rb\b")

_locks = {}
_locks_guard = threading.Lock()


def _lock(book_dir):
    key = os.path.normcase(os.path.abspath(book_dir))
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


# ---------- files ----------

def events_path(book_dir):
    return os.path.join(book_dir, EVENTS_FILE)


def state_path(book_dir):
    return os.path.join(book_dir, STATE_FILE)


def empty_state():
    return {"version": VERSION, "updated": 0, "snoozed_until": 0,
            "concepts": {}, "exercises": {}}


def load(book_dir):
    """Derived state; an empty state if the file is missing or unreadable."""
    try:
        with open(state_path(book_dir), encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict) or "concepts" not in state:
            raise ValueError("not a learning state")
        state.setdefault("exercises", {})
        state.setdefault("snoozed_until", 0)
        return state
    except FileNotFoundError:
        return empty_state()
    except (OSError, ValueError) as e:
        log.warning("learning.json unreadable (%s) — starting empty", e)
        return empty_state()


def _save_state(book_dir, state):
    state["updated"] = int(time.time())
    tmp = state_path(book_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, state_path(book_dir))


def _append_events(book_dir, lines):
    with open(events_path(book_dir), "a", encoding="utf-8") as f:
        for ev in lines:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


# ---------- trailer parsing ----------

def concept_id(label):
    return norm(label or "").replace(" ", "-") or None


def _loads_tolerant(raw):
    """json.loads with a repair pass for the usual LLM slips."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    fixed = re.sub(r",\s*([}\]])", r"\1", raw)          # trailing commas
    if '"' not in fixed:
        fixed = fixed.replace("'", '"')                  # single quotes
    fixed = re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', fixed)  # bare keys
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        return None


def parse_trailer(text):
    """(clean_text, events, warnings). Strips every rb trailer wherever it
    sits; never raises. Events are raw dicts (normalised later by record)."""
    events, warnings = [], []
    if not text:
        return text, events, warnings

    def _take(m):
        data = _loads_tolerant(m.group(1))
        if data is None:
            warnings.append(f"unparseable trailer: {m.group(1)[:120]}")
        else:
            items = data if isinstance(data, list) else [data]
            for it in items:
                if isinstance(it, dict) and it.get("ev") in EVENT_KINDS:
                    events.append(it)
                else:
                    warnings.append(f"unknown event: {json.dumps(it)[:120]}")
        return ""

    clean = TRAILER_RE.sub(_take, text)
    return clean.rstrip(), events, warnings


def _proper_prefix_len(s, pat):
    """Length of the longest proper prefix of `pat` that `s` ends with."""
    for k in range(min(len(pat) - 1, len(s)), 0, -1):
        if s.endswith(pat[:k]):
            return k
    return 0


class TrailerFilter:
    """Streaming strip of rb trailers: feed() returns the text safe to show,
    holding back anything that might still turn into a trailer."""

    def __init__(self):
        self._buf = ""
        self.dropped = []

    def feed(self, chunk):
        self._buf += chunk
        out = []
        while True:
            i = self._buf.find("<!--")
            if i < 0:
                k = _proper_prefix_len(self._buf, "<!--")
                cut = len(self._buf) - k
                out.append(self._buf[:cut])
                self._buf = self._buf[cut:]
                break
            out.append(self._buf[:i])
            self._buf = self._buf[i:]
            j = self._buf.find("-->")
            if j < 0:
                break                           # comment still open: hold
            comment, self._buf = self._buf[:j + 3], self._buf[j + 3:]
            if TRAILER_RE.fullmatch(comment):
                self.dropped.append(comment)
            else:
                out.append(comment)             # some other HTML comment
        return "".join(out)

    def flush(self):
        rest, self._buf = self._buf, ""
        if TRAILER_HEAD.match(rest):
            log.warning("unterminated rb trailer dropped: %s", rest[:80])
            self.dropped.append(rest)
            return ""
        return rest


# ---------- scheduler (pure) ----------

def _new_concept(label, sec, now):
    return {"label": label, "sec": sec, "first_seen": now, "last_seen": now,
            "box": 0, "streak": 0, "next_review": now + LADDER_DAYS[0] * DAY,
            "last_advance_day": None, "mastery": "new", "priority": False,
            "attempts": [], "pretests": [], "explains": [], "misconceptions": []}


def apply_result(concept, result, conf, now):
    """One graded recall. Mutates and returns `concept`."""
    day = now // DAY
    box = concept.get("box", 0)
    if result == "correct":
        if concept.get("last_advance_day") != day:
            box = min(box + 1, len(LADDER_DAYS) - 1)
            concept["streak"] = concept.get("streak", 0) + 1
            concept["last_advance_day"] = day
        concept["box"] = box
        concept["next_review"] = now + LADDER_DAYS[box] * DAY
        concept["priority"] = False
    elif result == "partial":
        concept["next_review"] = now + LADDER_DAYS[box] * DAY
    elif result == "wrong":
        concept["box"] = 0
        concept["streak"] = 0
        concept["next_review"] = now + LADDER_DAYS[0] * DAY
        if conf == "certain":
            concept["priority"] = True
    concept["mastery"] = mastery_of(concept)
    return concept


def mastery_of(concept):
    attempts = concept.get("attempts", [])
    if (concept.get("streak", 0) >= MASTER_STREAK
            and concept.get("box", 0) >= MASTER_MIN_BOX):
        return "mastered"
    if concept.get("priority"):
        return "shaky"
    recent = attempts[-2:]
    if any(a.get("result") in ("wrong", "partial") for a in recent):
        return "shaky"
    last_ok = max((a["t"] for a in attempts if a.get("result") == "correct"),
                  default=0)
    if any(m["t"] > last_ok for m in concept.get("misconceptions", [])):
        return "shaky"
    return "learning" if attempts else "new"


def due(state, now=None, limit=8):
    """Concepts due for review, priority first then most overdue, then
    interleaved across chapters so a session mixes material."""
    now = now or time.time()
    items = []
    for cid, c in state.get("concepts", {}).items():
        if c.get("next_review", 0) <= now:
            attempts = c.get("attempts", [])
            items.append({
                "cid": cid, "concept": c["label"], "sec": c.get("sec"),
                "mastery": c.get("mastery", "new"),
                "priority": bool(c.get("priority")),
                "misses": sum(1 for a in attempts if a.get("result") == "wrong"),
                "last": (max((a["t"] for a in attempts), default=None)
                         or c.get("last_seen")),
                "overdue": now - c.get("next_review", now),
            })
    items.sort(key=lambda i: (not i["priority"], -i["overdue"]))
    return interleave(items[:limit])


def interleave(items):
    """Round-robin over chapters (sec prefix), preserving order within one."""
    buckets, order = {}, []
    for it in items:
        key = (it.get("sec") or "")[:2]
        if key not in buckets:
            order.append(key)
        buckets.setdefault(key, []).append(it)
    out = []
    while any(buckets.values()):
        for key in order:
            if buckets[key]:
                out.append(buckets[key].pop(0))
    return out


def digest(state, now=None, limit=10):
    """One bracketed line for the per-turn tail, or "" when there is nothing
    worth telling Claude yet."""
    now = now or time.time()
    concepts = state.get("concepts", {})
    if not concepts:
        return ""
    mastered = [c["label"] for c in concepts.values()
                if c.get("mastery") == "mastered"]
    shaky = []
    for c in concepts.values():
        if c.get("mastery") == "shaky":
            tag = " (missed w/ high confidence)" if c.get("priority") else ""
            shaky.append(c["label"] + tag)
    n_due = sum(1 for c in concepts.values()
                if c.get("next_review", 0) <= now)
    bits = []
    if mastered:
        bits.append("mastered — " + ", ".join(sorted(mastered)[:limit]))
    if shaky:
        bits.append("shaky — " + ", ".join(sorted(shaky)[:limit]))
    if n_due:
        bits.append(f"due for review: {n_due}")
    return "[learning digest: " + "; ".join(bits) + "]" if bits else ""


# ---------- recording ----------

def _normalise(ev, ctx, now):
    """Raw trailer dict -> clean event line, or None (with a reason)."""
    kind = ev.get("ev")
    if kind not in EVENT_KINDS:
        return None, f"unknown ev {kind!r}"
    out = {"t": now, "sid": ctx.get("sid"), "page": ctx.get("page"), "ev": kind}
    sec = str(ev.get("sec") or "").strip()
    toc_ids = ctx.get("toc_ids")
    if not sec or (toc_ids and sec not in toc_ids):
        sec = ctx.get("sec_fallback")
    out["sec"] = sec
    if kind == "hint":
        # hints are keyed by the passage the bus sent for hinting (ctx),
        # never by an exercise number Claude reports — books number (or
        # don't number) exercises however they like
        pending = ctx.get("hint") or {}
        if not pending.get("key"):
            return None, f"hint with no passage pending: {ev}"
        out["exercise"] = pending["key"]
        label = str(ev.get("label") or ev.get("exercise") or "").strip()
        out["label"] = (label or pending.get("label") or "")[:80]
        if pending.get("sec") and not ev.get("sec"):
            out["sec"] = pending["sec"]
        try:
            out["level"] = max(1, min(3, int(ev.get("level") or 1)))
        except (TypeError, ValueError):
            out["level"] = 1
        if ev.get("solved") is not None:
            out["solved"] = bool(ev.get("solved"))
        if ev.get("solution"):
            out["solution"] = True
        return out, None
    label = " ".join(str(ev.get("concept") or "").split())[:80]
    cid = concept_id(label)
    if not cid:
        return None, f"{kind} without concept: {ev}"
    out["concept"], out["cid"] = label, cid
    if ev.get("note"):
        out["note"] = str(ev["note"])[:300]
    if kind == "quiz":
        result = str(ev.get("result") or "").lower()
        if result not in RESULTS:
            return None, f"quiz with bad result {result!r}"
        conf = ctx.get("conf")
        out["result"] = result
        out["conf"] = conf if conf in CONFIDENCES else None
        out["hc"] = result == "wrong" and out["conf"] == "certain"
        out["q"] = str(ev.get("q") or ctx.get("q") or "")[:20] or None
        if ctx.get("placement"):        # graded inside a test-out, not practice
            out["placement"] = True
    elif kind == "ask":
        out["q"] = str(ev.get("q") or "q")[:20]
        out["pretest"] = bool(ev.get("pretest"))
    elif kind == "pretest":
        guess = str(ev.get("guess") or "none").lower()
        out["guess"] = guess if guess in GUESSES else "none"
    elif kind == "explain":
        stage = str(ev.get("stage") or "").lower()
        out["stage"] = stage if stage in STAGES else None
    return out, None


def _concept(state, ev, now):
    c = state["concepts"].get(ev["cid"])
    if c is None:
        c = state["concepts"][ev["cid"]] = _new_concept(ev["concept"],
                                                        ev.get("sec"), now)
    c["last_seen"] = now
    if ev.get("sec") and not c.get("sec"):
        c["sec"] = ev["sec"]
    return c


def _apply_event(state, ev):
    now = ev["t"]
    kind = ev["ev"]
    if kind == "hint":
        x = state["exercises"].setdefault(
            ev["exercise"], {"sec": ev.get("sec"), "label": "", "hints": [],
                             "attempts": [], "solution_shown": None})
        if ev.get("label"):
            x["label"] = ev["label"]
        x["hints"].append({"t": now, "level": ev["level"], "sid": ev.get("sid")})
        if "solved" in ev:
            x["attempts"].append({"t": now, "solved": ev["solved"]})
        if ev.get("solution"):
            x["solution_shown"] = now
        return
    c = _concept(state, ev, now)
    if kind == "quiz":
        c["attempts"].append({"t": now,
                              "kind": "placement" if ev.get("placement") else "quiz",
                              "result": ev["result"],
                              "conf": ev.get("conf"), "hc": bool(ev.get("hc")),
                              "sid": ev.get("sid"), "q": ev.get("q"),
                              "note": ev.get("note")})
        apply_result(c, ev["result"], ev.get("conf"), now)
    elif kind == "pretest":
        c["pretests"].append({"t": now, "guess": ev["guess"], "sid": ev.get("sid")})
    elif kind == "explain":
        c["explains"].append({"t": now, "stage": ev.get("stage")})
    elif kind == "misconception":
        c["misconceptions"].append({"t": now, "note": ev.get("note")})
    c["mastery"] = mastery_of(c)


def record(book_dir, events, ctx=None, now=None):
    """Normalise + persist a turn's trailer events. ctx: {sid, page,
    sec_fallback, conf, q, toc_ids}. Returns the normalised events."""
    ctx = ctx or {}
    now = int(now or time.time())
    lines = []
    for ev in events:
        line, why = _normalise(ev, ctx, now)
        if line is None:
            log.warning("dropped learning event: %s", why)
        else:
            lines.append(line)
    if not lines:
        return []
    with _lock(book_dir):
        state = load(book_dir)
        for line in lines:
            _apply_event(state, line)
        _append_events(book_dir, lines)
        _save_state(book_dir, state)
    return lines


def rebuild(book_dir):
    """Replay learning.jsonl into a fresh learning.json."""
    with _lock(book_dir):
        state = empty_state()
        try:
            with open(events_path(book_dir), encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        _apply_event(state, json.loads(ln))
                    except (ValueError, KeyError) as e:
                        log.warning("skipping bad event line: %s", e)
        except FileNotFoundError:
            pass
        _save_state(book_dir, state)
    return state


def snooze(book_dir, hours=4):
    with _lock(book_dir):
        state = load(book_dir)
        state["snoozed_until"] = int(time.time() + hours * 3600)
        _save_state(book_dir, state)


# ---------- exercises ----------

def passage_key(sec, text):
    """Stable id for a selected passage: its section id plus a short hash of
    the normalised opening. Hint history is tracked per passage, so no
    exercise numbering scheme is assumed of the book."""
    head = " ".join((text or "").casefold().split())[:120]
    h = hashlib.sha1(head.encode("utf-8")).hexdigest()[:10]
    return f"{sec or '??'}:{h}"


def hint_level(state, passage_key):
    x = state.get("exercises", {}).get(passage_key) or {}
    return min(3, len(x.get("hints", [])) + 1)


# ---------- export ----------

def export_anki(book_dir, out_path, include_mastered=False, title=""):
    """TSV (front, back, tags) importable by Anki. Returns rows written."""
    state = load(book_dir)
    rows = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for cid, c in sorted(state["concepts"].items()):
            if c.get("mastery") == "mastered" and not include_mastered:
                continue
            where = f" (§{c['sec']}" + (f", {title}" if title else "") + ")" \
                if c.get("sec") else ""
            front = f"{c['label']} — explain or define{where}"
            notes = [a.get("note") for a in c.get("attempts", []) if a.get("note")]
            notes += [m.get("note") for m in c.get("misconceptions", []) if m.get("note")]
            back = notes[-1] if notes else f"Explain {c['label']} in your own words."
            tags = " ".join(["lantern", f"sec-{(c.get('sec') or 'x').replace('.', '-')}",
                             f"mastery-{c.get('mastery', 'new')}"])
            f.write("\t".join(x.replace("\t", " ").replace("\n", " ")
                              for x in (front, back, tags)) + "\n")
            rows += 1
    return rows


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["show", "export", "rebuild"])
    ap.add_argument("book_dir")
    ap.add_argument("--out", default=None)
    ap.add_argument("--all", action="store_true", help="export mastered too")
    a = ap.parse_args(argv)
    if a.cmd == "rebuild":
        st = rebuild(a.book_dir)
        print(f"rebuilt: {len(st['concepts'])} concepts, "
              f"{len(st['exercises'])} exercises")
    elif a.cmd == "export":
        out = a.out or os.path.join(a.book_dir, "anki.tsv")
        n = export_anki(a.book_dir, out, include_mastered=a.all)
        print(f"wrote {n} cards -> {out}")
    else:
        st = load(a.book_dir)
        print(digest(st) or "(no digest yet)")
        for it in due(st):
            print(f"  due: {it['concept']} §{it['sec']} [{it['mastery']}]"
                  + (" PRIORITY" if it["priority"] else ""))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
