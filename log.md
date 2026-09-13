# Lantern dev log

Newest first.

## 2026-09-13 — Settings persist into new chats; fresh chat per book open; Plain style

- **⚙ settings persist.** `books/prefs.json` (shared across books) mirrors the panel: written
  on a dial change, a model change, and a session switch (`_save_prefs`). Every new chat —
  "+", task chats (`_fresh_session`), and the one each book open starts — is built with
  `sessions.new_record(dials=self._pref_dials())` (validated against `STYLE_PROMPTS` /
  `CHALLENGES`; legacy ids mapped); `ChatEngine.__init__` also takes the prefs model when
  the caller passed the default. `_fresh_session` no longer copies dials by hand.
- **Each book open starts a new chat** instead of resuming `meta.active_session`. A leftover
  blank session (`sessions.is_blank`: no `you` turn) is reused rather than stacking "New
  chat" rows; its dials are reset to prefs. Its chapter label is stamped on the first
  message (`_enqueue_user`) since no page position exists at open.
- **Style: Standard / Plain / Simple.** Simple's mandatory analogy-plus-term-mapping was
  producing ~2 paragraphs with jargon-defined-by-jargon. Simple now has a hard budget (≤5
  short sentences / ~80 words, one analogy, "never define a word with harder words");
  Plain is the step in between (gist first, ~120 words, the book's terms kept but glossed,
  no notation unless asked). `LEARNING_RULES` (cached prefix — one-time cache miss) names
  all three and says the `[style: …]` line is the exact budget.

## 2026-09-13 — Ctrl+click a PDF underline → the chat behind it; explicit reader reload

- **Annotation notes carry a chat ref.** `_finish_turn` stamps the annotation with
  `chat=<session id>#<n>` (n = ordinal of that user turn among the session's `you`
  entries — stable across restarts because meta lines are never persisted) and
  `_apply_annotation` writes `Lantern · <verb> · <date> — Ctrl+click to open the chat
  [chat:…]` as the note. Save-highlight notes say `(no chat) · Ctrl+click for the page
  notes`. To the question "can an annotation exist without a chat?": yes — Save
  highlight underlines without a turn, so those go to the page notes instead.
- **Ctrl+click in the reader** (`_ll_mouse`): on Ctrl+left-down over the canvas the hook
  asks Sumatra what is under the cursor — Sumatra registers the hovered element's text
  (the note) as the single tool of its infotip window on every mouse move, tip visible
  or not — via `_annotation_tip` / `_tooltip_text`: `TTM_ENUMTOOLS` + `TTM_GETTEXT` with
  a `TOOLINFOW` in Sumatra's address space (VirtualAllocEx; ~0.2 ms). Only a note
  starting with `Lantern` is swallowed (down + up), so Sumatra's own Ctrl+drag rectangle
  selection and its 3.6 "Ctrl+click to edit" on foreign annotations keep working.
  The bus (`open_annotation` → `_open_annotation`) switches to the session (busy guard
  as usual), then emits `jump {turn}`; chat.html scrolls to that `.msg.you` and flashes
  it. Missing session → meta note; highlight / pre-ref note → `jump {notes:true}` opens
  the 📝 panel plus a meta note.
- **The embedded reader never auto-reloaded.** Sumatra's `LoadDocument` returns before
  the file-watcher subscribe in `-plugin` mode (src/SumatraPDF.cpp), so every fresh
  underline was invisible until the book was reopened — the README's "Sumatra
  auto-reloads" was the LaTeX-workflow behaviour of a normal window. Now
  `bus._reload_reader` → `shell.reload_reader` posts `WM_COMMAND CmdReloadDocument`
  (`SUMATRA_RELOAD_CMD`: 3.4 → 209, 3.5 → 212, 3.6 → 214; same page/zoom/scroll) after
  each successful underline. Only 3.6.1 (the installed one) is verified; 3.4/3.5 ids are
  derived from Commands.h the same way as the copy ids.
- Verified with a scratch-PDF e2e against the real shell (private diag log, port 8378):
  one `/ask` turn, a manual `[chat:<sid>#1]` underline + the app's own Save highlight
  (which triggered the reload; both underlines appeared ~3 s later), new session, then
  synthetic Ctrl+clicks: live ref → `session` + `jump {turn:1}` events, sidebar back on
  the first chat with the turn flashed (screenshot); dead ref → "no longer exists" note;
  highlight → `jump {notes:true}` + ★ note. Hook read time 0–2 ms. Not covered: a real
  right-click ask producing the ref (same `_apply_annotation` path, exercised by the
  highlight verb), Sumatra 3.4/3.5.

## 2026-09-13 — SumatraPDF 3.6.1; rapid right-clicks no longer leak Sumatra's menu

- **Installed Sumatra is now 3.6.1** (was 3.4.6). `SUMATRA_COPY_CMD` gains `"3.6": 234`:
  3.6's `src/Commands.h` numbers the enum explicitly (same id at the 3.6rel and 3.6.1rel
  tags), so no more counting the `COMMANDS(V)` list. The frame still routes it to
  `CopySelectionInTabToClipboard`. Verified live: diag log `hotkey: Ctrl+Shift+C ->
  WM_COMMAND 234 (Sumatra 3.6.1.0)`, right-click menu with/without a selection,
  Ctrl+Shift+C quoting into the composer, no Sumatra menu, no browser tab.
