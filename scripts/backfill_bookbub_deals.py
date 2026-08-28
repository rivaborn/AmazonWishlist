#!/usr/bin/env python
"""Backfill historical BookBub daily deals into the deals database.

Walks the date range ``[BOOKBUB_BACKFILL_END .. BOOKBUB_BACKFILL_START]``
day by day (descending), and for each still-unserved day logs into BookBub
via the signed outbound link from the daily email (``--link`` or
``BOOKBUB_LOGIN_LINK``), fetches the day's deal page, and stores it in
``DEALS_DB`` through the same ``deals_db.store_deals`` path as
``scripts/build_bookbub_deals.py`` (ownership audit vs ``grimmory.db``,
``no_amazon_link`` for deals with no Amazon edition). It does NOT write
``booklist.md`` — that single-day report stays owned by the builder.

Design notes
------------
* Resumable / idempotent. A per-date status is mirrored to
  ``BOOKBUB_BACKFILL_PROGRESS`` (atomic tmp + ``os.replace``) after every
  date, and dates already present in ``DEALS_DB`` are treated as done on
  startup — so a killed run resumes and a re-run skips what is recorded.
* Every day gets a recorded status:
    ok        — deals fetched and stored
    empty     — the day's page has no deal cards (a day BookBub no longer
                serves); recorded so it is not retried every run
    challenge — the page was still the Cloudflare interstitial after the
                wait; retriable later (NOT marked done)
    error     — fetching or storing failed for that day
* Cloudflare monitoring. Challenge days are printed loudly and written to
  the progress file; when a chunk's pages are challenged (or bad days run
  consecutively) the between-chunk backoff is doubled to let the block cool
  down. A login whose page never clears the challenge aborts the whole run
  with a clear message (a dead session is not worth continuing) — the
  repo's login-expired-abort convention.

Usage (from the repo root):

    python scripts/backfill_bookbub_deals.py --link '<outbound email link>' [--start YYYYMMDD] [--end YYYYMMDD] [--dry-run]

Exit codes: 0 = run finished (partial is OK — see per-date statuses and the
progress file); 1 = fatal abort (dead login, bad date range, DB/schema
error); 2 = missing --link / usage.
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import date as _date, datetime
from datetime import timedelta
from pathlib import Path

# `python scripts/backfill_bookbub_deals.py` puts scripts/ on sys.path[0], so
# make the repo root importable the same way a caller at the root would have it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import bookbub  # noqa: E402
from app import deals_db  # noqa: E402
from app.config import (  # noqa: E402
    BOOKBUB_BACKFILL_BACKOFF,
    BOOKBUB_BACKFILL_CHUNK,
    BOOKBUB_BACKFILL_DELAY,
    BOOKBUB_BACKFILL_END,
    BOOKBUB_BACKFILL_PROGRESS,
    BOOKBUB_BACKFILL_START,
    BOOKBUB_LOGIN_LINK,
    DEALS_DB,
    GRIMMORY_DB,
)


# --------------------------------------------------------------------------- #
# Date range
# --------------------------------------------------------------------------- #
def _date_list(start: str, end: str) -> list[str]:
    """Inclusive YYYYMMDD list from ``end`` up to ``start``, descending.

    Going back day by day from ``start`` to ``end`` (both inclusive).
    """
    d0 = _date(int(end[:4]), int(end[4:6]), int(end[6:]))
    d1 = _date(int(start[:4]), int(start[4:6]), int(start[6:]))
    if d0 > d1:
        raise SystemExit(f"ERROR: --start {start} is after --end {end}")
    out: list[str] = []
    d = d1
    while d >= d0:
        out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# Progress mirror (resume state)
# --------------------------------------------------------------------------- #
def _load_progress(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_progress(path: Path, progress: dict) -> None:
    """Mirror the progress atomically (tmp + os.replace) — repo convention."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".backfill_progress.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(progress, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _dates_in_db(path: Path) -> set[str]:
    """Distinct deal dates already stored (the DB is the source of truth)."""
    if not path.exists():
        return set()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        try:
            return {r[0] for r in conn.execute("SELECT DISTINCT date FROM deal")}
        except sqlite3.OperationalError:
            return set()  # table not created yet
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill historical BookBub daily deals into DEALS_DB."
    )
    parser.add_argument("--link", default=None,
                        help="Outbound auto-login link from the daily email (else BOOKBUB_LOGIN_LINK).")
    parser.add_argument("--start", default=BOOKBUB_BACKFILL_START,
                        help=f"Newest day to process, YYYYMMDD (default: {BOOKBUB_BACKFILL_START}).")
    parser.add_argument("--end", default=BOOKBUB_BACKFILL_END,
                        help=f"Oldest day to process, YYYYMMDD (default: {BOOKBUB_BACKFILL_END}).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the date list and what would be done; fetch nothing.")
    args = parser.parse_args(argv)

    link = args.link or BOOKBUB_LOGIN_LINK
    if not link:
        print("ERROR: no BookBub login link: pass --link or set BOOKBUB_LOGIN_LINK", file=sys.stderr)
        return 2

    try:
        dates = _date_list(bookbub._normalise_date(args.start), bookbub._normalise_date(args.end))
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1

    # Seed the done-set: progress file first (it carries statuses), then
    # whatever is already in DEALS_DB (e.g. 20260827 from the single-day
    # builder) — DB rows win as "done" unless the progress file says more.
    progress = _load_progress(BOOKBUB_BACKFILL_PROGRESS)
    statuses: dict = progress.setdefault("dates", {})
    for d in _dates_in_db(DEALS_DB):
        statuses.setdefault(d, {"status": "ok", "count": None, "at": None})

    pending = [d for d in dates if statuses.get(d, {}).get("status") not in ("ok", "empty")]

    print(f"range: {dates[-1]}..{dates[0]} ({len(dates)} days) | "
          f"done: {len(dates) - len(pending)} | pending: {len(pending)}")
    for d in dates:
        print(f"  {d}: {statuses.get(d, {}).get('status', 'pending')}")

    if args.dry_run:
        print("dry-run: nothing fetched or stored.")
        return 0

    if not pending:
        print("nothing to do — every day in the range is already recorded ok/empty.")
        return 0

    # Walk the remaining dates in chunks (one login session per chunk).
    chunks = [pending[i:i + BOOKBUB_BACKFILL_CHUNK]
              for i in range(0, len(pending), BOOKBUB_BACKFILL_CHUNK)]

    totals = {"ok": 0, "empty": 0, "challenge": 0, "error": 0}
    total_deals = total_owned = total_no_amazon = 0
    consecutive_bad = 0

    for ci, chunk in enumerate(chunks):
        print(f"\n=== session {ci + 1}/{len(chunks)}: {chunk[0]} -> {chunk[-1]} "
              f"({len(chunk)} days) ===")
        challenged: set[str] = set()
        try:
            fetched = bookbub.fetch_daily_deals_many(
                chunk, link=link,
                inter_date_delay=BOOKBUB_BACKFILL_DELAY,
                challenged=challenged,
            )
        except bookbub.BookbubError as e:
            # Dead session / hard failure: abort the whole run (the repo's
            # login-expired-abort convention). Recorded dates stay put.
            print(f"ABORT: {e}", file=sys.stderr)
            print(f"resume later with a fresh --link; progress: {BOOKBUB_BACKFILL_PROGRESS}",
                  file=sys.stderr)
            return 1

        for d in chunk:
            deals = fetched.get(d, [])
            now = datetime.now().isoformat(timespec="seconds")
            try:
                if not deals and d in challenged:
                    status, count = "challenge", 0
                    totals["challenge"] += 1
                    print(f"  {d}: CHALLENGE — page still on the Cloudflare interstitial "
                          f"after the wait; will be retried on the next run", file=sys.stderr)
                elif not deals:
                    status, count = "empty", 0
                    totals["empty"] += 1
                    print(f"  {d}: empty — no deal cards on the page "
                          f"(day no longer served?); recorded, not retried")
                else:
                    stored, owned, no_amazon = deals_db.store_deals(
                        deals, d, deals_path=DEALS_DB, grimmory_path=GRIMMORY_DB)
                    status, count = "ok", len(deals)
                    totals["ok"] += 1
                    total_deals += stored
                    total_owned += owned
                    total_no_amazon += no_amazon
                    print(f"  {d}: ok — {stored} deals stored ({owned} owned, {no_amazon} no-amazon)")
            except sqlite3.Error as e:
                status, count = "error", None
                totals["error"] += 1
                print(f"  {d}: ERROR — store failed: {e}", file=sys.stderr)

            if status in ("challenge", "error"):
                consecutive_bad += 1
            else:
                consecutive_bad = 0

            statuses[d] = {"status": status, "count": count, "at": now}
            _save_progress(BOOKBUB_BACKFILL_PROGRESS, progress)

        # Between chunks: the configured backoff; double it under Cloudflare
        # pressure (a challenged page or consecutive bad days) to cool down.
        if ci < len(chunks) - 1:
            sleep_for = BOOKBUB_BACKFILL_BACKOFF
            if challenged or consecutive_bad >= 2:
                sleep_for *= 2
                print(f"  ! Cloudflare pressure detected "
                      f"({len(challenged)} challenged, {consecutive_bad} consecutive bad) "
                      f"— backing off {sleep_for:.0f}s before the next session",
                      file=sys.stderr)
            time.sleep(sleep_for)

    print(f"\nbackfill summary: ok={totals['ok']} empty={totals['empty']} "
          f"challenge={totals['challenge']} error={totals['error']} | "
          f"deals stored this run: {total_deals} (owned {total_owned}, "
          f"no-amazon {total_no_amazon})")
    print(f"progress: {BOOKBUB_BACKFILL_PROGRESS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
