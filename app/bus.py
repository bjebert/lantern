"""Lantern context bus: local HTTP server + one persistent Claude session.

Runs an asyncio loop on a daemon thread. Serves the chat UI (chat.html),
accepts questions over HTTP, streams reply text out over SSE, and keeps a
single ClaudeSDKClient subprocess alive for the whole app run (no per-message
claude -p cold start). Falls back to spawning `claude -p` per message if the
claude-agent-sdk package is unavailable.

Learning mode (2026-09-08): per-session dials (style / challenge)
ride the per-turn tail; Claude reports learning outcomes in a hidden
<!--rb …--> trailer that is stripped from the stream and recorded in
learning.jsonl/json (app/learning.py); a spaced-review card is offered at
session start; budgeted nudges fire at chapter boundaries.

Standalone check (no GUI): python bus.py --test "your question"
"""

import asyncio
import dataclasses
import json
import logging
import math
import os
import re
import subprocess
import threading
import time

log = logging.getLogger("lantern.bus")

try:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False

try:
    from aiohttp import web
    HAVE_AIOHTTP = True
except ImportError:
    HAVE_AIOHTTP = False

import bookcontext
import bookstore
import journal
import learning
import pdftools
import sessions
import summaries

CLAUDE = os.path.expanduser(r"~\.local\bin\claude.exe")
CHAT_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat.html")
CREATE_NO_WINDOW = 0x08000000
TURN_TIMEOUT = 300
# Fixed port so SumatraPDF's static SelectionHandlers URLs can find the bus
# (sumatra_settings.py writes them). Falls back to an ephemeral port if taken.
PREFERRED_PORT = 8378
SELECTION_MAX_CHARS = 20000
CHECKIN_DELAY = 4          # s a new section must stay on screen before the
                           # chapter-boundary check-in fires (skim guard)
NOTES_DELAY = 1.5          # s of page stability before the page-notes line
PAGE_SAVE_DELAY = 5        # s of page stability before last_page persists

# (label, model id) — /state hands this to chat.html for the dropdown.
MODELS = [
    ("Sonnet 5", "sonnet"),
    ("Haiku 4.5", "haiku"),
    ("Fable 5", "claude-fable-5"),
]
DEFAULT_MODEL = "sonnet"
DEFAULT_EFFORT = "medium"

# ---------- learning dials ----------
# Three per-session settings. The RULES for what they mean sit in the cached
# system prompt (bookcontext.LEARNING_RULES); only the selection plus short
# reminders ride the volatile per-turn tail, so switching costs nothing
# cache-wise.

STYLES = [
    ("standard", "Standard", None),
    # Plain (2026-09-13): the step between Standard and Simple. Simple's
    # mandatory analogy + term mapping was producing two paragraphs; Plain
    # keeps the book's vocabulary but caps length, and Simple now has a
    # hard sentence budget.
    ("plain", "Plain",
     "[style: plain — everyday words, brief: the one-sentence gist first, "
     "then at most one short paragraph or 3-4 bullets (about 120 words). "
     "Keep the book's terms but gloss each in a few words when it first "
     "appears; no notation unless the user asks for the math.]"),
    ("simple", "Simple",
     "[style: simple — explain as to a bright ten-year-old in at most 5 "
     "short sentences (about 80 words): one everyday analogy, the book's "
     "term named once beside it, no other jargon, no math notation. Never "
     "define a word with harder words — pick a plainer word instead. Stop "
     "as soon as the point is made.]"),
]
STYLE_PROMPTS = {s: p for s, _, p in STYLES}
# Styles from before 2026-09-13 (ELI5/ELI7 → simple; Socratic → the
# Challenge dial; Explain-back → the "Check my explanation" scope verb).
LEGACY_STYLES = sessions.LEGACY_STYLES
# Chat ref at the end of a PDF annotation note (see _apply_annotation):
# session id + the turn's ordinal among that session's user turns.
CHAT_REF_RE = re.compile(r"\[chat:([^\]#\s]+)#(\d+)\]")
CHALLENGES = [(0, "Just tell me"), (1, "Offer a check"),
              (2, "Try first"), (3, "Make me work")]
CHALLENGE_REMINDERS = {
    2: "[reminder: challenge 2 — if this asks you to EXPLAIN a concept, ask "
       "for their guess first and stop (that guess question's ask trailer "
       "carries pretest:true; quiz questions never do); if this IS their "
       "guess, explain now. 'Just tell me' wins.]",
    3: "[reminder: challenge 3 — attempt first, then explanation + one "
       "application task; wait for it.]",
}
DIAL_OPTIONS = {
    "styles": [{"id": s, "label": lbl} for s, lbl, _ in STYLES],
    "challenges": [{"id": c, "label": lbl} for c, lbl in CHALLENGES],
}
CONFIDENCE_WORDS = {"guess": "a guess", "sure": "fairly sure",
                    "certain": "certain"}

# ---------- situational tails (protocol lines for one turn) ----------

VERB_TAILS = {
    "define": "[verb: define — a lookup; no pretest, no check question.]",
    "quiz": "[quiz protocol on this passage: 3 questions, one at a time.]",
    "formula": (
        "[verb: unpack formula — concreteness fading: (1) plug in small "
        "concrete numbers from a book example and compute one step; (2) "
        "restate the formula as one plain sentence; (3) an analogy with an "
        "explicit mapping table (formula part → analogy part); (4) back to "
        "the symbols, read aloud with meanings attached. Challenge 0-1: all "
        "four stages compactly in one reply. Challenge 2-3: give stage 1, "
        "then ask the user to attempt stage 2 themselves; one stage per turn. "
        "Skip stages 1-2 for concepts the digest marks mastered. Emit an "
        "explain trailer with the stage reached.]"),
}


def explainback_tail(where, explanation_given):
    """Feynman explain-back on a page/section: critique the user's own-words
    explanation against the book. Without an explanation in hand, the turn
    just invites one; the bus then attaches this tail to their next message."""
    if not explanation_given:
        return (f"[explain-back protocol: the user is about to explain {where} "
                "in their own words. Reply with ONE short line inviting them "
                "to go ahead (name the section), nothing else, and wait.]")
    return (f"[explain-back protocol: the user's message is their own-words "
            f"explanation of {where}. Check it against the book's actual text "
            "(Read the file if it isn't already in the conversation): affirm "
            "what's right, correct what's subtly off, name what's missing, "
            "then ask them to re-explain the weakest part. Critique, don't "
            "lecture. Emit a quiz trailer grading it correct/partial/wrong "
            "(concept = the section's main idea) and, for a real "
            "misunderstanding, a misconception trailer.]")

# Sumatra's text extraction hard-wraps at the PDF's visual line ends, so a
# quoted passage arrives as one short line per printed line. Reflow it back
# into paragraphs before it reaches the composer or a verb prompt.
_LIST_LINE = re.compile(r"^\s*(?:[-*\u2022\u25e6\u2013]|\(?\d{1,3}[.)]|[a-zA-Z][.)])\s+")


def reflow_selection(text):
    """Join wrapped lines into paragraphs. Blank lines still separate
    paragraphs; list-marker lines keep their own line; a trailing hyphen
    followed by a lowercase continuation is dehyphenated."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paras = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        out = []
        for ln in lines:
            if not out or _LIST_LINE.match(ln):
                out.append(ln)
                continue
            prev = out[-1]
            if (prev.endswith("-") and len(prev) > 1 and prev[-2].isalpha()
                    and ln[:1].islower()):
                out[-1] = prev[:-1] + ln
            else:
                out[-1] = prev + " " + ln
        paras.append("\n".join(out))
    return "\n\n".join(paras)


# A selection that looks like maths: the PDF's text layer loses tall
# brackets, fraction bars and the like (U+FFFD), and arrows, Greek and
# primes only ever come from formulas. Such copies are rebuilt as LaTeX by
# a haiku one-shot so the sidebar can render them (chat.html: KaTeX).
_MATHY_RE = re.compile(r"[\ufffd\u2190-\u21ff\u2200-\u22ff\u0391-\u03a9"
                       r"\u03b1-\u03c9\u2032\u2033\u00b1\u00d7\u00f7]")
_LATEX_RE = re.compile(r"\$\$(.+?)\$\$", re.S)


def looks_mathy(text):
    return bool(_MATHY_RE.search(text))


async def latexify(text):
    """Rebuild a formula copied from a PDF's text layer as one line of
    display LaTeX ($$…$$), or None when the call fails or the model
    decides it isn't a formula."""
    if not HAVE_SDK:
        return None
    try:
        out, _ = await one_shot(
            "This text was copied from a PDF's text layer, so glyphs may be "
            "lost or spaced oddly: U+FFFD (\ufffd) usually marks a tall "
            "bracket, parenthesis or fraction bar. Rewrite it as LaTeX. "
            "Reply with ONLY the LaTeX, on one line, wrapped in $$ … $$ — "
            "no prose. Keep any leading label or trailing punctuation out. "
            "If it is not a mathematical expression, reply with the single "
            f"word NONE.\n\n{text[:1500]}",
            "You convert PDF-extracted formulas to LaTeX.")
    except Exception:
        log.exception("latexify call failed")
        return None
    m = _LATEX_RE.search(out or "")
    if not m:
        log.info("latexify: no formula in reply: %r", (out or "")[:80])
        return None
    return "$$" + " ".join(m.group(1).split()) + "$$"


def hint_tail(file, level, label=None):
    src = f" (section file {file})" if file else ""
    known = f' — previously labelled "{label}"' if label else ""
    return (
        f"[hint protocol: the user selected a passage{src} and asked for a "
        "hint. First decide whether it is actually an exercise, problem or "
        "question to work on; Read around it in the section file if the "
        "selection alone doesn't settle that. If it is NOT — ordinary prose, "
        "a definition, a derivation — say so in one line, give a single "
        "conceptual nudge (what to look for or ask oneself), and emit NO hint "
        f"trailer. If it is: this is hint level {level} of 3 ({level - 1} "
        f"given before){known}. Level 1: name the relevant concept/section "
        "and the first question to ask oneself. Level 2: set up the first "
        "step concretely without carrying it through. Level 3: work through "
        "everything except the final result. Give exactly this level, nothing "
        "further. Full worked solution ONLY if the user explicitly asks "
        "('show the solution') — then emit hint with solution:true. If the "
        "user presents an answer, grade it and emit hint with "
        "solved:true/false. End with the hint trailer {label, sec, level}; "
        "label is a short name for the exercise (its number and title if the "
        "book gives one, else a few words).]")


