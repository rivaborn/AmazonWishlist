#!/usr/bin/env python
"""Build booklist.md — the daily BookBub ebook-deal report.

Logs into BookBub via the signed outbound link from the daily email
(``--link`` or ``BOOKBUB_LOGIN_LINK``), fetches the day's ebook deals
(``app.bookbub``), and writes a markdown report to ``--out`` (default
``BOOKBUB_OUTPUT`` = ``<repo root>/booklist.md``). The write is atomic
(tmp file + ``os.replace``) so a crash never leaves a half-written report.

Usage (from the repo root):

    python scripts/build_bookbub_deals.py --link '<outbound email link>' [--date YYYYMMDD] [--out PATH]

Options:
    --date        Day to pull, YYYYMMDD (default: today).
    --out         Where to write the markdown report (default: config).
    --llm-model   Optionally normalise the list through the local LLMConfig
                  gateway (OpenAI-compatible chat completions at LLM_BASE_URL).
                  Off by default (LLM_MODEL); the deterministic selectolax
                  parse is the deliverable — an LLM failure NEVER blocks the
                  write, it only logs a warning and falls back to the raw list.

Exit codes: 0 = written; 1 = fetch/parse error; 2 = missing --link / usage.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

# `python scripts/build_bookbub_deals.py` puts scripts/ on sys.path[0], so make
# the repo root importable the same way a caller at the root would have it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import bookbub  # noqa: E402
from app.config import (  # noqa: E402
    BOOKBUB_LOGIN_LINK,
    BOOKBUB_OUTPUT,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TIMEOUT,
)


class BuildError(RuntimeError):
    """A fetch/parse failure that should stop the build with exit 1."""


# --------------------------------------------------------------------------- #
# LLM normalisation (optional, off by default, never blocks the write)
# --------------------------------------------------------------------------- #
_LLM_PROMPT = (
    "You are given JSON: the day's BookBub ebook deals "
    "(a list of {title, author, price, url}). Rewrite them as a markdown list, "
    "one line per deal in the form:\n\n"
    "- [Title](url) — Author — price\n\n"
    "Rules: keep every deal and keep the exact prices; do not add or invent "
    "anything; if author is empty omit the author segment; output ONLY the "
    "markdown list, no commentary."
)


def _llm_normalize(deals: list, model: str) -> str:
    """Ask the local gateway to reformat the deals as markdown.

    Raises on any problem; the caller treats that as "use the raw list".
    """
    import json

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _LLM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    [{"title": d.title, "author": d.author, "price": d.price, "url": d.url} for d in deals]
                ),
            },
        ],
        "temperature": 0,
    }
    base = LLM_BASE_URL.rstrip("/")
    # Tolerate both ".../v1" and ".../v1/" (with or without trailing slash).
    endpoint = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
    with httpx.Client(timeout=LLM_TIMEOUT) as client:
        resp = client.post(endpoint, json=payload)
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
    if not text.strip():
        raise BuildError("LLM returned an empty completion")
    return text.strip()


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #
def _md_escape(text: str) -> str:
    """Escape the pipe so a '|' in a title/author can't break the table."""
    return (text or "").replace("|", "\\|")


def _deals_to_markdown(deals: list, date: str) -> str:
    """Render the deal list (non-LLM path) as markdown."""
    lines = [
        f"# BookBub ebook deals — {date}",
        "",
        f"_{len(deals)} deals, pulled from the daily BookBub email "
        f"(outbound auto-login) by `scripts/build_bookbub_deals.py`._",
        "",
        "| Title | Author | Deal price |",
        "| --- | --- | --- |",
    ]
    for d in deals:
        title = _md_escape(d.title)
        if d.url:
            title = f"[{title}]({d.url})"
        lines.append(f"| {title} | {_md_escape(d.author)} | {_md_escape(d.price)} |")
    lines.append("")
    return "\n".join(lines)


def _write_atomic(path: Path, content: str) -> None:
    """Write `content` to `path` atomically (tmp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the day's BookBub ebook deals and write booklist.md."
    )
    parser.add_argument("--link", default=None,
                        help="Outbound auto-login link from the daily email (else BOOKBUB_LOGIN_LINK).")
    parser.add_argument("--date", default=None,
                        help="Day to pull, YYYYMMDD (default: today).")
    parser.add_argument("--out", default=None, type=Path,
                        help=f"Report path (default: {BOOKBUB_OUTPUT}).")
    parser.add_argument("--llm-model", default=LLM_MODEL,
                        help="Normalise the list through this model on the local LLM "
                             "gateway (default: LLM_MODEL env; empty = off).")
    args = parser.parse_args(argv)

    link = args.link or BOOKBUB_LOGIN_LINK
    if not link:
        print("ERROR: no BookBub login link: pass --link or set BOOKBUB_LOGIN_LINK", file=sys.stderr)
        return 2
    out_path = args.out or BOOKBUB_OUTPUT
    date = bookbub._normalise_date(args.date)  # shared YYYYMMDD normalisation (today default)

    try:
        deals = bookbub.fetch_daily_deals(date, link=link)
    except bookbub.BookbubError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not deals:
        print(f"ERROR: no deals found for {date} — nothing to write", file=sys.stderr)
        return 1

    body = _deals_to_markdown(deals, date)
    if args.llm_model:
        try:
            body = _llm_normalize(deals, args.llm_model)
            print(f"(list normalised via local LLM '{args.llm_model}')", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - the LLM must never block the write
            print(f"WARNING: LLM normalisation unavailable ({type(e).__name__}: {e}); "
                  f"writing the raw parsed list instead.", file=sys.stderr)

    _write_atomic(out_path, body)
    print(f"wrote {len(deals)} deals for {date} to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
