"""Vertical-slice CLI: grounded Q&A REPL over an ingested book store.

Proves the tiered-context design end to end (cached TOC system prompt,
pull-based section reads, grep retrieval, citations) before any GUI plumbing.
Also the quickest place to prototype learning-mode prompts: the same dials
tail the bus sends rides every turn, and any <!--rb …--> trailer Claude emits
is shown (not recorded).

Usage: python app\\ask.py [book_dir] [--page N] [--model sonnet]
                          [--effort medium] [--dials challenge,style]
  book_dir defaults to the newest store under books/.
  /page N   set reader position     /dials 2,simple   switch dials
  /tail X   one-off protocol line for the next turn      /quit   exit
"""

import argparse
import asyncio
import os
import sys
import time

from claude_agent_sdk import ClaudeSDKClient

import bookcontext
import learning
from bookstore import BookStore, BOOKS_DIR
from bus import _sdk_options, CLAUDE, tail_parts


def newest_store():
    if not os.path.isdir(BOOKS_DIR):
        return None
    dirs = [os.path.join(BOOKS_DIR, d) for d in os.listdir(BOOKS_DIR)
            if os.path.isfile(os.path.join(BOOKS_DIR, d, "toc.json"))]
    return max(dirs, key=os.path.getmtime) if dirs else None


def parse_dials(spec, base=None):
    d = dict(base or {"challenge": 1, "style": "standard"})
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            d["challenge"] = max(0, min(3, int(part)))
        else:
            d["style"] = part
    return d


async def run(store, page, model, effort, dials):
    kwargs = dict(
        bookcontext.sdk_kwargs(store),
        model=model,
        effort=effort,
        include_partial_messages=True,
        max_thinking_tokens=0,
    )
    if os.path.isfile(CLAUDE):
        kwargs["cli_path"] = CLAUDE
    client = ClaudeSDKClient(options=_sdk_options(**kwargs))
    await client.connect()
    state = learning.load(store.book_dir)
    print(f"reading: {store.title}")
    print(f"store:   {store.book_dir}")
    print(f"model:   {model} ({effort})   position: "
          f"{f'PDF p. {page}' if page else 'unset (--page N or /page N)'}")
    print(f"dials:   {dials}")
    print("ask away — /page N, /dials a,b,c, /tail X, /quit\n")

    loop = asyncio.get_running_loop()
    tail = None
    while True:
        try:
            q = (await loop.run_in_executor(None, input, "you> ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q.lower() in ("/quit", "/q", "/exit"):
            break
        if q.lower().startswith("/page"):
            try:
                page = int(q.split()[1])
                node = store.locate_page(page)
                print(f"  -> {node['title'] if node else '(outside book)'}\n")
            except (IndexError, ValueError):
                print("  usage: /page N\n")
            continue
        if q.lower().startswith("/dials"):
            dials = parse_dials(q[6:], dials)
            print(f"  -> {dials}\n")
            continue
        if q.lower().startswith("/tail"):
            tail = q[5:].strip() or None
            print(f"  -> next turn tail: {tail}\n")
            continue

        parts = []
        ctx = bookcontext.reader_context_line(store, page)
        if ctx:
            parts.append(ctx)
        parts += tail_parts(dials, state, tail)
        tail = None
        prompt = "\n".join(parts) + f"\n\n{q}"
        t0 = time.monotonic()
        await client.query(prompt)
        print()
        filt = learning.TrailerFilter()
        async for msg in client.receive_response():
            kind = type(msg).__name__
            if kind == "StreamEvent":
                ev = getattr(msg, "event", None) or {}
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        print(filt.feed(delta["text"]), end="", flush=True)
            elif kind == "AssistantMessage":
                for block in getattr(msg, "content", []) or []:
                    name = getattr(block, "name", None)
                    if name:                      # tool call -> show retrieval
                        arg = getattr(block, "input", {}) or {}
                        target = (arg.get("file_path") or arg.get("pattern")
                                  or arg.get("path") or "")
                        print(f"\n  [{name}] {target}", flush=True)
            elif kind == "ResultMessage":
                print(filt.flush(), end="")
                _, events, warns = learning.parse_trailer(
                    getattr(msg, "result", None) or "")
                u = getattr(msg, "usage", None) or {}
                print(f"\n\n  -- {time.monotonic() - t0:.1f}s | "
                      f"in {u.get('input_tokens', '?')} "
                      f"| cache read {u.get('cache_read_input_tokens', '?')} "
                      f"| cache write "
                      f"{u.get('cache_creation_input_tokens', '?')} "
                      f"| out {u.get('output_tokens', '?')}")
                for ev in events:
                    print(f"  ## trailer: {ev}")
                for w in warns:
                    print(f"  ## trailer warning: {w}")
                print()
    await client.disconnect()


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("book_dir", nargs="?", default=None)
    ap.add_argument("--page", type=int, default=None)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--dials", default="",
                    help="e.g. retain,2,eli5 (any order; digit = challenge)")
    args = ap.parse_args()
    book_dir = args.book_dir or newest_store()
    if not book_dir or not os.path.isfile(os.path.join(book_dir, "toc.json")):
        sys.exit("no book store found — run: python app\\ingest.py <book.pdf>")
    store = BookStore.open(book_dir)
    asyncio.run(run(store, args.page, args.model, args.effort,
                    parse_dials(args.dials)))


if __name__ == "__main__":
    main()