# Multi-question protocols. The app, not the model, remembers how many
# questions were agreed and how far along the quiz is (a `run` on the session
# record, see quiz_run / ChatEngine._run_after_turn): the model's grading
# replies drift into the challenge dial's habits (a check question, a
# wrap-up) and used to end a quiz after q1.
QUIZ_COUNTS = {"selection": 3, "page": 2, "section": 3, "checkpoint": 4}
TESTOUT_COUNTS = {"section": 3, "chapter": 5}
TESTOUT_MAX_WRONG = 2      # a placement test stops early: the answer is "read it"
RUN_NAMES = {"quiz": "quiz", "checkpoint": "checkpoint quiz",
             "review": "review session", "testout": "test-out"}
STOP_RE = re.compile(
    r"^\s*(stop|quit|enough|end (the |this )?(quiz|test)|no more( questions)?|"
    r"just tell me|i'?m done|done)\b[.!]*\s*$", re.I)


def quiz_run(kind, total):
    """Fresh run bookkeeping for a quiz / checkpoint / review / test-out.
    correct / partial / wrong tally the grades; guessed counts the correct
    answers the user marked as a guess (a test-out treats those as partial
    for its pass rule)."""
    return {"kind": kind, "total": max(1, int(total)), "asked": 0,
            "graded": 0, "correct": 0, "partial": 0, "wrong": 0,
            "guessed": 0, "nudged": 0, "stop": False}


def run_tally(run):
    return (f"{run.get('correct', 0)} correct, {run.get('partial', 0)} "
            f"partial, {run['wrong']} wrong so far"
            + (f", {run['guessed']} of the correct ones guessed"
               if run.get("guessed") else ""))


def run_finished(run):
    return (run["graded"] >= run["total"] or bool(run.get("stop"))
            or (run["kind"] == "testout"
                and run["wrong"] >= TESTOUT_MAX_WRONG))


def quiz_answer_tail(quiz, run=None):
    conf = CONFIDENCE_WORDS.get(quiz.get("conf") or "", "not given")
    q = quiz.get("q") or "the question"
    base = (f"[quiz: this is the user's answer to {q} ({quiz.get('concept')}). "
            f"Stated confidence: {conf}. Grade it against the book text")
    if not run:
        return base + " and end with the quiz trailer.]"
    n, total = run["graded"] + 1, run["total"]
    name = RUN_NAMES.get(run["kind"], "quiz")
    nxt = (f"then, in this same reply, pose q{n + 1} of {total} — one "
           "question, numbered, ending with an ask trailer (the trailer line "
           "is then a JSON array: the quiz event, then the ask event)")
    if run["kind"] == "testout":
        fmt = ("grade word + [p.N] cite of where the answer lives, nothing "
               "else — no correction, no expected answer (those come in "
               "the answer key at the end)")
        last = ("this was the last question: after the grade word and cite, "
                "close the test-out with the four parts of the test-out "
                "protocol — the result line (count this answer; passed = no "
                "wrong and at most one partial, guessed-correct counts as "
                "partial), the answer key with one line per partial / wrong "
                "/ guessed question giving the expected answer and a [p.N] "
                "cite, the per-section verdict (skip / skim / read with the "
                "reason and the section's first page [p.N]; untested "
                "sections say so), then one focus line naming the section "
                "where the missed concept is defined — and pose nothing "
                "further")
        if run.get("stop") or n >= total:
            what = last
        elif run["wrong"] + 1 >= TESTOUT_MAX_WRONG:
            what = (f"if this answer is wrong it is miss number "
                    f"{TESTOUT_MAX_WRONG}, so {last}; otherwise {nxt}")
        else:
            what = nxt
    else:
        fmt = ("grade word first, then at most three lines with a [p.N] "
               "cite — for partial or wrong, the first line states the "
               "expected answer and what in theirs was off")
        last = ("this was the last question: after the grade, give the "
                "two-line wrap-up and pose nothing further")
        what = last if (run.get("stop") or n >= total) else nxt
    return (f"{base}. This is q{n} of {total} in the {name} "
            f"({run_tally(run)}): {fmt}; {what}. No check "
            "question, no offer.]")


def run_nudge_tail(run):
    """The model graded but forgot the next question: ask for it."""
    n = run["asked"] + 1
    return (f"[quiz protocol: your previous reply did not pose the next "
            f"question. Pose q{n} of {run['total']} now — one question, "
            "numbered, ending with an ask trailer, nothing else. If the "
            "previous reply already posed it, restate it in one line with "
            "the ask trailer.]")


def testout_tail(where, sections, summary_rel, n):
    ids = "; ".join(f"{s['id']} {s['title']}" for s in sections)
    summ = (f" Summary file: {summary_rel} — read it first for the key "
            "concepts and what later chapters build on." if summary_rel
            else " Read the file first.")
    return (f"[test-out protocol on {where}: sections {ids}.{summ} "
            f"{n} questions, one at a time, stopping after "
            f"{TESTOUT_MAX_WRONG} wrong answers. Every content section gets "
            "a question (merge neighbours when there are more than "
            "questions; summary / history / bibliography / exercise-list "
            "sections get none and are 'not tested' in the verdict). "
            "Application over recitation, at least one mechanism question, "
            "no question that contains its own answer, no [p.N] in question "
            "stems. Between questions the grade word and a [p.N] cite only "
            "— the expected answers are withheld until the answer key that "
            "closes the test with the result line, verdict and focus line.]")


def review_tail(items, store):
    bits = []
    for it in items:
        node = store.node_by_id(it.get("sec")) if it.get("sec") else None
        loc = f"§{it['sec']}, {node['file']}" if node else "section unknown"
        flag = ", PRIORITY" if it.get("priority") else ""
        bits.append(f"{it['concept']} ({loc}{flag})")
    return ("[review protocol: items due — " + "; ".join(bits) +
            ". Interleave chapters in this order, priority items first. One "
            "retrieval question per reply (recall or application, not "
            "recognition), quiz protocol, trailers must reuse these exact "
            "concept labels. After the last item, wrap up in two lines with "
            "what moved.]")


def checkpoint_tail(chapter, sections, summary_rel):
    ids = ", ".join(n["id"] for n in sections)
    summ = f" Summary file: {summary_rel}." if summary_rel else ""
    return (f"[checkpoint protocol: chapter {chapter['title']}, sections "
            f"{ids}.{summ} Ask 4 questions, quiz protocol, one at a time; "
            "prioritise concepts the digest marks shaky/learning, then "
            "untested sections; at least 2 application questions.]")


def refresh_tail(chapter, summary_rel, challenge):
    summ = (f"Read {summary_rel} (its 'Assumes' list) first" if summary_rel
            else "Read the chapter's opening section and Grep text/ for "
                 "back-references ('recall', 'as in Chapter', 'Section k.m' "
                 "from earlier chapters)")
    ask = ("" if challenge == 0 else
           " End with 'want a quick check on any of these?'")
    return (f"[refresh protocol: the user is about to start {chapter['title']} "
            f"({chapter['file']}). Identify 3-6 concepts from EARLIER chapters "
            f"this chapter assumes: {summ}. For each: if the digest marks it "
            "mastered, one line ('you've got this: …'); else a 2-3 line "
            f"refresher citing where it was introduced [p.N].{ask}]")


def _scope_where(scope, node, chapter, page, printed):
    if scope == "page":
        pr = f" (book p. {printed})" if printed else ""
        where = f"PDF page {page}{pr}"
        if node:
            where += (f", inside section {node['id']} {node['title']} "
                      f"(file {node['file']}; the page's text follows its "
                      f"'[p.{page}]' anchor line — Grep for it)")
        return where
    if scope == "section":
        return f"section {node['id']} {node['title']} (file {node['file']})"
    return f"chapter {chapter['title']} (file {chapter['file']})"


def scope_lead_and_tail(verb, scope, node, chapter, page, printed,
                        sections=(), summary_rel=None, challenge=1):
    """(visible user message, protocol tail, quiz run or None) for a scope
    verb — a verb on the current page / section / chapter rather than on
    a selection."""
    where = _scope_where(scope, node, chapter, page, printed)
    if scope == "page":
        name = f"this page (PDF p. {page})"
    elif scope == "section":
        name = f"section {node['title']}"      # titles carry their number
    else:
        name = f"chapter {chapter['title']}"
    read = ("Read the section file and take the text after the page's "
            "anchor line" if scope == "page" else "Read the file first")
    summ = f" Summary file: {summary_rel}." if summary_rel else ""
    if verb == "quiz":
        if scope == "chapter":
            return (f"Checkpoint quiz: {chapter['title']}",
                    checkpoint_tail(chapter, sections, summary_rel),
                    quiz_run("checkpoint", QUIZ_COUNTS["checkpoint"]))
        n = QUIZ_COUNTS[scope]
        return (f"Quiz me on {name}",
                f"[quiz protocol on {where}: {read}. {n} questions, one at "
                "a time; prioritise concepts the digest marks shaky; at "
                "least one application question.]",
                quiz_run("quiz", n))
    if verb == "testout":
        n = TESTOUT_COUNTS[scope]
        secs = sections if scope == "chapter" else (node,)
        return (f"Test out of {name}",
                testout_tail(where, secs, summary_rel, n),
                quiz_run("testout", n))
    if verb == "summary":
        if scope == "chapter":
            return (f"Summarise {name}",
                    f"[verb: summarise {where}.{summ} Read the summary file "
                    "(section files only for detail) and give: what the "
                    "chapter is for in one line, its key concepts and main "
                    "results as bullets with [p.N] cites, and what it assumes "
                    "from earlier chapters. A lookup: no pretest.]", None)
        return (f"Summarise {name}",
                f"[verb: summarise {where} — {read}. One line on what it is "
                "for, then the key ideas as 3-8 bullets with [p.N] cites, "
                "equations stated in words, and one line on how it connects "
                "to what came before. A lookup: no pretest.]", None)
    if verb == "explainback":
        return (f"Let me explain {name} back in my own words.",
                explainback_tail(where, False), None)
    if verb == "refresh":
        return (f"Refresh me before {chapter['title']}",
                refresh_tail(chapter, summary_rel, challenge), None)
    raise ValueError(f"unknown scope verb {verb!r}")


