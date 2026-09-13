# Lantern

*(formerly readbuddy; renamed 2026-09-13. The icon is the Lantern relic from Slay the Spire.)*

Claude alongside the page. A learning companion that sits next to your existing eBook reader — it knows what page you're on and what you've selected, answers questions in context, and keeps a persistent map of your journey through each book.

## Vision

- **Context-aware Q&A** — ask about the current page/section without pasting anything; highlight text and ask for a different perspective, ELI5, an analogy, a worked example.
- **Learning journal per book** — a persistent record of concepts explored, questions asked, explanations that clicked, misconceptions corrected. The tutor reads it at session start and relates new material back to it.
- **Journey awareness** — rough model of progress through the book, used as a teaching tool ("this builds on the eigenvector intuition from ch. 3").

## Approach (decided 2026-08-27)

**Do not build a reader.** Lantern is a companion sidebar on top of existing readers (Calibre viewer, SumatraPDF, anything else), connected via lightweight adapters:

1. A small local server ("context bus") receives selections/positions from the reader.
2. Book text is extracted once per book (calibre CLI) so any text snippet can be located → chapter/section/position inferred without reader cooperation.
3. Claude (API / Agent SDK) answers with page context + learning journal loaded.
4. Sidebar UI: browser window docked beside the reader — or embedded *inside* Calibre's Lookup panel, which is itself a browser pointed at a configurable URL.

See `notes/2026-08-27-reader-integration.md` for the integration research and hook details.

## Running it (v0.1 shell, 2026-08-29)

`lantern.bat [path-to-pdf]` — with no path, the **library view** opens
(2026-09-08): every ingested book from `books/*/meta.json` with page N of M
(printed page + %), the section you were in, last opened, and journal size;
double-click / Enter resumes it, "Open PDF…" browses for anything else.

**First open of any book (2026-09-08): the shell indexes it itself** — pure
local text extraction with pymupdf (~1s for a PDF), so the "book text not
indexed" fallback now only appears when ingestion declines (scanned PDF: OCR
it first) or crashes. **EPUB / MOBI / FB2 work too**: pymupdf lays the ebook
out at a fixed 6×9in page (11pt body) and writes `books/<id>/book.pdf`,
which is what Sumatra opens — so the page box, `[p.N]` citations,
click-to-jump and underline annotations all share one pagination. The
original ebook is never modified (`meta.original_path`); the trade-off is
no reflow / font-size change in the reader. EPUB stylesheets get one fix on
the way in: `vertical-align` is stripped from `img` rules, which MuPDF
otherwise mis-sizes so body text prints over the figures. `app\ingest.py`
remains for `--force` rebuilds and `--summarize`.
Then one window: SumatraPDF embedded left (via `-plugin`), Claude chat sidebar right
(resizable via the sash, collapsible via the ❯ button). The sidebar is an Edge
`--app` window reparented into the pane, rendering `chat.html` (markdown + KaTeX,
streaming replies, model dropdown: Sonnet/Haiku/Fable). It talks to the context
bus (`bus.py`): a local aiohttp server holding **one persistent Claude session**
(claude-agent-sdk on the local Claude Code login — no API key, no per-message
cold start; ~2–3s replies vs ~60s in v0). The shell polls Sumatra's toolbar
page box, so Claude always knows the current page (shown in the chat header
and injected as reader context into every turn).