- **Toast class changed in 3.5.** Notifications are built on wingui's `Wnd` since 3.5 and
  register the shared default class `SumatraWgDefaultWinClass` instead of
  `SUMATRA_PDF_NOTIFICATION_WINDOW`. `SUMATRA_NOTIFY_CLASSES` + `is_notify_window()` (class
  in the tuple *and* parent is the canvas) replace the single constant, so the quiet
  probe still hides the "Select content…" toast before it paints (3–11 ms on 3.6.1; the
  window stays as a hidden child until Sumatra's 2 s timer destroys it).
- **Dropped the "3.6+ binds Ctrl+Shift+C itself" special case.** Stable 3.6 SelectionHandlers
  only have URL/Name/Key (Method/Body are prerelease-only), so the handler's Key would
  bounce through the browser; the shell's own hook wins on every known version. Comments in
  `sumatra_settings.py` / `bus.py` / README corrected to say so.
- **Rapid right-click leak fixed** (Blake: "occasionally when right clicking rapidly, the old
  menu does appear"). Cause: `_on_canvas` stopped swallowing while our menu was up so a
  click outside could dismiss it, but `_menu` is set before tk has built/posted the native
  menu, and a right-down in that gap (or during teardown) reached Sumatra, which starts a
  right-drag on the down and shows its menu on the up (`OnMouseRightButtonUp` ignores an
  up without the down — verified in 3.4.6 source). Now: canvas right-clicks are swallowed
  even while our menu is open (`_on_canvas(pt, menu_ok=True)`), `_cancel_menu()` posts
  `WM_CANCELMODE` to every top-level window of the tk thread (the popup's owner is tk's
  hidden menu window, so root alone isn't enough) which ends `tk_popup`'s modal loop, and
  the button-up re-probes so the menu reappears at the new spot. A click generation
  (`_click_gen`) plus `_probe_lock` mean a burst ends with one menu at the last click
  instead of queued menus. Live: 8 clicks 60 ms apart alternating two canvas points → 8
  probes, 0 Sumatra menus, one menu at the last point (before: 1 Sumatra menu in 8).
  Ctrl+left annotation clicks keep the old `_on_canvas` semantics.
- Harness notes: the tk loop stalls 2–8 s when a harness forces our window to the
  foreground (`SetWindowPos` TOPMOST + `SetForegroundWindow`; the focus watch's
  AttachThreadInput path), so a first-click menu check must wait that out; a peer session's
  test window covering ours makes right-clicks vanish (guard with `WindowFromPoint`).

## 2026-09-13 — UI de-duplication: one verb home, two dials, two button shapes

- **Verbs live in the right-click menu only.** The sidebar's ✦ Actions panel (passage
  scope + page/section/chapter pills + verb chips) is gone, with its `/scope` route and the
  `loc` / `grounded` / verb lists in `/state`. The menu keeps passage verbs on a selection
  plus the This page / Section / Chapter cascades. Ctrl+Shift+C still quotes into the composer.
- **Fewer verbs.** Passage: Copy to chat / Explain / Define this term / Unpack this formula /
  Hint for this exercise / Quiz me on this / Save highlight. Scope: Quiz me / Summarise /
  Check my explanation (page, section) / Refresh me (chapter). Dropped as verbs: ELI5 (now the
  Style dial), Try first (Challenge 2), scope-level Explain (≈ Summarise).
- **Dials: three → two.** Intent (understand/retain) removed — its only effect was a reminder
  line plus digest gating that Challenge ≥ 1 already covers. Style is now Standard / Simple
  (ELI5 + ELI7 merged); Socratic dropped in favour of Challenge 3; Explain-back is no longer
  a sticky style but the `explainback` scope verb: Claude invites the own-words explanation,
  and the bus attaches the critique tail (`explainback_tail`) to the user's next `/ask`
  (`_explainback` pending state; cleared on a manual session switch). Old session records
  are mapped on load (`sessions.LEGACY_STYLES`; `intent` key dropped). The `[dials: …]` line
  and `LEARNING_RULES` no longer mention intent. Legacy `/mode` endpoint removed.
- **Chrome.** Model + dials moved behind a ⚙ settings panel; the header keeps the 🎯 badge
  (only shown after *Later*, as the way to bring the card back — card and badge never
  coexist) and a 📝 N notes badge that opens the notes panel. 📷 moved down beside the
  composer where the ✦ was. Top chrome is now header + session row.
- **CSS.** Two shapes: `.btn` (rect) and `.btn.pill`; `.badge` and `.btn.mini` retired
  into `.pill`; `.seg` and `.chip` kept. `.tog` for the 30px icon buttons.
- Verified: py_compile + imports of bus / shell / ask, `node --check` on the page script,
  and the page rendered at 430px in headless Edge against a stub bus (header badges, ⚙
  panel with all three rows, review card, quiz bar, 📷 + composer all in place). Not yet
  exercised in the running app (two live instances were up); the right-click cascades
  and the explain-back flow need a real session.

## 2026-09-13 — Renamed to Lantern

- **readbuddy → Lantern** everywhere in code, README, launcher (`lantern.bat`, now
  `%~dp0`-relative so the folder can move), module `app/lantern_shell.py`, class
  `LanternShell`, logger names `lantern.*`, Claude system prompt, PDF annotation text,
  Anki tag, diag log `%TEMP%\lantern_diag.log`, Edge profile `%LOCALAPPDATA%\lantern\`.
- Backup suffixes are `.lantern-orig` / `.lantern-bak`; an existing `.readbuddy-*` backup is
  renamed in place rather than re-copied, so the "pristine" copy stays pristine.
- **Icon**: `app/lantern.ico` (16–256px DIB entries, tk-parseable) + `app/lantern.png`, built
  from the Slay the Spire Lantern relic art. `app/branding.py` applies it to both tk roots and
  sets an AppUserModelID so the taskbar groups as Lantern, not pythonw.
  Rebuilt later the same day so the lantern fills the canvas: cropped to the visible body
  (alpha>128) with no margin (was ~85% of the height, now ~99%), each size resampled straight
  from its source (game sprite `images/relics/lantern.png` from the StS jar for ≤64px, the
  larger wiki render for 128/256) with Lanczos + a mild unsharp mask at ≤48px.
- Not done: the folder is still `D:\projects
eadbuddy` (two live instances + Positron had it
  open; the venv is in use, so the directory can't move). Dated notes/ keep the old name.

## 2026-09-08 (late) — Right-click is ours; Actions panel; page/section/chapter verbs

- **Context-menu takeover** (`readbuddy_shell.py`): a `WH_MOUSE_LL` hook on the
  existing hotkey thread swallows `WM_RBUTTONDOWN/UP` when the point is over
  `SUMATRA_PDF_CANVAS` (or a Sumatra toast) inside our root; the menu is a
  native `tk.Menu` popped on button-up unless the mouse moved (right-drag →
  nothing, like Windows). Sumatra never sees the click, so Translate with
  Bing & co. are gone. Selection detection = the Ctrl+Shift+C copy probe
  (`_grab_selection`, refactored out of the hotkey worker) with `quiet=True`:
  the no-selection "Select content with Ctrl+left mouse button" toast is
  found by class and `ShowWindowAsync(SW_HIDE)`d as soon as it exists (~3–9ms,
  before Sumatra paints it — verified: no toast in screenshots). Menu: preview
  line + `bookcontext.SELECTION_VERBS` + Copy text, then This page / Section /
  Chapter cascades with `SCOPE_VERBS`. Unknown Sumatra version → hook off,
  SelectionHandlers remain the fallback. Side effect to know: a right-click
  with a selection puts the selection on the clipboard (that is the probe).
- **Scope verbs** (`bus.scope_lead_and_tail`, `POST /scope`, `engine.send_scope`,
  `engine.location()`): quiz / summary / explain / eli5 on the current page or
  section, quiz (= checkpoint protocol) / summary / refresh on the chapter.
  Page scope points Claude at the `[p.N]` anchor inside the section file;
  chapter scope writes `summaries/NN.md` lazily first. `/state` now carries
  `loc`, `grounded`, `selection_verbs`, `scope_verbs`; the `page` SSE event
  carries `loc`. `/sel` accepts an optional `note` (typed text under the quote).
- **Actions panel** (`chat.html`): ✦ button in the input row toggles a panel
  above the composer — scope segments (Passage / p. N / §x.y / Ch. N) and verb
  chips; the `compose` event (Ctrl+Shift+C / Copy to chat) opens it on
  Passage; Esc closes; chips post `/sel` or `/scope` and clear the composer.
- **Stale sidebar fix**: Edge's app window kept a heuristically cached
  `chat.html` per origin across restarts — the test pane showed the pre-learning
  UI (no dials, no quiz bar) until the URL changed. The bus now serves `/`
  with `Cache-Control: no-cache` and the shell appends `?v=<chat.html mtime>`.
  If the dials/quiz bar ever looked missing in a real session, this was why.
- `sumatra_settings.ACTIONS` now imports `bookcontext.SELECTION_VERBS`.
- Verified live (scratch store `books/ztest-menu` on a copy of RL.pdf, removed
  after; harness in the session scratchpad): no-selection right-click → 3-item
  scope menu, no Sumatra menu, no toast; Chapter ▸ Summarise → `summaries/05.md`
  written, grounded summary in 8.4s; drag-select → 16-item menu → Quiz me on
  this → q1 + ask trailer + underline; `/sel` with note → note appended after
  the quote and answered; `/sel copy` → panel opens on Passage with 8 chips;
  chip click → turn, composer cleared, quiz bar shown. Harness lessons: other
  sessions launch shells concurrently, so park the test window at (0,0)
  TOPMOST and assert `WindowFromPoint` is ours; tk menus are owner-drawn
  (`GetMenuString` blank — pick by `GetMenuItemRect` index); redirect `TEMP`
  for a private diag log.

## 2026-09-08 (later) — Auto-ingest on open; EPUB / MOBI / FB2 via fixed-layout PDF

- Blake opened an EPUB and got the "book text not indexed — run ingest.py"
  meta line. Ingest is pure pymupdf (no Claude calls, <1s for a 350pp PDF),
  so the shell now runs `ingest.ingest()` itself when `find_for_pdf` misses
  (`_auto_ingest` in `readbuddy_shell.py`; title bar says "indexing …" while
  it runs, stdout captured into the diag log, result posted as a meta line).
  Scanned PDFs / crashes fall back to the old general-knowledge notice with
  the reason.
- EPUBs can't be used directly: Sumatra reflows them at its own layout, so its
  page N ≠ pymupdf's page N and every page-keyed feature (citations, goto,
  section detect, annotations) would point at the wrong place; Sumatra also
  can't annotate an EPUB. Instead `ingest.convert_reflowable()` lays the book
  out at 432×648pt / 11pt and `convert_to_pdf()`s it into
  `books/<id>/book.pdf` (outline + title/author carried over; `<id>` hashes
  the *original* file so re-opening the EPUB finds the same store).
  `meta.source_path` = that PDF (what the reader opens, where underlines go),
  `meta.original_path` = the ebook; `find_for_pdf` matches either. Library
  file picker now lists *.pdf *.epub *.mobi *.fb2. No calibre dependency.
- MuPDF layout bug found on Statistical Rethinking (CRC Press EPUB): with the
  stylesheet's `img{max-width:100%;max-height:100%;vertical-align:middle}`
  every figure was drawn full width but the flow reserved only a text line,
  so captions/body printed over the pictures (345 of 598 pages). Culprit is
  `vertical-align` (not the max-* rules; `fz_set_user_css` can't override
  document CSS). `_sanitize_epub` rewrites a temp copy dropping
  `vertical-align` from img rules / inline img styles → 1 residual overlap.
  First version of that regex backtracked catastrophically on large XHTML
  (hung the CLI); now scans CSS rules linearly and only touches `<style>`
  blocks and `<img>` tags in XHTML.
- Verified: CLI ingest of the EPUB in 4.9s (598pp, 126 sections, embedded
  TOC); shell auto-ingest in 4.8s with the meta note; converted PDF figure
  page renders cleanly; RL.pdf re-ingests unchanged.

## 2026-09-08 — Learning mode: dials, trailer signals, learning record, quiz confidence, review queue, nudges, new verbs, summaries

Plan and evidence base: `notes/2026-09-08-learning-mode-plan.md`. Everything in
it shipped in one pass except the fiction backlog.

- **Three dials per session** (`sessions.py`: `style` — the old `mode`, migrated
  on load — plus `intent` understand|retain and `challenge` 0-3). `POST /dials`,
  `dials` SSE event, `/state.dials` + `dial_options`; `/mode` kept as a shim.
  The rules live once in `bookcontext.LEARNING_RULES` (in BOTH system prompts,
  cached); only a `[dials: …]` line, short reminders for challenge 2/3 and
  intent=retain, the style prompt and a `[learning digest: …]` ride the tail
  (`bus.tail_parts`, shared with `ask.py --dials`).
- **Hidden trailer signals.** Claude ends learning-relevant replies with
  `<!--rb {"ev":…}-->`; `learning.TrailerFilter` strips it from the delta
  stream (held across chunk boundaries), `learning.parse_trailer` from the
  final text (tolerant JSON repair). Events: quiz, ask, pretest, hint, explain,
  misconception. The SDK's `output_format` was rejected for the persistent
  session (whole reply becomes JSON) but is used for chapter summaries.
- **Learning record** `books/<id>/learning.jsonl` (append-only) + `learning.json`
  (derived, rebuildable): concepts with a fixed-ladder schedule (1/3/7/14/30 d,
  advance on a correct recall on a new day, reset on a miss, mastered after 3
  correct once past the 7-day box), hypercorrection flag on certain+wrong,
  exercises with hint history. `python app\learning.py show|export|rebuild`.
- **Quiz with confidence.** An `ask` trailer sets the session's pending question;
  the sidebar shows Guess / Fairly sure / Certain pills (Alt+1/2/3) above the
  composer; `/ask {confidence}` (or `/confidence`) is bus-authoritative; the
  grading `quiz` trailer clears it, chips the graded You message (persisted on
  the history entry), journals a `quiz` line, flags high-confidence misses.
  Quiz protocol overrides style (Socratic still grades) and challenge (no
  pretest inside a quiz — the bus also ignores a stray pretest flag there).
- **Try-first pretesting.** Challenge 2/3, or the new right-click verb: Claude
  asks for a guess (ask trailer `pretest:true`) → the bus emits a `pretest`
  nudge with a "Just tell me" button; the next turn explains and emits a
  `pretest` trailer.
- **Spaced review at session start.** `_worker` loads learning.json; due items
  (priority first, most overdue, interleaved across chapters) become a `review`
  card that takes the greeting's slot; Start → interleaved retrieval quiz with
  the review tail; Later snoozes 4 h; 🎯 badge in the header re-shows it.
- **Budgeted chapter nudges.** `_checkin` now tracks the chapter; moving forward
  into a new one (≥10 pages or ≥15 min since the last offer) emits a `nudge`
  with Refresh me (+ Checkpoint on the previous chapter when it has activity).
  Refresh/checkpoint turns first create `summaries/NN.md` lazily (Haiku one-shot,
  Read only, JSON-schema output; `ingest.py --summarize` batch-writes them).
- **New verbs** (9 SelectionHandlers): Try first (pretest), Unpack this formula
  (concreteness fading, stages compressed by challenge/mastery), Hint for this
  exercise (graduated hints 1..3; exercise id from the selection or the section
  file via `BookStore.exercise_before`; level from learning.json).
- **Hover fix.** The PDF annotation note is now a one-liner (Sumatra's native
  popup flickers); the Q&A shows in the sidebar as a collapsible "📝 N notes on
  this page" line (`notes` event, `journal.entries_for_page`).
- Verified (no test suite by decision): py_compile all modules; chat.html script
  node-parsed; two headless live runs on a scratch copy of the RL store with
  haiku on an ephemeral port — trailer never leaked into deltas or the final
  text; quiz → pending q → certain+wrong → `hc:true`, chip, journal line;
  challenge-2 explain → guess question + pretest nudge → pretest trailer;
  backdated due items → review card n=2 (priority first) → Start → q1 pending;
  ch.6→ch.7 page move → chapter nudge → Refresh → summaries/07.md written via
  structured output → grounded refresher. Untested by hand: the Edge pane
  layout at 430px (learnRow fit, quiz bar, notes panel), Alt+digit in Edge.
- Live instance untouched (scratch store in %TEMP%, removed). Restart to pick
  everything up; the first launch re-installs the 9 right-click handlers.

## 2026-09-08 — Ctrl+Shift+C: send the highlight to chat

Keyboard route for "Copy to chat" — no right-click, no self-closing browser
tab on 3.4.6.

- Sumatra 3.4/3.5 have no user-bindable shortcuts (`Shortcuts` arrived in
  3.5 for built-ins only; per-handler `Key` is 3.6+), so the shell owns the
  chord: a **WH_KEYBOARD_LL hook** on its own pumping thread
  (`_hotkey_thread`), swallowing Ctrl+Shift+C only when `GetForegroundWindow`'s
  root is our toplevel — reader or embedded chat focused, never other apps.
- The copy is Sumatra's own: **`WM_COMMAND CmdCopySelection` to its frame**
  (what its Ctrl+C accelerator sends → `CopySelectionInTabToClipboard`). Ids
  computed from `src/Commands.h` at the release tags: 3.4.6rel = 228,
  3.5.2rel = 232 (`SUMATRA_COPY_CMD`, keyed by the exe's VERSIONINFO
  major.minor). Unknown version → hotkey off + meta note, never a stray
  command. Rejected: firing the SelectionHandler's own id — 3.4.6 assigns
  `sh->cmdID` only while building the context menu (null deref before the
  first right-click) and it still bounces through the browser.
- Clipboard handshake: `GetClipboardSequenceNumber` before/after (no
  sentinel writes), `OpenClipboard` retried while Sumatra still holds it,
  CRLF → LF, then `ChatEngine.send_selection("copy", text)` — the same
  `_dispatch_selection` as `/sel`, so truncation, the `compose` SSE event and
  the focus hand-off to Edge all apply. Tk-fallback chat gets the quote
  inserted directly. Nothing selected → clipboard unchanged within 0.8s →
  no-op (Sumatra shows its own "Select content with Ctrl+left mouse" hint).
- Settings installer adds `Key = Ctrl+Shift+C` to the Copy-to-chat entry
  for 3.6+, where Sumatra binds it natively and the hook stays off.
- Verified end-to-end on the real 3.4.6 inside the embedded shell: posted
  mouse-drag on the canvas (Sumatra only starts a text selection when the
  mouse-down is on a glyph; elsewhere it drag-scrolls), SendInput
  Ctrl+Shift+C → log `hotkey: 212 chars -> chat`, passage quoted in the
  composer. With nothing selected Sumatra's own "Select content…" toast
  appears and the chat is untouched.
- Test-harness gotchas: `.venv\Scripts\python.exe` is a launcher whose
  *child* python owns the tk window (find it by title, kill with
  `taskkill /T`); Git Bash rewrites `/F` into a path — use PowerShell
  `Stop-Process` from there.

## 2026-09-08 — Library view (no-args launch)

`readbuddy.bat` with no PDF now opens `app/library.py` instead of hardcoding
RL.pdf: a ttk table of every ingested book, most recently opened first.

- **`bookstore.library()`** scans `books/*/meta.json` (+ toc.json) into one summary
  per book: `last_page` → "p. 25 of 352 (book p. 11)  7%" plus the deepest TOC
  section at that page; journal.md entry count (`## ` headers) and size; session
  count; whether `source_path` still exists.
- **`meta.last_opened`** is stamped by the shell on every launch (new
  `BookStore.touch_opened()` / `save_meta()`; the bus's `_save_meta` now delegates to
  it, so its later writes carry the stamp through). Pre-existing stores fall back to
  the newest session's `updated`, then meta.json's mtime — so the RL store showed
  "10 days ago" correctly on first run.
- Double-click / Enter / **Resume** returns the PDF path to `main()`, which then
  builds the shell exactly as before; **Open PDF…** browses for any file (an
  un-ingested one still gets the "not indexed" notice). A book whose PDF moved is
  greyed "(PDF missing)" and refuses to open with a hint to browse. Esc/close exits 0.
- `DEFAULT_PDF` is gone; pass a path to skip the library.

## 2026-08-29 (late night) — Chat & sessions: multi-session, auto-titles, relaunch memory, tutoring modes

The whole TODO "Chat & sessions" section (new `app/sessions.py`; bus/shell/chat.html):

- **Multiple chat sessions, Claude-GUI style.** One JSON per session under
  `books/<id>/sessions/` holding transcript (you/claude only — meta notices are
  ephemeral), sdk resume id, tutoring mode, starting chapter, timestamps. The bus's
  `history` is now the *current* session's list, swapped wholesale on switch; the SDK
  client is disconnected and lazily reconnected with the new session's `resume` id, so
  each chat keeps its own Claude-side memory. Sidebar: session dropdown + "+" button
  (disabled while busy; switching is refused mid-reply). Non-ingested books get
  in-memory sessions that simply don't persist.
- **Auto-titles.** After a session's first real exchange, a one-shot haiku client
  (same minimal-options recipe as the main engine: no tools, no MCP, no settings)
  names it in 2-5 words; fallback is the truncated first message. Dropdown shows
  "Title · ch.N" from the chapter the session started in (TODO's "auto-scope" idea).
  Measured: "In one sentence: what is a reward signal?" → "Reward Signal
  Fundamentals · ch.5".
- **Relaunch memory.** `meta.json.last_page` saved once the page holds still 5s (and
  at shutdown); on launch the page-poll thread's first read drives Sumatra back there
  via `goto_page` (2 attempts, transit pages not reported — so the section check-in
  seeds at the *restored* position, no spurious "entering §…"). `active_session` is
  resumed with its transcript rendered; greeting is skipped for any session that has
  history or a resume id (no re-hello every launch). Legacy `meta.session_ids.chat`
  migrates to an "(earlier chat)" record on first run.
- **Tutoring modes.** Standard / ELI5 / ELI7 / Socratic / Feynman explain-back, per
  session, persisted. Mode instructions ride the volatile per-turn tail next to
  reader context — the cached system prompt never changes, so switching is free.
  Feynman treats the user's message as their explanation of the current section and
  critiques it against the actual text (Read via existing retrieval). `_reader_context`
  refactored into a parts list: [reader line] + [attachment] + [mode] + prompt.
- Verified: 21-check headless suite on scratch stores (record save/load with meta
  filtering, legacy migration, mode injection on/off, new-session chapter capture,
  live turn + auto-title landing, switch-back, debounced last_page, cold relaunch
  restoring 2 sessions + active + no-greet) plus a live shell run: launch → Sumatra
  driven to stored p.42, both sessions listed. chat.html script block node-parsed.
  Untested by hand: the dropdown/mode UX feel in the Edge pane.
- Blake's running instance untouched (ephemeral ports, scratch stores; temp
  `books/ztest-restore` removed after the live test). Restart to pick everything up.

## 2026-08-29 (night) — Reader integration: annotations, clickable cites, new verbs, check-ins, page images

The whole TODO "Reader integration" section, in one pass (new `app/pdftools.py`,
`app/journal.py`; ~200 lines across bus/shell/chat.html/bookcontext):

- **PDF annotation of asks & highlights.** Right-click asks (ELI5/Explain/Define/Quiz)
  now, after the answer lands, underline the selected passage in the *actual PDF*
  (violet, pymupdf `search_for` on located page ±1) with "You asked (…): …\n\nClaude: …"
  as the annotation note, and append passage+answer+location to the book's journal.
  Safety: one-time `<pdf>.readbuddy-orig` backup before the first write; incremental
  save only (`can_save_incrementally` guard — never a full rewrite); Sumatra doesn't
  lock the file and auto-reloads (the LaTeX workflow). Untested visually: whether
  3.4.6 shows the note popup on hover — the Q&A is in journal.md regardless.
- **Learning journal exists** (`books/<id>/journal.md`, append-only markdown, entries
  tagged `[§node-id]` for counting/grep). It lives inside the retrieval cwd, and the
  grounding prompt tells Claude to consult it — "what have I highlighted?" now works.
- **Clickable citations.** Grounding rules now demand a `[p.N]` PDF-page cite alongside
  printed pages; chat.html linkifies `[p.N]` / "PDF p. N" in rendered replies →
  POST `/goto` → shell drives Sumatra by `WM_SETTEXT` + `VK_RETURN` into the toolbar
  page box (same proven Edit hwnd the page poll reads — no DDE required, and it works
  in `-plugin` mode). Live-verified: /goto 148 → page poll reports 148.
- **Three new right-click verbs** (SelectionHandlers now 6 entries): *Define this term*
  (system prompt: grep for the first real definition, quote + cite), *Quiz me on this*
  (2-3 questions, ONE at a time, graded against the text), *Save highlight* — instant
  journal entry + PDF underline, no Claude turn, sidebar note `★ highlight saved — §…`.
- **Chapter-boundary check-ins.** Page poll → `locate_page`; when the section id
  changes and survives a 4s settle (skim guard, timer task cancelled on further
  movement), an ambient meta line lands in the sidebar: `▸ entering 5.6 Incremental
  Implementation (ch: 5 Monte Carlo Methods) — 2 journal notes here`. Deterministic
  (toc + journal count only, zero tokens); first page report seeds silently.
- **Page-image attachment.** 📷 header button (shown when store + SDK + pymupdf) →
  `/pageimg` renders the current page to `books/<id>/pageimg/p-N.png` (≤1600px, 2x);
  the next turn's reader-context line tells Claude to Read it — figures/diagrams
  reach the model, once, without polluting later turns.
- pymupdf is now a lazy runtime import (`pdftools`), not ingest-only; everything
  degrades (no pymupdf → no underlines/renders, journal still works).
- Verified: 26-check headless suite on scratch copies of RL.pdf + store (underline hit
  + backup + note content, render size, journal counts, check-in fired incl. correct
  §5.6 for PDF p.148 / skim suppression, goto clamp, pageimg attach-once, unknown-action
  400, live ELI5 end-to-end: answer in 5s, journaled with answer, 3rd annot with Q&A
  note) + live goto test against a real embedded Sumatra. Untestable headlessly: menu
  clicks, annotation visuals, check-in feel during real reading.
- Blake's running instance kept port 8378 throughout; tests ran on ephemeral ports and
  scratch files. New verbs/features need an app restart to go live.

## 2026-08-29 (evening) — right-click actions in the reader (Copy to chat / ELI5 / Explain)

- No hijacking needed: Sumatra's **SelectionHandlers** setting natively adds entries
  to the text-selection context menu, and (code-verified in 3.4.6rel source) the
  context menu is NOT suppressed in `-plugin` embed mode. New `app/sumatra_settings.py`
  idempotently (re)installs our three entries into `SumatraPDF-settings.txt` before
  each Sumatra launch (one-time `.readbuddy-bak` backup; foreign entries preserved;
  our entries recognized by the `127.0.0.1…/sel` URL and replaced wholesale, so port
  changes/field upgrades self-heal — necessary anyway because 3.4.6 strips fields it
  doesn't know when it rewrites the file on exit).
- Bus now prefers **fixed port 8378** (static menu URLs must find it; ephemeral
  fallback if taken → meta notice that clicks land in the other instance) and serves
  `/sel` on GET+POST: `copy` → SSE `compose` event, chat.html quotes the passage into
  the composer and pulls focus; `eli5`/`explain` → enqueued as a normal user turn
  (`ELI5 this passage:\n\n> …`), so reader-context/page injection applies. System
  prompt told to expect those two shapes.
- **Version split discovered reading master source**: 3.4/3.5 force the handler URL
  to http(s) and open the *default browser* (GET, selection URL-encoded, `\n`-joined)
  — our GET response is a self-closing tab. Master/3.6-prerelease adds `Method = POST`
  (+ `Body` with `${selectionjson}`, `ContentType`, `Headers`): **Sumatra POSTs the
  JSON itself via WinHTTP — no browser** — and shows our ≤300-char plain-text reply
  as an in-canvas notification. Entries carry both transports; installed 3.4.6 uses
  GET today, upgrading Sumatra to prerelease flips to silent POST with zero code
  change. (Prerelease also has per-handler `Key` shortcuts + selection-toolbar
  buttons + `Exe` handlers — future candy.)
- Verified: patcher idempotency/no-block/foreign-entry/fresh-file cases; 10-check
  integration test on `/sel` (GET+POST, template-literal and empty guards, unknown
  action 400) incl. a live ELI5 turn end-to-end (real Claude reply); full
  `--selftest` OK in 2.5s with handlers auto-installed on the real settings file.
  Untestable headlessly: the physical right-click → menu click, needs a human run.

## 2026-08-29 — v0.1: the three v0 pain points fixed

- **(c) 60s replies → ~2–3s.** Root cause: every message spawned a cold `claude -p`
  inheriting the *global* config — `claude-fable-5[1m]` at `xhigh` effort, plus the
  r-mcptools MCP server (Rscript) booting per spawn. Fix: new `app/bus.py` context bus
  holds one persistent `ClaudeSDKClient` (claude-agent-sdk, subscription login) for the
  whole app run: `model=sonnet`, `effort=medium`, `setting_sources=[]`, `mcp_servers={}`,
  `strict_mcp_config`, `max_thinking_tokens=0`, streaming deltas. Measured: first token
  1.8–1.9s, full reply 2.6–2.9s. Turns queue (input never locks); warm greeting turn
  primes the session at launch. Fallback to `claude -p --model sonnet` if SDK missing.
- **(b) markdown + LaTeX.** Sidebar is now `app/chat.html` served by the bus and rendered
  in an Edge `--app` window reparented into the tk pane (same trick as Sumatra; dedicated
  `--user-data-dir` profile so the HWND is findable by PID). marked.js + KaTeX (math spans
  stashed behind private-use placeholders so markdown can't mangle `_`/`*` inside `$...$`),
  streaming cursor, SSE live updates, **model dropdown** (Sonnet/Haiku/Fable — live switch
  via `ClaudeSDKClient.set_model`, session kept; verified Haiku replies in ~0.7s).
  `--tkchat` keeps the old plain tk chat as fallback (also auto if Edge missing).
- **(a) dead sidebar input for ~1min.** Diagnosed (Plan-agent + live confirmation): the
  cross-process `SetParent` from `-plugin` *attaches the two threads' input queues*, so
  input serializes behind Sumatra's busy UI thread during doc load (H1) — `--diag` run
  logged the smoking gun: `GetFocus()` on the tk thread returning a Sumatra-PID HWND.
  Synchronous `MoveWindow` on every `<Configure>` also blocked the tk loop on Sumatra's
  thread (H2) — frantic resizing made it worse. Fixes: `SetWindowPos` with
  `SWP_ASYNCWINDOWPOS|SWP_NOACTIVATE` + size cache; 40ms trailing-edge resize debounce;
  PID-verified child discovery (FindWindowExW's first-child could match a tk HWND);
  bounded 15s focus retry that yields on first user click; children launch on `<Map>`
  not `after(50)`. In reserve if input still dies: `AttachThreadInput(...,False)` detach,
  then a dedicated always-pumping proxy-host window for Sumatra (~60 lines).
- New: `.venv` + `requirements.txt` (claude-agent-sdk, aiohttp); bat prefers venv pythonw;
  under pythonw all `Popen`s default to CREATE_NO_WINDOW (SDK's claude.exe would flash a
  console). `--diag` instruments mainloop stalls, focus/foreground transitions, Sumatra
  responsiveness edges, resize timings → `%TEMP%\readbuddy_diag.log`. `--selftest` now
  exits 0/1 for CI-ish checks. Verified end-to-end: selftest OK in 3.1s; /state, /ask+SSE,
  /model switch integration tests all pass.
- Next milestone unchanged: real page context (UIA poll of Sumatra's page box / DDE-driven
  nav) + the learning journal, both natural residents of the now-existing bus.

## 2026-08-29 (later) — first-use feedback fixes

- **Keystrokes leaked to Sumatra while typing in the chat pane** (page flips, colour
  inverts). Cause: a cross-process reparented Chromium window never receives a proper
  activation, so clicking the web page doesn't move win32 keyboard focus — it stayed on
  Sumatra. Fix: chat.html POSTs `/focus` on mousedown/load (bus → shell callback), and the
  tk thread hands focus to Edge's `Chrome_RenderWidgetHostHWND` (AttachThreadInput dance as
  fallback). Verified in diag log: focus lands on the render host.
- **Edge's self-drawn title bar (min/max/close) could minimise the pane into the void.**
  Styles `WS_SYSMENU|WS_MINIMIZEBOX|WS_MAXIMIZEBOX` stripped; chrome measured off the
  render-host rect (30px bar + 7px borders at current DPI) and clipped outside the pane by
  offsetting the child window; a 50ms pump restores (ShowWindowAsync) if it ever goes
  iconic/zoomed. Sidebar fold/unfold stays on the tk ❯ / ❮ Chat arrows.
- Default book → `RL.pdf`; `BOOK_ALIASES` maps the stem to "Reinforcement Learning: An
  Introduction (Sutton & Barto, 2nd ed.)" so the system prompt names the real book.

## 2026-08-29 (later still) — live page context ("what page am I on?" works)

- The 2026-08-27 bonus discovery paid off: plugin mode keeps Sumatra's toolbar, and the
  page box is a plain Win32 `Edit` control. A daemon thread polls it every 500ms via
  `WM_GETTEXT` through `SendMessageTimeoutW` (100ms, `SMTO_ABORTIFHUNG` — never blocks on
  a busy Sumatra); total pages parsed once from the toolbar's `/ N` static.
- Page flows shell → `engine.set_page` → SSE `page` event (chat header shows "· p. N/M")
  and a `[reader context: viewing PDF page N of M]` line prepended to each turn (system
  prompt tells Claude to treat it as ground truth and not mention the mechanism; legacy
  claude -p path gets it too). Verified: "What page am I on?" → "You're on page 1 of 352"
  in 3.0s; chapter question correctly reasons from front matter.
- Hardening: if a second instance's Edge delegates to the first (shared profile) and
  exits, relaunch once with a spare `-b` profile before falling back to tk chat.
- Next: map PDF page → book section via extracted text (the real position-inference
  milestone), then the learning journal.

## 2026-08-29 (evening) — double-scrollbar / dead-space fix

- In real use the chat page grew an outer body scrollbar (scrolling the whole page,
  hiding the header) and the Edge window didn't fill the pane (dead space under the
  input). Fixes: `overflow:hidden` on html/body so `#log` is structurally the only
  scroller (+ explicit `min-height:0` on it); Edge launched with `--window-size` matching
  the pane so its remembered (possibly maximized) app-window bounds can't fight ours;
  restore-from-iconic/zoomed now reasserts geometry 250ms *after* the async restore;
  chrome offsets re-measured on every resize + at 2/5/10s; and the focus pump verifies
  the Edge window rect every ~2s, force-reasserting on drift. Self-heals within 2s
  regardless of root cause.

## 2026-08-27 (evening) — v0 shell built and working

- `app/reread_shell.py` (tkinter, stdlib only) + `reread.bat` launcher. Verified end-to-end
  with ESL (13MB / 764pp): Sumatra embeds via `-plugin <HWND>`, resizes with its pane, chat
  sidebar round-trips through `claude -p --output-format json` with `--resume` session
  continuity. Sidebar is resizable (sash) and collapsible (❯ / ⟨ Chat buttons).
- Bonus discovery: plugin mode keeps Sumatra's minimal toolbar (editable page box, find) —
  candidate for reading live page position later (UIA on the page textbox).
- Sumatra steals keyboard focus on spawn → app now forces focus back to chat input once the
  child window appears. `--selftest "msg"` flag auto-sends a message for headless testing.
- Known v0 limitations: replies render as raw text (markdown asterisks visible); ~5–30s
  claude -p latency per reply with no streaming; no book context yet (Claude doesn't know the
  page — that's the next milestone: the context bus).

## 2026-08-27 (later) — SumatraPDF containment question

- Can't put UI *inside* Sumatra (no plugin system, no PDF JS) — but `-plugin <HWND>` embeds
  Sumatra inside *our* window. End-state: reread as the shell, book canvas + Claude sidebar in
  one window, nav driven via DDE so current page is always known.
- Plan: standalone sidebar first (same hooks, nothing thrown away), upgrade to shell later.

## 2026-08-27 — Project created; reader-integration research

- Created project. Vision: Claude-alongside-the-page companion with a persistent per-book
  learning journal (the differentiator nothing on the market has).
- Decided: **no reader from scratch** — companion sidebar + thin adapters on existing readers.
- Verified hooks: Calibre Lookup panel (custom URL source, UI can live inside the viewer),
  Calibre highlights in metadata.db (pollable), SumatraPDF SelectionHandlers (`${selection}` →
  URL) + ExternalViewers hotkey (`%p` page, `%1` file), universal clipboard-watcher fallback with
  snippet→position inference from extracted book text.
- Details in `notes/2026-08-27-reader-integration.md`.
- Next: pick first adapter based on which reader Blake actually uses; test how much text
  Calibre's Lookup `{word}` placeholder carries for multi-word selections.