def tail_parts(dials, learning_state=None, tail=None):
    """The learning part of the per-turn tail, in order: dials line,
    challenge reminder, style prompt, learning digest, situational protocol
    line. Shared with ask.py."""
    parts = [f"[dials: challenge={dials['challenge']} style={dials['style']}]"]
    rem = CHALLENGE_REMINDERS.get(dials["challenge"])
    if rem:
        parts.append(rem)
    style = STYLE_PROMPTS.get(dials["style"])
    if style:
        parts.append(style)
    if learning_state and dials["challenge"] >= 1:
        d = learning.digest(learning_state)
        if d:
            parts.append(d)
    if tail:
        parts.append(tail)
    return parts


# ---------- nudges & review ----------
NUDGE_MIN_PAGES = 10       # an unsolicited offer needs this much reading …
NUDGE_MIN_SECONDS = 900    # … or this much time since the previous one
REVIEW_LIMIT = 8
REVIEW_MINUTES_PER_ITEM = 1.5
SNOOZE_HOURS = 4


def system_prompt(book_name):
    return (
        "You are the chat sidebar of 'Lantern', a personal reading-companion "
        f"app. The user is currently reading '{book_name}'. Assume questions "
        "refer to that book unless stated otherwise. Be a concise, friendly "
        "study companion: explain clearly, offer intuition and analogies, and "
        "keep answers short enough for a narrow sidebar. Use markdown, and "
        "LaTeX ($...$ / $$...$$) for any math. You have no tools; answer from "
        "knowledge and the conversation. User messages may start with a "
        "[reader context: ...] line reporting their current PDF page — treat "
        "it as ground truth about where they are in the book (PDF page "
        "numbering, so front matter counts), use it to ground your answers, "
        "and don't mention the mechanism. " + bookcontext.VERBS_LINE +
        "\n\n" + bookcontext.LEARNING_RULES
    )


def _sdk_options(**kwargs):
    """Build ClaudeAgentOptions, dropping kwargs this SDK version lacks."""
    valid = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
    dropped = sorted(set(kwargs) - valid)
    if dropped:
        log.info("SDK options not supported by this version, dropped: %s", dropped)
    return ClaudeAgentOptions(**{k: v for k, v in kwargs.items() if k in valid})


async def one_shot(prompt, system, model="haiku", tools=(), cwd=None,
                   max_turns=1, output_format=None):
    """Throwaway client for one question: (result_text, structured_output).
    Used for session titles and chapter summaries."""
    kwargs = dict(model=model, system_prompt=system,
                  allowed_tools=list(tools), tools=list(tools),
                  setting_sources=[], mcp_servers={}, strict_mcp_config=True,
                  max_thinking_tokens=0, max_turns=max_turns)
    if tools:
        kwargs["permission_mode"] = "dontAsk"
    if cwd:
        kwargs["cwd"] = cwd
    if output_format:
        kwargs["output_format"] = output_format
    if os.path.isfile(CLAUDE):
        kwargs["cli_path"] = CLAUDE
    client = ClaudeSDKClient(options=_sdk_options(**kwargs))
    await client.connect()
    try:
        await client.query(prompt)
        text, structured = None, None
        async for msg in client.receive_response():
            if (type(msg).__name__ == "ResultMessage"
                    and not getattr(msg, "is_error", False)):
                text = (getattr(msg, "result", None) or "").strip()
                structured = getattr(msg, "structured_output", None)
        return text, structured
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