**Right-click in the reader is Lantern's** (2026-09-08, Sumatra 3.4 – 3.6):
the shell's low-level mouse hook swallows right-clicks on the PDF canvas and
pops its own native menu, so Sumatra's Copy/Search/Translate items never
appear. With text selected: a preview line, **Copy to chat / Explain /
Define this term / Unpack this formula / Hint for this exercise / Quiz me on
this / Save highlight**, then *Copy text*. Always: cascades for **This page /
Section … / Chapter …** with the scope verbs (Quiz me, Test out on
section/chapter — a placement test: grade word + page cite only between
questions, then a result line (passed = no wrong, at most one partial), an
answer key for every partial / wrong / guessed answer, a skip / skim / read
verdict per section with the reason, and a focus line; questions carry no
page links and summary / history sections are 'not tested', Summarise, Check my explanation on page/section, Refresh me on chapters — a
chapter quiz is the checkpoint protocol). Every quiz-like verb runs a fixed
count of questions (2 page / 3 selection or section / 4 checkpoint / 5
chapter test-out, stopping after 2 wrong) that the app tracks: the answer
tail tells Claude which question it is grading and whether to pose the next
one, and if a grading reply forgets to, the app asks for it. The menu is the
one home for verbs (simplified
2026-09-13: ELI5 and Try-first are the Style / Challenge dials, not verbs;
the sidebar's ✦ Actions panel is gone). Selection detection reuses the Ctrl+Shift+C
copy probe (Sumatra's "Select content…" toast for an empty selection is
hidden before it paints). Right-clicking the canvas again while the menu is
open closes it and reopens it at the new spot, so a rapid burst of clicks
ends with one Lantern menu and never Sumatra's (2026-09-13). Items call the
bus in-process — no browser bounce.
The same verb lists live in `bookcontext.SELECTION_VERBS` / `SCOPE_VERBS`.
On an unknown Sumatra version the hook stays off and the old route applies:
SelectionHandlers entries (auto-installed into `SumatraPDF-settings.txt`,
backup kept) appended to Sumatra's own menu, hitting the bus on fixed port
8378 — on stable releases through a self-closing browser tab, on prerelease
builds via a silent POST.

**Ctrl+Shift+C** (2026-09-08) sends the highlighted passage to the chat
without the menu: the shell hooks the chord while its window is in front,
asks Sumatra to run its own Copy Selection command, and quotes the
clipboard text into the composer (same path as *Copy to chat*, no browser
bounce). Works with the cursor in the reader or the chat. Sumatra's copy
command id is version-specific (`SUMATRA_COPY_CMD` in the shell: 3.4, 3.5,
3.6 — add the next release from its `src/Commands.h`); on an unknown version
the chord is left alone, the menu stays Sumatra's, and the chat says so.

Chat & sessions (2026-08-29): the sidebar holds **multiple chat sessions**
per book (dropdown + "+" under the header), each auto-titled from its first
exchange (one-shot haiku) and tagged with the chapter it started in. Sessions
persist in the book store — transcript, Claude-side memory (SDK resume id),
and learning dials — so on relaunch the last active chat is back on screen
and the reader jumps to the **last page you were on**. The header's **⚙**
opens the settings panel: model, the Challenge dial and the Style dial (see
Learning mode); the 📷 page-image button sits beside the composer.

Reader integration (2026-08-29, ingested books): asked-about passages and
saved highlights get a violet **underline annotation in the PDF itself**
(one-time `<pdf>.lantern-orig` backup; incremental save, then the shell
sends Sumatra its Reload command — the embedded reader never watches the
file itself). **Ctrl+click an underline to reopen the chat behind it**
(2026-09-13): the note ends in a `[chat:<session>#<turn>]` ref, the shell's
mouse hook reads the note Sumatra registered for its hover tip and the
sidebar switches to that chat and scrolls to the turn. A saved highlight has
no chat, so Ctrl+click opens the page notes instead; notes from before the
refs get the same treatment. `[p.N]` citations in Claude's
replies are **clickable and jump the reader to that page** (via Sumatra's
toolbar page box). Quiz questions that refer to a section, example, or
figure carry a `[p.N]` link to it as well. Settling in a new section is
silent (the old "▸ entering §…" line was removed 2026-09-13 — it pushed the
chat up while navigating); crossing into a new chapter still offers a
refresher / checkpoint. The 📷 button beside the composer renders the current page to PNG so
Claude can Read the figures/diagrams that text extraction misses — it rides
along with your next message.

Setup once: `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`
Requires: Python 3.10+ (tkinter), SumatraPDF in %LOCALAPPDATA%, `claude` CLI, Edge.
Flags: `--diag` (win32/latency log → %TEMP%\lantern_diag.log), `--tkchat`
(plain tk chat, no Edge), `--selftest "msg"` (send one message, exit 0/1).
Degrades gracefully: no Edge → tk chat; no claude-agent-sdk → claude -p fallback.

Learning mode (2026-09-08, see `notes/2026-09-08-learning-mode-plan.md` for
the evidence base): each chat has two **dials** (behind ⚙; simplified
2026-09-13 from three) — Style (Standard / Plain: everyday words, ~120 words,
terms glossed / Simple: ten-year-old register, ≤5 sentences, one analogy, no
notation) and a four-step Challenge dial (Just tell me / Offer a check /
Try first / Make me work). Level 0 never asks anything back; "just tell me"
wins at every level. The ⚙ settings you last had showing (dials + model)
persist in `books/prefs.json` and seed every new chat; each book open starts
a fresh chat (older ones stay in the dropdown). The old Intent dial, ELI7, Socratic and the sticky
Explain-back style are gone: Socratic is Challenge 3, and explain-back is
the right-click **Check my explanation** verb on a page or section (Claude
invites your own-words explanation, then critiques your next message
against the book's text and grades it).
Claude reports learning outcomes in a hidden trailer the bus strips and
records in `books/<id>/learning.json` (+ append-only `learning.jsonl`): quiz
grades with your **confidence rating** (pills above the composer, Alt+1/2/3;
a confident miss is flagged for priority review), try-first guesses, hint
levels, misconceptions. Concepts follow a 1/3/7/14/30-day ladder; due ones
appear as a **review card** at session start (after *Later*, a 🎯 badge in
the header brings it back; card and badge are never both shown) that
runs an interleaved retrieval quiz. Entering a new chapter offers **Refresh
me** (prerequisite briefing from a lazily written `summaries/NN.md`), **Test
out** (can I skip this chapter?) or a
**Checkpoint** quiz on the previous chapter — at most one unsolicited offer per
10 pages / 15 minutes. Right-click verbs: **Unpack this formula** (numbers →
words → analogy with mapping → symbols), **Hint for this exercise**
(graduated hints, never the answer unless asked); pretesting is Challenge
level 2, not a verb. The PDF
annotation note is a one-liner ending in the chat ref (Ctrl+click opens the
chat); the Q&A lives in the sidebar's "📝 notes on this page" line and
`journal.md`. `python app\learning.py export <book_dir>`
writes an Anki TSV; `ingest.py --summarize` writes all chapter summaries.

## Layout

- `README.md` — this file: vision, approach, layout
- `app/lantern_shell.py` — tk shell: window, Sumatra + Edge embedding, win32 fixes
- `app/branding.py` — app name, window/taskbar icon (`lantern.ico` / `lantern.png`), AppUserModelID
- `app/library.py` — no-args library view: ingested books with progress, pick one to resume
- `app/bus.py` — context bus: aiohttp server + persistent ClaudeSDKClient session
- `app/chat.html` — sidebar UI: markdown/KaTeX chat page served by the bus
- `app/sumatra_settings.py` — installs the right-click actions (SelectionHandlers)
  into Sumatra's settings file
- `app/journal.py` — per-book learning journal (`books/<id>/journal.md` appends + per-page parse)
- `app/learning.py` — learning record: trailer parsing/stripping, review ladder, `learning.json[l]`, Anki export
- `app/summaries.py` — per-chapter `summaries/NN.md` via a structured-output Haiku one-shot
- `app/pdftools.py` — runtime pymupdf: underline annotations + page-image renders
- `app/sessions.py` — chat-session records (`books/<id>/sessions/*.json`)
- `lantern.bat` — launcher (uses `.venv` if present)
- `requirements.txt` — claude-agent-sdk, aiohttp
- `log.md` — dated dev log, newest first
- `notes/` — research & design notes, one file per topic, date-prefixed
