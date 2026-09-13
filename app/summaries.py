"""Per-chapter summaries: books/<id>/summaries/NN.md, one per level-1 node.

Each file carries a short summary, the chapter's key concepts (with section
and PDF page), and the earlier concepts the chapter assumes. Written by a
one-shot Haiku call that Reads the chapter's section files and answers in a
JSON schema (structured output); the bus renders the markdown so Claude's
own client never needs a Write tool inside the store.

Used first for book-wide questions (grounding rules mention the directory),
and by the checkpoint / "Refresh me" turns, which create a missing summary
lazily. Batch: python app\\summaries.py <book_dir> [--model haiku] [--force]
"""

import asyncio
import json
import logging
import os
import re

log = logging.getLogger("lantern.summaries")

DIRNAME = "summaries"
MODEL = "haiku"

SYSTEM = (
    "You summarise textbook chapters for a reading-companion app. You have "
    "Read access to the book's extracted text files. Be faithful to the text: "
    "no invented concepts, page numbers or section ids."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "one paragraph, at most 150 words"},
        "key_concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "definition": {"type": "string"},
                    "sec": {"type": "string",
                            "description": "NN.MM section id from the file name"},
                    "pdf_page": {"type": "integer"},
                },
                "required": ["label", "definition", "sec", "pdf_page"],
            },
        },
        "assumes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "from_sec": {"type": "string",
                                 "description": "earlier NN.MM section that introduced it, or empty"},
                    "why": {"type": "string"},
                },
                "required": ["label", "from_sec", "why"],
            },
        },
    },
    "required": ["summary", "key_concepts", "assumes"],
}


def path(store, chapter):
    return os.path.join(store.book_dir, DIRNAME, f"{chapter['id'][:2]}.md")


def relpath(store, chapter):
    return f"{DIRNAME}/{chapter['id'][:2]}.md"


def exists(store, chapter):
    return os.path.isfile(path(store, chapter))


def chapters(store):
    return [n for n in store.toc if n["level"] == 1
            and not n["id"].startswith(("00.", "zz."))]


def missing(store):
    return [c for c in chapters(store) if not exists(store, c)]


def _prompt(store, chapter):
    files = [n["file"] for n in store.sections_of(chapter)]
    return (
        f"Chapter: {chapter['title']} (id {chapter['id']}, PDF pages "
        f"{chapter['pdf_start']}–{store.sections_of(chapter)[-1]['pdf_end']}).\n"
        "Read every one of these section files (lines like [p.N] mark PDF "
        "page N):\n" + "\n".join(f"- {f}" for f in files) +
        "\n\nThen answer in the required JSON: `summary` (one paragraph, at "
        "most 150 words); `key_concepts` (6-12 items: label, one-line "
        "definition in the book's terms, the NN.MM section id taken from the "
        "file name, and the PDF page where it is introduced); `assumes` "
        "(3-8 concepts from EARLIER chapters this chapter relies on — look "
        "for back-references such as 'recall', 'as in Chapter', 'Section "
        "k.m' — each with the earlier section id if the text names it, else "
        "an empty string, and one line on why it matters here)."
    )


def _extract_json(text):
    """Structured output is preferred; fall back to the first JSON object in
    free text."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def render(chapter, data):
    lines = [f"# {chapter['title']} — summary", "",
             (data.get("summary") or "").strip(), "", "## Key concepts", ""]
    for c in data.get("key_concepts") or []:
        pg = c.get("pdf_page")
        cite = f" [p.{pg}]" if pg else ""
        lines.append(f"- **{c.get('label', '').strip()}** — "
                     f"{c.get('definition', '').strip()} (§{c.get('sec', '?')}{cite})")
    lines += ["", "## Assumes (from earlier chapters)", ""]
    for a in data.get("assumes") or []:
        src = f" (§{a['from_sec']})" if a.get("from_sec") else ""
        lines.append(f"- **{a.get('label', '').strip()}**{src} — "
                     f"{a.get('why', '').strip()}")
    return "\n".join(lines).rstrip() + "\n"


async def summarize_chapter(store, chapter, one_shot, model=MODEL):
    """Write summaries/NN.md for `chapter` via `one_shot(prompt, system,
    model, tools, cwd, max_turns, output_format) -> (text, structured)`.
    Returns the file path, or None if the model gave nothing usable."""
    n_files = len(store.sections_of(chapter))
    text, structured = await one_shot(
        _prompt(store, chapter), SYSTEM, model=model, tools=("Read",),
        cwd=store.book_dir, max_turns=n_files + 6,
        output_format={"type": "json_schema", "schema": SCHEMA})
    data = structured if isinstance(structured, dict) else _extract_json(text)
    if not data or not data.get("summary"):
        log.warning("no usable summary for %s", chapter["id"])
        return None
    out = path(store, chapter)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(chapter, data))
    return out


async def summarize_all(store, one_shot, model=MODEL, force=False,
                        progress=print):
    todo = chapters(store) if force else missing(store)
    for ch in todo:
        progress(f"summarising {ch['title']} …")
        p = await summarize_chapter(store, ch, one_shot, model=model)
        progress(f"  -> {p or 'FAILED'}")
    return len(todo)


def main(argv=None):
    import argparse
    import sys
    from bookstore import BookStore
    from bus import one_shot
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("book_dir")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--force", action="store_true", help="rewrite existing")
    a = ap.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    store = BookStore.open(a.book_dir)
    n = asyncio.run(summarize_all(store, one_shot, model=a.model, force=a.force))
    print(f"done: {n} chapter(s)")


if __name__ == "__main__":
    main()
