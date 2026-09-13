# Lantern

An AI learning companion that runs alongside SumatraPDF. It explains, summarises, quizzes and tracks your understanding as you read eBooks and PDFs, especially technical ones.

![Lantern](app/lantern.png)

## What it does

- **Reads with you.** Claude always knows the page you are on and the text you have selected.
- **Right-click the page** to Explain, Define, Unpack a formula, Hint for an exercise, Quiz me, or Save a highlight.
- **Page citations are links.** `[p.N]` in a reply jumps the reader to that page.
- **Learning journal per book.** Questions, highlights and quiz results are kept, and Claude refers back to them.
- **Spaced review.** Concepts you got wrong come back on a 1/3/7/14/30 day ladder. Export to Anki.
- **Dials.** Style (Standard / Plain / Simple) and Challenge (from "just tell me" to "make me work").
- **PDF, EPUB, MOBI, FB2.** Books are indexed locally on first open.

## Requirements

Windows 10 or later, Python 3.10+, [SumatraPDF](https://www.sumatrapdfreader.org/), Microsoft Edge, and a logged-in [Claude Code](https://claude.com/claude-code) CLI. No API key needed.

## Setup

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Run

```
lantern.bat                 # library of your books
lantern.bat path\to\book.pdf
```

## More

`log.md` is the dated dev log. `notes/` holds design notes. Book data lives in `books/` and is never committed.