class ChatEngine:
    """Persistent Claude chat session + local web server, on a daemon thread."""

    def __init__(self, book_name, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT,
                 greet=True, debug_print=False, book_dir=None):
        self.book_name = book_name
        self.model = model
        self.effort = effort
        self.store = None            # ingested book store -> grounded answers
        if book_dir:
            try:
                self.store = bookstore.BookStore.open(book_dir)
            except Exception:
                log.exception("book store unreadable: %s", book_dir)
        self.greet = greet
        self.debug_print = debug_print
        self.port = None
        self.busy = False
        # Last-used ⚙ settings (books/prefs.json, shared across books):
        # every new chat starts from these, and the model too unless the
        # caller picked one.
        self.prefs = sessions.load_prefs()
        if model == DEFAULT_MODEL and any(
                self.prefs.get("model") == m for _, m in MODELS):
            self.model = self.prefs["model"]
        # Chat sessions: multiple parallel conversations per book, persisted
        # in the store (in-memory only when the book isn't ingested). Each
        # open starts a NEW chat (2026-09-13; earlier ones stay in the
        # dropdown) — reusing a leftover blank one rather than stacking
        # "New chat" entries.
        self.sessions = []
        if self.store:
            self.sessions = sessions.load_all(self.store.book_dir)
            if not self.sessions:
                # pre-sessions stores kept a single sdk id in meta.json
                legacy = self.store.meta.get("session_ids", {}).get("chat")
                if legacy:
                    rec = sessions.new_record(sdk_session_id=legacy,
                                              title="(earlier chat)")
                    self.sessions.append(rec)
                    sessions.save(self.store.book_dir, rec)
        active = self.store.meta.get("active_session") if self.store else None
        if not self.prefs:
            # first run since prefs exist: carry the last chat's dials over
            # instead of dropping the user back to the defaults
            last = next((r for r in self.sessions if r["id"] == active), None)
            if last is not None:
                self.prefs = sessions.dials(last)
        blanks = [r for r in self.sessions if sessions.is_blank(r)]
        blank = next((r for r in blanks if r["id"] == active),
                     blanks[-1] if blanks else None)
        if blank is not None:
            blank.update(self._pref_dials())
            self.current = blank
        else:
            self.current = sessions.new_record(dials=self._pref_dials())
            self.sessions.append(self.current)
        if self.store:
            try:
                sessions.save(self.store.book_dir, self.current)
                self.store.meta["active_session"] = self.current["id"]
                self.store.save_meta()
            except OSError:
                log.exception("couldn't persist the opening session")
        # history is the CURRENT session's transcript (same list object;
        # swapped wholesale on session switch)
        self.history = self.current["history"]
        # learning record (learning.json), cached; refreshed after writes
        self.learning = (learning.load(self.store.book_dir) if self.store
                         else learning.empty_state())
        self.pending_review = None   # review card payload offered to the UI
        self._nudges = {}            # offer id -> {kind, ...} awaiting a click
        self._explainback = None     # scope awaiting the user's own-words explanation
        self._hint = None            # passage last sent for a hint: {key, sec, label}
        self.client = None
        self.loop = None
        self.on_focus = None         # shell hook: chat pane asked for keyboard focus
        self.on_goto = None          # shell hook: drive the reader to a page
        self.on_reload = None        # shell hook: re-read the PDF (fresh underline)
        self.page = None             # live reader position, fed by the shell
        self.total_pages = None
        self.pending_image = None    # (relpath, page) to attach to next turn
        self._turn_annot = None      # selection behind the in-flight turn
        self._turn_tail = ""         # protocol line behind the in-flight turn
        self._checkin_seeded = False # first page report only seeds, no notice
        self._checkin_last = None    # last announced (or seeded) section id
        self._checkin_chapter = None # chapter id at the last check-in
        self._checkin_task = None
        self._notes_task = None      # debounced page-notes line
        self._pagesave_task = None   # debounced last_page persist
        self._subs = []              # one asyncio.Queue per SSE listener
        self._turns = None           # asyncio.Queue of pending prompts
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="lantern-bus")

    # ---------- public, thread-safe ----------

    def start(self, timeout=15):
        self._thread.start()
        if not self._ready.wait(timeout) or self.port is None:
            raise RuntimeError("context bus failed to start")
        return self.port

    def ask(self, text):
        asyncio.run_coroutine_threadsafe(self._enqueue_user(text), self.loop)

    def set_model(self, model):
        asyncio.run_coroutine_threadsafe(self._switch_model(model), self.loop)

    def set_page(self, page, total=None):
        if self.loop is None or not self.loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._set_page(page, total), self.loop)

    def send_selection(self, action, text):
        """Reader selection verbs raised by the shell (the Ctrl+Shift+C
        hotkey) — same dispatch as the right-click menu's /sel requests."""
        if self.loop is None or not self.loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            self._dispatch_selection(action, text), self.loop)

    def send_scope(self, verb, scope):
        """Scope verbs (page / section / chapter) raised by the shell's
        right-click menu."""
        if self.loop is None or not self.loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            self._dispatch_scope(verb, scope), self.loop)

    def open_annotation(self, note):
        """Ctrl+click on a Lantern underline in the reader (shell): `note`
        is the annotation's popup text. Jumps the sidebar to the chat turn
        it names, or to the page notes for a saved highlight."""
        if self.loop is None or not self.loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._open_annotation(note),
                                         self.loop)

    def location(self):
        """Where the reader is, for scope menus: page, its section and
        chapter (ids + titles). Pure lookup — safe from any thread."""
        page = self.page
        loc = {"page": page, "printed": None, "section": None, "chapter": None}
        if not (self.store and page):
            return loc
        loc["printed"] = self.store.printed_label(page)
        node = self.store.locate_page(page)
        ch = self.store.chapter_of(node)
        if node and node is not ch:
            loc["section"] = {"id": node["id"], "title": node["title"]}
        if ch:
            loc["chapter"] = {"id": ch["id"], "title": ch["title"]}
        return loc

    def shutdown(self):
        if self.loop and self.loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
                fut.result(timeout=5)
            except Exception:
                pass

    # sdk session id of the CURRENT chat session (kept attribute-shaped:
    # _ensure_client/_legacy_claude_p read and assign it)
    @property
    def session_id(self):
        return self.current["sdk_session_id"]

    @session_id.setter
    def session_id(self, sid):
        self.current["sdk_session_id"] = sid

    # ---------- loop thread ----------

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception:
            log.exception("bus loop crashed")
        finally:
            self._ready.set()

    async def _main(self):
        self._turns = asyncio.Queue()
        if HAVE_AIOHTTP:
            await self._start_server()
        self._ready.set()
        await self._worker()

    async def _start_server(self):
        app = web.Application()
        app.router.add_get("/", self._h_index)
        app.router.add_get("/state", self._h_state)
        app.router.add_get("/events", self._h_events)
        app.router.add_post("/ask", self._h_ask)
        app.router.add_post("/confidence", self._h_confidence)
        app.router.add_post("/model", self._h_model)
        app.router.add_post("/focus", self._h_focus)
        app.router.add_post("/goto", self._h_goto)
        app.router.add_post("/pageimg", self._h_pageimg)
        app.router.add_post("/session", self._h_session)
        app.router.add_post("/dials", self._h_dials)
        app.router.add_post("/review", self._h_review)
        app.router.add_post("/nudge", self._h_nudge)
        app.router.add_route("*", "/sel", self._h_selection)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self._runner = runner
        try:
            site = web.TCPSite(runner, "127.0.0.1", PREFERRED_PORT)
            await site.start()
        except OSError:
            log.warning("port %d taken (another Lantern?) — using an "
                        "ephemeral port; reader right-click actions go to "
                        "whichever instance owns %d",
                        PREFERRED_PORT, PREFERRED_PORT)
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        log.info("context bus on http://127.0.0.1:%d (sdk=%s)", self.port, HAVE_SDK)

    # ---------- turn pipeline ----------

    async def _worker(self):
        # session start: a due-review card takes the greeting's slot; greet
        # only a genuinely fresh session otherwise — a resumed chat already
        # has its transcript on screen and needs no re-introduction
        offered = False
        if self.store:
            offered = await self._offer_review()
        if self.greet and not offered and not self.session_id and not any(
                h["role"] in ("you", "claude") for h in self.history):
            await self._turns.put(
                "(app start) The reader just opened the book. Greet them in one "
                "short sentence and say what you can help with. No question back."
            )
        while True:
            item = await self._turns.get()
            if item is None:
                break
            if isinstance(item, str):
                item = {"prompt": item, "annot": None, "tail": None}
            if item.get("new_chat"):
                await self._fresh_session()
                self.history.append({"role": "you", "text": item["prompt"]})
                self._emit({"type": "user", "text": item["prompt"]})
            if item.get("run"):             # after the switch: lands on the task chat
                self.current["run"] = item["run"]
            if item.get("auto"):            # app-raised turn: a meta line, no "you"
                self._note(item["auto"])
            self._turn_annot = item.get("annot")
            self._turn_tail = item.get("tail") or ""
            try:
                await asyncio.wait_for(self._turn(item["prompt"], item.get("tail")),
                                       TURN_TIMEOUT)
            except asyncio.TimeoutError:
                self._finish_turn(f"[error] no reply within {TURN_TIMEOUT}s", ok=False)
            except Exception as e:
                log.exception("turn failed")
                self._finish_turn(f"[error] {type(e).__name__}: {e}", ok=False)

    async def _set_page(self, page, total):
        if (page, total) == (self.page, self.total_pages):
            return
        self.page = page
        self.total_pages = total or self.total_pages
        self._emit({"type": "page", "page": self.page,
                    "total": self.total_pages, "loc": self.location()})
        if self.store:
            self._watch_section(page)
            if self._pagesave_task:
                self._pagesave_task.cancel()
            self._pagesave_task = self.loop.create_task(
                self._persist_page(page))
            if self._notes_task:
                self._notes_task.cancel()
            self._notes_task = self.loop.create_task(self._page_notes(page))

    async def _persist_page(self, page):
        """Debounced: remember the reading position once it holds still, so
        the next launch reopens the book right there."""
        await asyncio.sleep(PAGE_SAVE_DELAY)
        self._pagesave_task = None
        self.store.meta["last_page"] = page
        try:
            await self.loop.run_in_executor(None, self._save_meta)
        except OSError:
            log.exception("couldn't save last page")

    async def _page_notes(self, page):
        """Debounced: the journal entries located on this page, for the
        sidebar's notes line (replaces the reader's flickering hover popup)."""
        await asyncio.sleep(NOTES_DELAY)
        self._notes_task = None
        items = await self.loop.run_in_executor(None, self._notes_for, page)
        self._emit({"type": "notes", "page": page, "items": items})

    def _notes_for(self, page):
        out = []
        for e in journal.entries_for_page(self.store.book_dir, page):
            out.append({"kind": e["kind"], "when": e["when"],
                        "title": e.get("title") or "",
                        "text": (e.get("text") or "")[:1500]})
        return out

    # ---------- chapter-boundary check-ins ----------

    def _watch_section(self, page):
        """Track the section the reader settles in, so crossing into a new
        chapter can offer a refresher / checkpoint. Silent otherwise: the
        old '▸ entering …' line pushed the chat up while navigating."""
        node = self.store.locate_page(page)
        sec = node["id"] if node else None
        if not self._checkin_seeded:            # opening position: no notice
            self._checkin_seeded = True
            self._checkin_last = sec
            ch = self.store.chapter_of(node)
            self._checkin_chapter = ch["id"] if ch else None
            return
        if sec == self._checkin_last or sec is None:
            if self._checkin_task:              # wandered back: stand down
                self._checkin_task.cancel()
                self._checkin_task = None
            return
        if self._checkin_task:
            self._checkin_task.cancel()
        self._checkin_task = self.loop.create_task(self._checkin(node))

    async def _checkin(self, node):
        await asyncio.sleep(CHECKIN_DELAY)
        self._checkin_task = None
        self._checkin_last = node["id"]
        chapter = self.store.chapter_of(node)
        # chapter boundary: offer a refresher / checkpoint, within budget
        ch_id = chapter["id"] if chapter else None
        prev_id, self._checkin_chapter = self._checkin_chapter, ch_id
        if (chapter and ch_id != prev_id and self._forward(prev_id, ch_id)
                and self._nudge_allowed()):
            self._offer_chapter(chapter, self.store.node_by_id(prev_id)
                                if prev_id else None)

    def _forward(self, prev_id, new_id):
        """True when moving into a later chapter (not flipping back)."""
        if not prev_id:
            return True
        ids = [n["id"] for n in self.store.toc]
        try:
            return ids.index(new_id) > ids.index(prev_id)
        except ValueError:
            return True

    def _offer_chapter(self, chapter, prev):
        actions = [{"id": "refresh", "label": "Refresh me"},
                   {"id": "testout", "label": "Test out"}]
        text = (f"New chapter: {chapter['title']}. Want a refresher on what "
                "it assumes, a test-out to see if you can skip it")
        if prev and self._chapter_has_activity(prev):
            num = prev["id"][:2].lstrip("0") or "0"
            actions.append({"id": "checkpoint", "label": f"Checkpoint ch. {num}"})
            text += f", or a checkpoint quiz on {prev['title']}"
        text += "?"
        self._offer("chapter", text, actions, chapter=chapter["id"],
                    prev=prev["id"] if prev else None)

    def _chapter_has_activity(self, chapter):
        prefix = chapter["id"][:2]
        if any((c.get("sec") or "")[:2] == prefix
               for c in self.learning.get("concepts", {}).values()):
            return True
        try:
            return any(journal.count_for_section(self.store.book_dir, n["id"])
                       for n in self.store.sections_of(chapter))
        except OSError:
            return False

    # ---------- nudges (budgeted, unsolicited offers) ----------

    def _nudge_allowed(self):
        n = self.current.get("nudges") or {}
        if not n.get("last_t"):
            return True
        return (time.time() - n["last_t"] >= NUDGE_MIN_SECONDS
                or abs((self.page or 0) - n.get("last_page", 0)) >= NUDGE_MIN_PAGES)

    def _offer(self, kind, text, actions, **data):
        """Emit an actionable offer line. Counts against the budget."""
        nid = f"n-{int(time.time() * 1000)}"
        self._nudges[nid] = {"kind": kind, **data}
        n = self.current.setdefault("nudges", {"last_t": 0, "last_page": 0, "count": 0})
        n.update(last_t=int(time.time()), last_page=self.page or 0,
                 count=n.get("count", 0) + 1)
        self._save_current(touch=False)
        self._emit({"type": "nudge", "id": nid, "kind": kind, "text": text,
                    "actions": actions})
        return nid

    async def _act_nudge(self, nid, action):
        info = self._nudges.pop(nid, None)
        if info is None or action == "dismiss":
            return
        if self.busy or not self._turns.empty():
            self._note("finish the current reply first")
            self._nudges[nid] = info          # keep the offer alive
            return
        if info["kind"] == "pretest" and action == "reveal":
            await self._enqueue_user("Just tell me.")
        elif info["kind"] == "chapter" and action == "refresh":
            ch = self.store.node_by_id(info["chapter"])
            rel = await self._ensure_summary(ch)
            await self._enqueue_user(
                f"Refresh me before {ch['title']}",
                tail=refresh_tail(ch, rel, self.current.get("challenge", 1)),
                new_chat=True)
        elif info["kind"] == "chapter" and action == "checkpoint" and info.get("prev"):
            prev = self.store.node_by_id(info["prev"])
            rel = await self._ensure_summary(prev)
            await self._enqueue_user(
                f"Checkpoint quiz: {prev['title']}",
                tail=checkpoint_tail(prev, self.store.sections_of(prev), rel),
                new_chat=True,
                run=quiz_run("checkpoint", QUIZ_COUNTS["checkpoint"]))
        elif info["kind"] == "chapter" and action == "testout":
            # the reader is in the new chapter: the scope verb finds it
            msg, ok = await self._dispatch_scope("testout", "chapter")
            if not ok:
                self._note(msg)
        else:
            log.info("nudge %s: unknown action %r", info["kind"], action)

    async def _ensure_summary(self, chapter):
        """summaries/NN.md relpath for a chapter, writing it lazily (one
        Haiku call, ~10-20s) when missing. None if unavailable."""
        if not chapter or chapter["level"] != 1:
            return None
        if summaries.exists(self.store, chapter):
            return summaries.relpath(self.store, chapter)
        if not HAVE_SDK:
            return None
        self._note(f"summarising {chapter['title']} …")
        try:
            path = await asyncio.wait_for(
                summaries.summarize_chapter(self.store, chapter, one_shot), 120)
        except Exception:
            log.exception("chapter summary failed")
            return None
        return summaries.relpath(self.store, chapter) if path else None

    # ---------- spaced review ----------

    def _review_payload(self, items):
        due = []
        for it in items:
            node = self.store.node_by_id(it.get("sec")) if it.get("sec") else None
            last = it.get("last")
            due.append({"concept": it["concept"], "sec": it.get("sec"),
                        "title": node["title"] if node else "",
                        "last": (time.strftime("%Y-%m-%d", time.localtime(last))
                                 if last else ""),
                        "misses": it.get("misses", 0),
                        "priority": it.get("priority", False)})
        return {"n": len(due),
                "minutes": math.ceil(len(due) * REVIEW_MINUTES_PER_ITEM),
                "due": due, "items": items}

    async def _offer_review(self, force=False):
        """Refresh the learning cache and (re)emit the review card. Returns
        True when something is due."""
        self.learning = await self.loop.run_in_executor(
            None, learning.load, self.store.book_dir)
        now = time.time()
        items = []
        if force or self.learning.get("snoozed_until", 0) <= now:
            items = learning.due(self.learning, now, REVIEW_LIMIT)
        self.pending_review = self._review_payload(items) if items else None
        self._emit_review()
        return bool(items)

    def _review_public(self):
        p = self.pending_review or {"n": 0, "minutes": 0, "due": []}
        return {"n": p["n"], "minutes": p["minutes"], "due": p["due"]}

    def _emit_review(self):
        self._emit({"type": "review", **self._review_public()})

    async def _act_review(self, action):
        if action == "show":
            await self._offer_review(force=True)
            return
        if action == "later":
            await self.loop.run_in_executor(
                None, learning.snooze, self.store.book_dir, SNOOZE_HOURS)
        if action == "start":
            if not self.pending_review:
                return
            if self.busy or not self._turns.empty():
                self._note("finish the current reply first")
                return
            items = self.pending_review["items"]
            self.pending_review = None
            self._emit_review()
            n = len(items)
            await self._enqueue_user(
                f"Start review: {n} concept{'s' if n != 1 else ''} due",
                tail=review_tail(items, self.store),
                run=quiz_run("review", n))
            return
        self.pending_review = None
        self._emit_review()

    # ---------- notes & context ----------

    def _note(self, text):
        """Meta line into history + sidebar (loop thread only)."""
        self.history.append({"role": "meta", "text": text})
        self._emit({"type": "meta", "text": text})

    def _note_threadsafe(self, text):
        self.loop.call_soon_threadsafe(self._note, text)

    def _dials(self):
        return sessions.dials(self.current)

    def _pref_dials(self):
        """Dials a brand-new chat starts with: the last-used ones
        (books/prefs.json), validated, else the defaults."""
        d = dict(sessions.DEFAULT_DIALS)
        s = LEGACY_STYLES.get(self.prefs.get("style"), self.prefs.get("style"))
        if s in STYLE_PROMPTS:
            d["style"] = s
        c = self.prefs.get("challenge")
        if isinstance(c, int) and c in dict(CHALLENGES):
            d["challenge"] = c
        return d

    def _save_prefs(self, **patch):
        """Write-through of the ⚙ settings the user has in front of them:
        called on a dial change, a model change, and a session switch (so
        prefs always mirror the visible panel)."""
        if all(self.prefs.get(k) == v for k, v in patch.items()):
            return
        self.prefs.update(patch)
        try:
            sessions.save_prefs(self.prefs)
        except OSError:
            log.exception("couldn't persist prefs")

    def _reader_context(self, prompt, tail=None):
        """Volatile per-turn tail: reader position, pending page image, the
        learning dials + digest, and this turn's protocol line — never
        touches the cached prefix."""
        parts = []
        if self.page:
            if self.store:
                line = bookcontext.reader_context_line(
                    self.store, self.page, self.total_pages)
                if line:
                    parts.append(line)
            else:
                total = f" of {self.total_pages}" if self.total_pages else ""
                parts.append(f"[reader context: the user is currently "
                             f"viewing PDF page {self.page}{total}]")
        if self.pending_image:
            rel, pg = self.pending_image
            self.pending_image = None
            parts.append(f"[attachment: {rel} is a PNG rendering of PDF "
                         f"page {pg}, which the user is viewing. Read that "
                         "file first to see the page's figures, diagrams, "
                         "and layout that plain text extraction misses.]")
        parts += tail_parts(self._dials(), self.learning if self.store else None,
                            tail)
        return "\n".join(parts) + f"\n\n{prompt}"

    async def _enqueue_user(self, text, annot=None, tail=None, new_chat=False,
                            run=None):
        """Queue a user turn. new_chat=True runs it in a fresh session
        (self-contained tasks: quizzes, summaries, refreshers): the switch
        and the visible message are deferred to the worker so an action
        raised mid-reply lands after that reply, not inside it. `run` (see
        quiz_run) starts a multi-question protocol on the chat it lands in."""
        if new_chat:
            if self.busy or not self._turns.empty():
                self._note("will open a new chat for that after this reply")
            await self._turns.put({"prompt": text, "annot": annot,
                                   "tail": tail, "new_chat": True, "run": run})
            return
        if self.current.get("chapter") is None and sessions.is_blank(self.current):
            # the chat each book open starts predates any page position;
            # stamp its chapter (dropdown label) from the first message
            self.current["chapter"] = self._current_chapter()
        self.history.append({"role": "you", "text": text})
        self._emit({"type": "user", "text": text})
        await self._turns.put({"prompt": text, "annot": annot, "tail": tail,
                               "run": run})

    async def _fresh_session(self):
        """Start a new chat for a task (dials carry over, as for every new
        chat). A current session with no user turns yet is reused — no
        blank orphans."""
        if sessions.is_blank(self.current):
            return
        await self._switch_session(new=True, force=True)

    async def _turn(self, prompt, tail=None):
        self.busy = True
        self._emit({"type": "status", "busy": True, "model": self.model})
        t0 = time.monotonic()
        if not HAVE_SDK:
            full, ok = await self.loop.run_in_executor(
                None, self._legacy_claude_p, self._reader_context(prompt, tail))
            self._finish_turn(full, ok, time.monotonic() - t0)
            return

        await self._ensure_client()
        parts = []                         # VISIBLE text only
        filt = learning.TrailerFilter()    # hides the <!--rb …--> trailer live
        full, ok, first = None, True, None
        await self.client.query(self._reader_context(prompt, tail))
        async for msg in self.client.receive_response():
            kind = type(msg).__name__
            if kind == "StreamEvent":
                ev = getattr(msg, "event", None) or {}
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        if first is None:
                            first = time.monotonic() - t0
                            log.info("first token after %.1fs", first)
                        visible = filt.feed(delta["text"])
                        if visible:
                            parts.append(visible)
                            self._emit({"type": "delta", "text": visible})
            elif kind == "ResultMessage":
                sid = getattr(msg, "session_id", None)
                if sid and sid != self.session_id:
                    self.session_id = sid
                    self._persist_session()
                if getattr(msg, "is_error", False):
                    ok = False
                    full = f"[error] {getattr(msg, 'result', None) or 'unknown'}"
                else:
                    full = (getattr(msg, "result", None) or "").strip()
        rest = filt.flush()
        if rest:
            parts.append(rest)
            self._emit({"type": "delta", "text": rest})
        if not full:                           # no result msg / empty result
            raw = "".join(parts) + "".join(filt.dropped)
            full, ok = raw or "[error] empty reply", bool(raw) and ok
        self._finish_turn(full, ok, time.monotonic() - t0)

    def _finish_turn(self, full, ok, elapsed=None):
        self.busy = False
        annot, self._turn_annot = self._turn_annot, None
        events = []
        if ok:
            full, events, warns = learning.parse_trailer(full)
            for w in warns:
                log.warning("trailer: %s", w)
            full = full or "(no reply text)"
        if ok and annot and self.store:
            # underline the asked-about passage in the PDF + journal it,
            # off-loop (opens the PDF; ~100s of ms). The note carries a
            # chat ref — session id + this turn's ordinal among the user
            # turns (stable across reloads: meta lines aren't persisted) —
            # so Ctrl+click on the underline can reopen the exchange.
            n_you = sum(1 for h in self.history if h["role"] == "you")
            annot = dict(annot, chat=f"{self.current['id']}#{n_you}")
            self.loop.run_in_executor(None, self._apply_annotation,
                                      annot, full)
        role = "claude" if ok else "meta"
        self.history.append({"role": role, "text": full})
        if self.store:
            if events:
                self.loop.create_task(self._record_signals(events))
            elif ok:
                if self.current.get("quiz"):
                    self._quiz_strike()
                self._run_after_turn(False, None)
            self._save_current()
        if (ok and self.current.get("title") is None
                and any(h["role"] == "you" for h in self.history)):
            self.loop.create_task(self._auto_title(self.current))
        self._emit({"type": "done", "text": full, "ok": ok,
                    "elapsed": round(elapsed, 1) if elapsed else None})
        self._emit({"type": "status", "busy": False, "model": self.model})
        if elapsed:
            log.info("turn done ok=%s in %.1fs (%d chars, %d events)",
                     ok, elapsed, len(full), len(events))

    # ---------- learning signals ----------

    def _quiz_state(self):
        q = self.current.get("quiz")
        if not q:
            return None
        out = {"pending": True, "item": {"q": q.get("q"),
                                        "concept": q.get("concept"),
                                        "sec": q.get("sec")}}
        run = self.current.get("run")
        if run:
            out["run"] = {"n": run["asked"], "total": run["total"],
                          "kind": run["kind"]}
        return out

    def _quiz_strike(self):
        """A pending question survived one unrelated turn; after two, drop
        it (and the quiz it belonged to — the user has moved on) so the
        confidence pills don't stick around forever."""
        q = self.current["quiz"]
        q["strikes"] = q.get("strikes", 0) + 1
        if q["strikes"] >= 2:
            self.current["quiz"] = None
            self.current["run"] = None
            self._emit({"type": "quiz", "pending": False})

    def _run_after_turn(self, asked, graded, conf=None):
        """Keep a multi-question protocol on the rails. `asked`: this reply
        posed a question; `graded`: the result it gave, if it graded one,
        with `conf` the confidence the user attached to that answer.
        The model is meant to keep posing questions until the agreed count,
        but a grading reply sometimes ends with a check question or a
        wrap-up instead (challenge-dial habits) — then the app asks for the
        next question itself, once, rather than letting the quiz die."""
        run = self.current.get("run")
        if not run:
            return
        if asked:
            run["asked"] += 1
            run["nudged"] = 0
        if graded:
            run["graded"] += 1
            if graded == "wrong":
                run["wrong"] += 1
            elif graded in ("correct", "partial"):
                run[graded] = run.get(graded, 0) + 1
                if graded == "correct" and conf == "guess":
                    run["guessed"] = run.get("guessed", 0) + 1
        if run_finished(run):
            self.current["run"] = None
            return
        if self.current.get("quiz"):            # a question is on the table
            return
        name = RUN_NAMES.get(run["kind"], "quiz")
        if run["nudged"] >= 1:
            self._note(f"{name} ended early — Claude stopped asking")
            self.current["run"] = None
            return
        run["nudged"] += 1
        self._turns.put_nowait({
            "prompt": "Next question, please.",
            "tail": run_nudge_tail(run),
            "auto": f"{name}: q{run['asked'] + 1} of {run['total']} is "
                    "owed — asking Claude for it"})

    async def _record_signals(self, events):
        q = self.current.get("quiz") or {}
        run = self.current.get("run") or {}
        node = self.store.locate_page(self.page) if self.page else None
        ctx = {"sid": self.current["id"], "page": self.page,
               "sec_fallback": node["id"] if node else None,
               "conf": q.get("conf"), "q": q.get("q"),
               "placement": run.get("kind") == "testout",
               "hint": self._hint,
               "toc_ids": {n["id"] for n in self.store.toc}}
        try:
            recorded = await self.loop.run_in_executor(
                None, learning.record, self.store.book_dir, events, ctx)
            self.learning = await self.loop.run_in_executor(
                None, learning.load, self.store.book_dir)
        except Exception:
            log.exception("recording learning events failed")
            return
        touched = False
        asked, graded = False, None
        # inside a quiz/review/checkpoint/test-out protocol every ask is a
        # quiz question, whatever flag the model put on it
        in_quiz = bool(run) or self._turn_tail.startswith(
            ("[quiz", "[review protocol", "[checkpoint protocol",
             "[test-out protocol"))
        for ev in recorded:
            kind = ev["ev"]
            sec_node = self.store.node_by_id(ev.get("sec")) if ev.get("sec") else None
            if kind == "ask":
                touched = True
                if ev.get("pretest") and not in_quiz:
                    self._offer_pretest()
                else:
                    asked = True
                    self.current["quiz"] = {
                        "q": ev.get("q"), "concept": ev.get("concept"),
                        "sec": ev.get("sec"), "conf": None,
                        "asked_t": ev["t"], "strikes": 0}
            elif kind == "quiz":
                touched = True
                graded = ev["result"]
                if not asked:       # [ask, quiz] order must not lose the question
                    self.current["quiz"] = None
                result = {"grade": ev["result"], "confidence": ev.get("conf"),
                          "concept": ev.get("concept"), "hc": ev.get("hc")}
                you = next((h for h in reversed(self.history)
                            if h.get("role") == "you"), None)
                if you is not None:
                    you["quiz"] = result
                self._emit({"type": "quiz", "pending": False, "result": result})
                conf = CONFIDENCE_WORDS.get(ev.get("conf") or "", "no confidence given")
                line = (f"{ev['result']} ({conf}) — {ev.get('concept')}"
                        + (" ⚠ high-confidence miss" if ev.get("hc") else "")
                        + (f" — {ev['note']}" if ev.get("note") else ""))
                self.loop.run_in_executor(
                    None, self._journal_quiet, "quiz", None, line, sec_node)
            elif kind == "hint":
                line = (f"Hint level {ev['level']} — "
                        f"{ev.get('label') or 'selected passage'}"
                        + (" — solution shown" if ev.get("solution") else "")
                        + (f" — solved: {ev['solved']}" if "solved" in ev else ""))
                self.loop.run_in_executor(
                    None, self._journal_quiet, "hint", None, line, sec_node)
        if not touched:
            self._quiz_strike_if_pending()
        self._run_after_turn(asked, graded, q.get("conf"))
        if self.current.get("quiz"):
            self._emit({"type": "quiz", **self._quiz_state()})
        self._emit({"type": "signal", "events": recorded})
        self._save_current(touch=False)

    def _quiz_strike_if_pending(self):
        if self.current.get("quiz"):
            self._quiz_strike()

    def _offer_pretest(self):
        self._offer("pretest", "Have a go first — or", [
            {"id": "reveal", "label": "Just tell me"}])

    def _journal_quiet(self, kind, passage, answer, node):
        try:
            journal.append(self.store.book_dir, kind, passage, answer=answer,
                           node=node, page=None)
        except OSError:
            log.exception("journal write failed")

    def _save_current(self, touch=True):
        if self.store:
            rec = self.current
            self.loop.run_in_executor(
                None, lambda: sessions.save(self.store.book_dir, rec, touch=touch))

    # ---------- chat sessions & dials ----------

    def _sessions_payload(self):
        ordered = sorted(self.sessions, key=lambda r: r.get("updated", 0),
                         reverse=True)
        return [{"id": r["id"], "label": sessions.label(r),
                 "current": r is self.current} for r in ordered]

    def _current_chapter(self):
        if not (self.store and self.page):
            return None
        node = self.store.locate_page(self.page)
        ch = self.store.chapter_of(node)
        return ch["title"] if ch else (node["title"] if node else None)

    def _session_payload(self, rec):
        return {"type": "session", "id": rec["id"],
                "dials": sessions.dials(rec),
                "quiz": self._quiz_state(),
                "review": self._review_public(),
                "history": rec["history"],
                "sessions": self._sessions_payload()}

    async def _switch_session(self, sid=None, new=False, force=False):
        """force=True skips the busy guard — only the worker uses it, between
        turns, where queued items are exactly what should follow the switch."""
        if not force and (self.busy or not self._turns.empty()):
            self._note("finish the current reply before switching chats")
            return
        if new:
            rec = sessions.new_record(chapter=self._current_chapter(),
                                      dials=self._pref_dials())
            self.sessions.append(rec)
        else:
            rec = next((r for r in self.sessions if r["id"] == sid), None)
            if rec is None or rec is self.current:
                return
        if self.client is not None:     # next turn reconnects with the new
            old, self.client = self.client, None   # session's resume id
            try:
                await old.disconnect()
            except Exception:
                pass
        prev = self.current
        self.current = rec
        self._hint = None                   # hint passages are per chat
        self.history = rec["history"]
        self._save_prefs(**sessions.dials(rec))   # panel now shows these
        if self.store:
            try:
                sessions.save(self.store.book_dir, prev, touch=False)
                sessions.save(self.store.book_dir, rec, touch=False)
                self.store.meta["active_session"] = rec["id"]
                self._save_meta()
            except OSError:
                log.exception("couldn't persist session switch")
        self._emit(self._session_payload(rec))
        log.info("session -> %s (%s)", rec["id"], "new" if new else "switch")

    async def _open_annotation(self, note):
        m = CHAT_REF_RE.search(note or "")
        if not m:
            # a saved highlight (never had a chat) or a pre-ref note: the
            # journal entry is in the page notes, so open that panel
            self._emit({"type": "jump", "notes": True})
            if "highlight" in (note or ""):
                self._note("★ saved highlight — no chat behind it; its "
                           "journal entry is in the page notes")
            else:
                self._note("✎ this note predates chat links — its Q&A is "
                           "in the page notes and journal.md")
            return
        sid, turn = m.group(1), int(m.group(2))
        rec = next((r for r in self.sessions if r["id"] == sid), None)
        if rec is None:
            self._note("the chat behind this note no longer exists — its "
                       "Q&A is still in the page notes and journal.md")
            return
        if rec is not self.current:
            await self._switch_session(sid)       # busy: it says so itself
            if self.current is not rec:
                return
        self._emit({"type": "jump", "turn": turn})
        log.info("annotation -> %s turn %d", sid, turn)

    async def _set_dials(self, style=None, challenge=None):
        changed = False
        if style is not None:
            style = LEGACY_STYLES.get(style, style)
            if style not in STYLE_PROMPTS:
                self._note(f"unknown style '{style}'")
            elif style != self.current.get("style"):
                self.current["style"] = style
                changed = True
        if challenge is not None:
            try:
                challenge = int(challenge)
            except (TypeError, ValueError):
                challenge = -1
            if challenge not in dict(CHALLENGES):
                self._note(f"unknown challenge level '{challenge}'")
            elif challenge != self.current.get("challenge"):
                self.current["challenge"] = challenge
                changed = True
        if changed:
            self._save_current(touch=False)
            self._save_prefs(**self._dials())
            log.info("dials -> %s", self._dials())
        self._emit({"type": "dials", **self._dials()})

    async def _auto_title(self, rec):
        """Name a session from its first exchange (Claude-GUI style): a
        one-shot haiku call, falling back to a truncated first message."""
        first = next((h["text"] for h in rec["history"]
                      if h["role"] == "you"), "")
        title = None
        if HAVE_SDK:
            try:
                title, _ = await one_shot(
                    "Reply with ONLY a short title — 2-5 words, no quotes, no "
                    "trailing punctuation — for a chat about the book "
                    f"'{self.book_name}' that opens with:\n\n{first[:500]}",
                    "You title chat conversations.")
            except Exception:
                log.exception("auto-title call failed; using fallback")
        title = title or " ".join(first.split())[:40] or "Chat"
        rec["title"] = " ".join(title.split()).strip("\"'“”").rstrip(".")[:60]
        if self.store:
            try:
                await self.loop.run_in_executor(
                    None, lambda: sessions.save(self.store.book_dir, rec,
                                                touch=False))
            except OSError:
                log.exception("couldn't persist title")
        self._emit({"type": "sessions", "sessions": self._sessions_payload()})
        log.info("session titled: %s", rec["title"])

    # ---------- SDK client lifecycle ----------

    def _persist_session(self):
        if not self.store:
            return
        try:
            sessions.save(self.store.book_dir, self.current)
            self.store.meta["active_session"] = self.current["id"]
            self._save_meta()
        except OSError:
            log.exception("couldn't persist session")

    def _save_meta(self):
        self.store.save_meta()

    async def _ensure_client(self):
        if self.client is not None:
            return
        kwargs = dict(
            model=self.model,
            effort=self.effort,
            system_prompt=system_prompt(self.book_name),
            allowed_tools=[],
            tools=[],
            setting_sources=[],
            mcp_servers={},
            include_partial_messages=True,
            max_thinking_tokens=0,
            strict_mcp_config=True,
        )
        if self.store:
            # grounded mode: cached TOC system prompt + Read/Grep/Glob
            # scoped to the book store (see bookcontext.sdk_kwargs)
            kwargs.update(bookcontext.sdk_kwargs(self.store))
        if os.path.isfile(CLAUDE):
            kwargs["cli_path"] = CLAUDE
        if self.session_id:
            kwargs["resume"] = self.session_id
        self.client = ClaudeSDKClient(options=_sdk_options(**kwargs))
        await self.client.connect()
        log.info("SDK client connected (model=%s, resume=%s)",
                 self.model, self.session_id)

    async def _switch_model(self, model):
        if model == self.model:
            return
        if not any(model == m for _, m in MODELS):
            self._emit({"type": "meta", "text": f"unknown model '{model}'"})
            return
        self.model = model
        self._save_prefs(model=model)
        if self.client is not None:
            try:
                await self.client.set_model(model)   # live switch, session kept
            except Exception:
                old, self.client = self.client, None  # fall back: reconnect+resume
                try:
                    await old.disconnect()
                except Exception:
                    pass
        label = next(lbl for lbl, m in MODELS if m == model)
        note = f"model → {label}"
        self.history.append({"role": "meta", "text": note})
        self._emit({"type": "meta", "text": note})
        self._emit({"type": "status", "busy": self.busy, "model": self.model})
        log.info("model switched to %s", model)

    async def _shutdown(self):
        if self.store:
            try:
                sessions.save(self.store.book_dir, self.current, touch=False)
                self.store.meta["active_session"] = self.current["id"]
                if self.page:
                    self.store.meta["last_page"] = self.page
                self._save_meta()
            except OSError:
                log.exception("couldn't persist state at shutdown")
        await self._turns.put(None)
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        # release the fixed port: the shell may open another book in-process
        runner = getattr(self, "_runner", None)
        if runner is not None:
            self._runner = None
            try:
                await runner.cleanup()
            except Exception:
                log.exception("bus server cleanup failed")

    # ---------- legacy fallback: claude -p per message ----------

    def _legacy_claude_p(self, prompt):
        sp = (bookcontext.build_system_prompt(self.store, tools=False)
              if self.store else system_prompt(self.book_name))
        cmd = [CLAUDE, "-p", "--output-format", "json", "--model", self.model,
               "--append-system-prompt", sp]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        cmd.append(prompt)
        try:
            run = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 timeout=TURN_TIMEOUT, creationflags=CREATE_NO_WINDOW)
            data = json.loads(run.stdout.strip() or "{}")
            if data.get("is_error"):
                return f"[error] {data.get('result', run.stderr)}", False
            self.session_id = data.get("session_id", self.session_id)
            return data.get("result", "").strip(), True
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}", False

    # ---------- events ----------

    def _emit(self, event):
        # Serialise now, not when the SSE writer drains its queue: payloads
        # such as the session event carry live references (rec["history"])
        # that the worker mutates a moment later, which would otherwise
        # show the new user message twice (once in the replayed history,
        # once from the following "user" event).
        payload = json.dumps(event, ensure_ascii=False)
        if self.debug_print:
            print("EVENT", payload[:300], flush=True)
        for q in list(self._subs):
            q.put_nowait((event["type"], payload))

    # ---------- HTTP handlers (loop thread) ----------

    async def _h_index(self, request):
        # Edge's app window would otherwise keep a heuristically cached copy
        # of chat.html across restarts and miss UI updates.
        return web.FileResponse(CHAT_HTML,
                                headers={"Cache-Control": "no-cache"})

    async def _h_state(self, request):
        notes = []
        if self.store and self.page:
            notes = await self.loop.run_in_executor(None, self._notes_for, self.page)
        return web.json_response({
            "book": self.book_name, "model": self.model, "busy": self.busy,
            "models": [{"label": lbl, "id": m} for lbl, m in MODELS],
            "history": self.history, "sdk": HAVE_SDK,
            "page": self.page, "total": self.total_pages,
            "can_image": self._can_image(),
            "sessions": self._sessions_payload(),
            "dials": self._dials(), "dial_options": DIAL_OPTIONS,
            "quiz": self._quiz_state(),
            "review": self._review_public(),
            "notes": {"page": self.page, "items": notes},
        })

    def _can_image(self):
        return bool(HAVE_SDK and pdftools.HAVE_PYMUPDF and self.store
                    and os.path.isfile(self.store.meta.get("source_path", "")))

    async def _h_ask(self, request):
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"ok": False, "error": "empty"}, status=400)
        tail = None
        q = self.current.get("quiz")
        if q:
            conf = body.get("confidence")
            if conf in learning.CONFIDENCES:
                q["conf"] = conf
            run = self.current.get("run")
            if run and STOP_RE.match(text):
                run["stop"] = True
            tail = quiz_answer_tail(q, run)
        elif self._explainback:
            tail = explainback_tail(self._explainback, True)
        self._explainback = None
        await self._enqueue_user(text, tail=tail)
        return web.json_response({"ok": True})

    async def _h_confidence(self, request):
        body = await request.json()
        q = self.current.get("quiz")
        conf = body.get("confidence")
        if q and (conf in learning.CONFIDENCES or conf is None):
            q["conf"] = conf
        return web.json_response({"ok": bool(q),
                                  "confidence": q.get("conf") if q else None})

    async def _h_model(self, request):
        body = await request.json()
        await self._switch_model((body.get("model") or "").strip())
        return web.json_response({"ok": True, "model": self.model})

    async def _h_session(self, request):
        body = await request.json()
        self._explainback = None            # the invitation belonged to the old chat
        if body.get("new"):
            await self._switch_session(new=True)
        else:
            await self._switch_session((body.get("id") or "").strip())
        return web.json_response({"ok": True, "id": self.current["id"]})

    async def _h_dials(self, request):
        body = await request.json()
        await self._set_dials(style=body.get("style"),
                              challenge=body.get("challenge"))
        return web.json_response({"ok": True, **self._dials()})

    async def _h_review(self, request):
        if not self.store:
            return web.json_response({"ok": False, "error": "no store"}, status=400)
        body = await request.json()
        action = (body.get("action") or "").strip()
        if action not in ("start", "later", "skip", "show"):
            return web.json_response({"ok": False, "error": "bad action"}, status=400)
        await self._act_review(action)
        return web.json_response({"ok": True})

    async def _h_nudge(self, request):
        body = await request.json()
        nid = (body.get("id") or "").strip()
        action = (body.get("action") or "dismiss").strip()
        await self._act_nudge(nid, action)
        return web.json_response({"ok": True})

    # Clickable [p.N] citations in the chat drive the reader to that page.
    async def _h_goto(self, request):
        body = await request.json()
        try:
            page = int(body.get("page"))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "bad page"},
                                     status=400)
        if self.total_pages:
            page = max(1, min(page, self.total_pages))
        cb = self.on_goto
        if cb is not None:
            try:
                cb(page)
            except Exception:
                log.exception("on_goto callback failed")
        return web.json_response({"ok": cb is not None, "page": page})

    # 📷 button: render the current page to PNG; the path rides the next
    # turn's reader-context line so Claude Reads the image (figures/diagrams
    # that text extraction misses).
    async def _h_pageimg(self, request):
        if not (self._can_image() and self.page):
            return web.json_response(
                {"ok": False, "error": "page image unavailable"}, status=400)
        page = self.page
        out_dir = os.path.join(self.store.book_dir, "pageimg")
        try:
            path = await self.loop.run_in_executor(
                None, pdftools.render_page,
                self.store.meta["source_path"], page, out_dir)
        except Exception as e:
            log.exception("page render failed")
            self._note(f"page render failed: {type(e).__name__}")
            return web.json_response({"ok": False}, status=500)
        if not path:
            return web.json_response({"ok": False}, status=500)
        rel = f"pageimg/{os.path.basename(path)}"
        self.pending_image = (rel, page)
        self._note(f"📷 image of PDF p.{page} will accompany your next "
                   "message")
        return web.json_response({"ok": True, "page": page})

    # Right-click actions from the reader (SumatraPDF SelectionHandlers,
    # installed by sumatra_settings.py). Prerelease Sumatra POSTs the JSON
    # body itself (Method = POST — no browser; our short reply shows as an
    # in-canvas notification). Stable releases (3.4 – 3.6.1) ignore the
    # Method field and GET the same URL with the selection URL-encoded into
    # ?t=, which opens a browser tab — the HTML response tries to close it
    # again. On known versions the shell's own menu means neither happens.
    async def _h_selection(self, request):
        action, text, note = None, "", None
        if request.method == "POST":
            raw = (await request.text()).strip()
            try:
                data = json.loads(raw)
                action = (data.get("a") or "").strip() or None
                text = (data.get("text") or "").strip()
                note = (data.get("note") or "").strip() or None
            except (json.JSONDecodeError, AttributeError):
                text = raw
        q = request.rel_url.query
        action = action or (q.get("a") or "").strip() or "copy"
        if not text:
            text = (q.get("t") or "").strip()
        if text == "${selection}":     # POST leaves the URL template unexpanded
            text = ""
        if not text:
            return self._sel_response(request, "nothing selected", ok=False)
        text = reflow_selection(text)
        if len(text) > SELECTION_MAX_CHARS:
            text = text[:SELECTION_MAX_CHARS] + " […]"
        msg, ok = await self._dispatch_selection(action, text, note)
        return self._sel_response(request, msg, ok)

    async def _dispatch_scope(self, verb, scope):
        """A verb on the current page / section / chapter (no selection):
        one grounded turn with the matching protocol tail."""
        allowed = {v: sc for v, _, sc in bookcontext.SCOPE_VERBS}
        if verb not in allowed or scope not in allowed[verb]:
            return f"unknown scope action {verb}/{scope}", False
        if not self.store:
            return "book not indexed — page/section/chapter actions need it", False
        page = self.page
        if not page:
            return "reader position unknown yet", False
        node = self.store.locate_page(page)
        chapter = self.store.chapter_of(node)
        if scope == "section" and node is None:
            return "no section here", False
        if scope == "chapter" and chapter is None:
            return "no chapter here", False
        summary_rel, sections = None, ()
        if scope == "chapter":
            sections = self.store.sections_of(chapter)
            summary_rel = await self._ensure_summary(chapter)
        lead, tail, run = scope_lead_and_tail(
            verb, scope, node, chapter, page, self.store.printed_label(page),
            sections=sections, summary_rel=summary_rel,
            challenge=self.current.get("challenge", 1))
        # explainback: Claude invites the explanation; the user's next
        # message gets the critique tail (see _h_ask)
        self._explainback = (_scope_where(scope, node, chapter, page,
                                          self.store.printed_label(page))
                             if verb == "explainback" else None)
        await self._enqueue_user(lead, tail=tail, new_chat=True, run=run)
        return f"{verb} on {scope}: asked Claude in a new chat", True

    async def _dispatch_selection(self, action, text, note=None):
        if action == "copy":
            if looks_mathy(text):
                text = await latexify(text) or text
            self._emit({"type": "compose", "text": text})
            if self.on_focus:               # user is about to type about it
                try:
                    self.on_focus()
                except Exception:
                    log.exception("on_focus callback failed")
            return "copied to chat", True
        if action == "highlight":
            if not self.store:
                return "book not indexed — run ingest.py to save highlights", False
            msg = await self.loop.run_in_executor(None, self._save_highlight,
                                                  text)
            return msg, True
        leads = bookcontext.VERB_LEADS
        if action not in leads:
            return f"unknown action '{action}'", False
        tail = VERB_TAILS.get(action)
        if action == "hint":
            tail = await self._hint_tail(text)
        shown = text
        if action == "formula" and looks_mathy(text):
            latex = await latexify(text)
            if latex:
                shown = latex
                tail = (tail or "") + (
                    "\n[the LaTeX above was reconstructed by a helper from "
                    "this raw PDF text-layer copy — trust the raw text where "
                    f"they disagree: {' '.join(text.split())[:600]}]")
        quoted = "\n".join("> " + ln for ln in shown.splitlines())
        if note:                            # the user's own words, if any
            quoted += f"\n\n{note}"
        fresh = action in bookcontext.TASK_VERBS
        run = (quiz_run("quiz", QUIZ_COUNTS["selection"])
               if action == "quiz" else None)
        await self._enqueue_user(f"{leads[action]}\n\n{quoted}",
                                 annot={"action": action, "text": text},
                                 tail=tail, new_chat=fresh, run=run)
        where = "a new chat" if fresh else "the sidebar"
        return f"{action}: asked Claude, reply lands in {where}", True

    async def _hint_tail(self, text):
        """Key the selection as a passage and look up how many hints it has
        already had (learning.json), for the graduated-hint protocol. No
        exercise numbering is assumed: Claude decides from the text whether
        the passage is an exercise at all, and names it in the trailer."""
        node, file = None, None
        if self.store:
            node, _, _ = await self.loop.run_in_executor(
                None, self._locate_selection, text)
            if node:
                file = node["file"]
        sec = node["id"] if node else None
        key = learning.passage_key(sec, text)
        known = self.learning.get("exercises", {}).get(key) or {}
        self._hint = {"key": key, "sec": sec, "label": known.get("label")}
        return hint_tail(file, learning.hint_level(self.learning, key),
                         known.get("label"))

    # ---------- annotation + journal (executor threads) ----------

    def _locate_selection(self, text):
        """(node, pdf_page, printed_label) for a selection, best effort."""
        node, page = None, self.page
        try:
            hit = self.store.locate_snippet(text)
            if hit:
                node, page = hit
            elif page:
                node = self.store.locate_page(page)
        except Exception:
            log.exception("locate_selection failed")
        printed = self.store.printed_label(page) if page else None
        return node, page, printed

    def _underline_pages(self, page):
        """Candidate pages for the text search: located page, viewed page ±1."""
        cands = [page, self.page,
                 (self.page or 0) + 1, (self.page or 0) - 1]
        return [p for p in dict.fromkeys(cands) if p and p >= 1]

    def _save_highlight(self, text):
        """'Save highlight' verb: journal entry + underline, no Claude turn."""
        node, page, printed = self._locate_selection(text)
        try:
            journal.append(self.store.book_dir, "highlight", text,
                           node=node, page=page, printed=printed)
        except OSError:
            log.exception("journal write failed")
            return "couldn't write journal.md"
        hit = None
        pdf = self.store.meta.get("source_path")
        if pdf and os.path.isfile(pdf):
            try:
                hit = pdftools.underline(
                    pdf, self._underline_pages(page), text,
                    f"Lantern highlight · {time.strftime('%Y-%m-%d')} — "
                    "saved to the learning journal (no chat) · Ctrl+click "
                    "for the page notes")
            except Exception:
                log.exception("underline failed")
        if hit:
            self._reload_reader()
        where = f" — {node['title']}" if node else ""
        pg = f", PDF p.{page}" if page else ""
        self._note_threadsafe(f"★ highlight saved{where}{pg}"
                              + (" (underlined in the PDF)" if hit else ""))
        if page:
            self.loop.call_soon_threadsafe(self._refresh_notes, page)
        return "highlight saved to journal" + (" + underlined" if hit else "")

    def _refresh_notes(self, page):
        if page == self.page and not self._notes_task:
            self._notes_task = self.loop.create_task(self._page_notes(page))

    def _reload_reader(self):
        """A fresh underline stays invisible (no hover, no Ctrl+click) until
        Sumatra re-reads the file, and the embedded reader never watches
        it: -plugin LoadDocument returns before the file-watcher subscribe
        (src/SumatraPDF.cpp). So the shell sends Sumatra's own Reload."""
        cb = self.on_reload
        if cb is not None:
            try:
                cb()
            except Exception:
                log.exception("on_reload callback failed")

    def _apply_annotation(self, annot, answer):
        """After a right-click ask is answered: journal it and underline the
        passage in the PDF. The annotation note is a one-liner — Sumatra's
        hover popup flickers, so the full Q&A lives in the sidebar's page
        notes and journal.md instead — ending in a [chat:<session>#<turn>]
        ref that Ctrl+click on the underline resolves (open_annotation)."""
        action, text = annot["action"], annot["text"]
        node, page, printed = self._locate_selection(text)
        try:
            journal.append(self.store.book_dir, action, text, answer=answer,
                           node=node, page=page, printed=printed)
        except OSError:
            log.exception("journal write failed")
        if page:
            self.loop.call_soon_threadsafe(self._refresh_notes, page)
        pdf = self.store.meta.get("source_path")
        if not (pdf and os.path.isfile(pdf)):
            return
        content = (f"Lantern · {action} · {time.strftime('%Y-%m-%d')} — "
                   "Ctrl+click to open the chat"
                   + (f" [chat:{annot['chat']}]" if annot.get("chat") else ""))
        try:
            hit = pdftools.underline(pdf, self._underline_pages(page),
                                     text, content)
        except Exception:
            log.exception("underline failed")
            return
        if hit:
            self._reload_reader()
            self._note_threadsafe(f"✎ underlined on PDF p.{hit} — Q&A saved "
                                  "to journal.md")

    def _sel_response(self, request, msg, ok=True):
        status = 200 if ok else 400
        if request.method == "POST":
            return web.Response(text=f"Lantern: {msg}", status=status)
        html = ("<!doctype html><meta charset='utf-8'><title>Lantern</title>"
                "<body style='font:14px Segoe UI,sans-serif;color:#3f3f46;"
                f"background:#fafafa;padding:24px'>Lantern: {msg} — you can "
                "close this tab.<script>setTimeout(()=>window.close(),300)"
                "</script>")
        return web.Response(text=html, content_type="text/html", status=status)

    async def _h_focus(self, request):
        cb = self.on_focus
        if cb is not None:
            try:
                cb()
            except Exception:
                log.exception("on_focus callback failed")
        return web.json_response({"ok": True})

    async def _h_events(self, request):
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        })
        await resp.prepare(request)
        q = asyncio.Queue()
        self._subs.append(q)
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(q.get(), 15)
                    await resp.write(f"event: {kind}\ndata: {payload}\n\n"
                                     .encode("utf-8"))
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, ConnectionAbortedError, RuntimeError):
            pass
        finally:
            if q in self._subs:
                self._subs.remove(q)
        return resp


def _selftest(message):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    engine = ChatEngine("The Elements of Statistical Learning",
                        greet=False, debug_print=True)
    port = engine.start()
    print(f"bus up on port {port}; asking: {message!r}")
    t0 = time.monotonic()
    engine.ask(message)
    while True:
        time.sleep(0.2)
        if engine.history and engine.history[-1]["role"] in ("claude", "meta") \
                and not engine.busy:
            break
        if time.monotonic() - t0 > TURN_TIMEOUT + 10:
            print("TIMED OUT")
            return 1
    last = engine.history[-1]
    print(f"\n--- reply ({last['role']}, {time.monotonic()-t0:.1f}s total) ---")
    print(last["text"])
    engine.shutdown()
    return 0 if last["role"] == "claude" else 1


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        sys.exit(_selftest(sys.argv[2]))
    print(__doc__)
