"""Business logic: ingest snapshots, query views, compute drops."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Literal, Optional

from .config import (
    CHROMIUM_USER_DATA_DIR,
    INGEST_SHRINK_FLOOR,
    PLAYWRIGHT_HEADLESS,
    PROGRESS_PATH,
    RESUME_MAX_AGE_SEC,
    SCRAPE_PER_WISHLIST_SECONDS,
    STALE_AFTER_HOURS,
    STORAGE_STATE,
    use_playwright,
)
from .db import connect
from .models import BookRow, ScrapedItem
from .scraper import BotDetected, FetchFailed, LoginExpired, fetch_wishlist

log = logging.getLogger(__name__)

Basis = Literal["prev", "list"]


class SuspiciousShrink(Exception):
    """A scrape came back too short to be trusted, so it was not ingested.

    `ingest_wishlist` replaces a wishlist's membership wholesale, so a scrape
    that ends early -- pagination stopping short without ever erroring -- silently
    drops every item it missed. That has happened for real: 2026-08-10 list 5
    ingested 320 of 554, 2026-08-12 list 7 ingested 170 of 407, each wiping the
    remainder for a day. The old 0.8x check lived only in the wishlists template,
    so it coloured the row red *after* the data was already gone.

    Refusing once and accepting a shrink that a second consecutive scrape agrees
    with is what keeps this a guard rather than a trap: a list the owner really
    did prune returns to normal within a day instead of stranding.
    """


# ---------- in-memory scrape progress (single-process app) ----------

_progress_lock = threading.Lock()
_progress: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "total": 0,
    "done": 0,
    "current_label": None,
    "current_url": None,
    "items_total": 0,
    "error": None,
    "waiting": False,
    "next_starts_at": None,
    # ---- resume bookkeeping (persisted; not part of the public API contract) ----
    # run_id          identifies a single full-scrape run
    # pending_ids     wishlist ids not yet processed in the current run; the only
    #                 field that stays non-empty after an abrupt death, so it's
    #                 our "this run was interrupted" signal on the next startup
    # last_started_at wall-clock ISO of when the most recent wishlist scrape began,
    #                 used to re-derive pacing across a restart (monotonic resets)
    "run_id": None,
    "pending_ids": [],
    "last_started_at": None,
}


def get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def _persist_progress_locked() -> None:
    """Atomically mirror `_progress` to disk. Caller must hold `_progress_lock`."""
    try:
        tmp = PROGRESS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_progress), encoding="utf-8")
        os.replace(tmp, PROGRESS_PATH)
    except OSError:
        log.exception("Failed to persist scrape progress to %s", PROGRESS_PATH)


def _progress_update(**kwargs) -> None:
    with _progress_lock:
        _progress.update(kwargs)
        _persist_progress_locked()


def _complete_wishlist(wishlist_id: int, items_added: int = 0) -> None:
    """Mark one wishlist done in the current run: advance counters and drop it
    from the pending queue, then persist. Called for both successful ingests and
    handled failures (a bot-block/fetch-failure still 'consumes' the slot)."""
    with _progress_lock:
        _progress["done"] += 1
        _progress["items_total"] += items_added
        try:
            _progress["pending_ids"].remove(wishlist_id)
        except ValueError:
            pass
        _persist_progress_locked()


def _now() -> str:
    # microsecond precision so two ingests in the same second still order
    return datetime.now().isoformat(timespec="microseconds")


def add_wishlist(url: str, label: Optional[str] = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO wishlist (url, label, added_at) VALUES (?, ?, ?)",
            (url, label, _now()),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute("SELECT id FROM wishlist WHERE url = ?", (url,)).fetchone()
        return row["id"]


def remove_wishlist(wishlist_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM wishlist WHERE id = ?", (wishlist_id,))


def list_wishlists() -> list[dict]:
    """Every wishlist plus its current size and how stale its last scrape is.

    `stale_hours` / `stale` are derived here rather than in the template because
    they are the only honest health signal on that page. A failed scrape leaves
    `previous_item_count`, the membership count and `last_scraped_at` all
    untouched by design ("never clobber good data"), so a list that has been
    bot-blocked for days still renders a perfectly matched Previous/Current
    pair. Age is the one column that moves. A never-scraped wishlist ages from
    `added_at`, so it is not exempt.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                w.id, w.url, w.label, w.added_at, w.last_scraped_at,
                w.previous_item_count, w.pending_shrink_count,
                (SELECT COUNT(*) FROM wishlist_book wb WHERE wb.wishlist_id = w.id) AS last_item_count
            FROM wishlist w
            ORDER BY w.added_at
            """
        ).fetchall()

    now = datetime.now()
    out = []
    for r in rows:
        d = dict(r)
        # A wishlist that has NEVER been scraped successfully ages from its
        # added_at, so the unhealthiest row of all still goes stale instead of
        # reading as clean forever.
        basis = d.get("last_scraped_at") or d.get("added_at")
        hours = None
        if basis:
            try:
                hours = (now - datetime.fromisoformat(basis)).total_seconds() / 3600.0
            except (TypeError, ValueError):
                hours = None
        d["stale_hours"] = hours
        d["stale"] = hours is not None and hours > STALE_AFTER_HOURS
        out.append(d)
    return out


def _mark_scraped(wishlist_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE wishlist SET last_scraped_at = ? WHERE id = ?",
            (_now(), wishlist_id),
        )


def _clear_pending_shrink(wishlist_id: int) -> None:
    """A failed scrape breaks the shrink-confirmation chain.

    `pending_shrink_count` means "the LAST completed scrape was suspiciously
    short". A bot-block or fetch failure in between makes two short scrapes
    non-consecutive, so the next short one must re-confirm from scratch rather
    than being accepted against a days-old marker.
    """
    with connect() as conn:
        conn.execute(
            "UPDATE wishlist SET pending_shrink_count = NULL WHERE id = ?",
            (wishlist_id,),
        )


def ingest_wishlist(wishlist_id: int, items: list[ScrapedItem]) -> None:
    """Replace the wishlist's membership and append a price snapshot per item.

    Raises `SuspiciousShrink` — before touching anything — if `items` is below
    `INGEST_SHRINK_FLOOR` of the count already stored, unless the previous
    completed scrape was refused for the same reason AND the two short counts
    agree (see the class docstring).
    """
    now = _now()
    new_count = len(items)
    with connect() as conn:
        # Decided BEFORE opening the transaction: the refusal path has to leave a
        # marker behind, and a rejection is not a rollback.
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM wishlist_book wb WHERE wb.wishlist_id = w.id) AS prev_count,
                w.pending_shrink_count
            FROM wishlist w WHERE w.id = ?
            """,
            (wishlist_id,),
        ).fetchone()
        prev_count = row["prev_count"] if row else 0
        pending_shrink = row["pending_shrink_count"] if row else None
        short = prev_count > 0 and new_count < prev_count * INGEST_SHRINK_FLOOR
        # Two short scrapes only confirm each other when they roughly AGREE on
        # the new size (within the same floor ratio): two different truncation
        # points are evidence of an unstable scrape, not of a real prune.
        agrees = pending_shrink is not None and min(new_count, pending_shrink) >= (
            max(new_count, pending_shrink) * INGEST_SHRINK_FLOOR
        )

        if short and not agrees:
            # Short and unconfirmed: record it and keep the existing membership.
            # A real prune shows up again -- at the same size -- on the very
            # next run and is accepted then. (A scrape failure in between
            # clears the marker; see _clear_pending_shrink.)
            conn.execute(
                "UPDATE wishlist SET pending_shrink_count = ? WHERE id = ?",
                (new_count, wishlist_id),
            )
            disagree = (
                f"; previous short scrape saw {pending_shrink}, which does not agree"
                if pending_shrink is not None
                else ""
            )
            raise SuspiciousShrink(
                f"{new_count} items vs {prev_count} stored "
                f"({new_count / prev_count:.0%}, floor {INGEST_SHRINK_FLOOR:.0%})"
                f"{disagree}; membership kept - will accept if the next scrape agrees"
            )
        if short:
            log.warning(
                "Wishlist %s: accepting shrink to %d items (was %d) - confirmed by a "
                "second consecutive short scrape that agrees (first refusal saw %d)",
                wishlist_id, new_count, prev_count, pending_shrink,
            )

        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE wishlist SET previous_item_count = ?, pending_shrink_count = NULL "
                "WHERE id = ?",
                (prev_count, wishlist_id),
            )
            conn.execute(
                "DELETE FROM wishlist_book WHERE wishlist_id = ?", (wishlist_id,)
            )
            for it in items:
                conn.execute(
                    """
                    INSERT INTO book (asin, title, author, product_url, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(asin) DO UPDATE SET
                        title = excluded.title,
                        author = COALESCE(excluded.author, book.author),
                        product_url = excluded.product_url,
                        last_seen = excluded.last_seen
                    """,
                    (it.asin, it.title, it.author, it.product_url, now, now),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO wishlist_book (wishlist_id, asin) VALUES (?, ?)",
                    (wishlist_id, it.asin),
                )
                conn.execute(
                    """
                    INSERT INTO price_snapshot
                        (asin, observed_at, current_price_cents, list_price_cents, availability)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        it.asin,
                        now,
                        it.current_price_cents,
                        it.list_price_cents,
                        it.availability,
                    ),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def run_full_scrape(resume: bool = False) -> dict[str, int]:
    """Scrape every registered wishlist; return per-wishlist item counts.

    Progress is persisted to disk at every step so an interrupted run (e.g. the
    service restarted by an OS library upgrade) can be picked up by
    `resume_if_interrupted()` on the next startup. Pacing is anchored on
    wall-clock `last_started_at` so the one-wishlist-per-interval rule survives a
    restart (a monotonic clock resets to zero with the new process).

    `resume=True` continues the current persisted run, scraping only the
    wishlists still in `pending_ids` and preserving the already-done count and
    pacing. A fresh call (`resume=False`) starts a new run over every wishlist.

    Idempotent against concurrent calls only via the API guard
    (`POST /api/scrape/run` rejects if `running` is True).
    """
    interval = max(0, SCRAPE_PER_WISHLIST_SECONDS)

    if resume and (_progress.get("pending_ids") or []):
        run_id = _progress.get("run_id") or _now()
        pending_ids = list(_progress.get("pending_ids") or [])
        started_at = _progress.get("started_at") or _now()
        done = int(_progress.get("done") or 0)
        items_total = int(_progress.get("items_total") or 0)
        total = int(_progress.get("total") or (done + len(pending_ids)))
        last_started_at = _progress.get("last_started_at")
        log.info("Resuming scrape run %s: %d of %d wishlist(s) remaining",
                 run_id, len(pending_ids), total)
    else:
        run_id = _now()
        started_at = run_id
        pending_ids = [w["id"] for w in list_wishlists()]
        done = 0
        items_total = 0
        total = len(pending_ids)
        last_started_at = None

    _progress_update(
        running=True,
        run_id=run_id,
        started_at=started_at,
        finished_at=None,
        total=total,
        done=done,
        current_label=None,
        current_url=None,
        items_total=items_total,
        error=None,
        waiting=False,
        next_starts_at=None,
        pending_ids=pending_ids,
        last_started_at=last_started_at,
    )
    counts: dict[str, int] = {}
    last_error: Optional[str] = None

    # Decide which scraper path to use *once* per run, opening one Playwright
    # context (if applicable) for the whole run instead of paying chromium
    # spin-up cost per wishlist.
    pw_ctx = _open_playwright_context_or_none()
    log.info("Scraper path: %s", "playwright" if pw_ctx is not None else "httpx")

    try:
        # Resolve the pending ids to current wishlist rows in their original
        # order, skipping any deleted since the run started.
        by_id = {w["id"]: w for w in list_wishlists()}
        work = [by_id[i] for i in pending_ids if i in by_id]

        for w in work:
            # Pace: ensure >= `interval` seconds since the previous wishlist
            # START. Measured on wall-clock so the gap is honoured even if the
            # service was restarted mid-wait.
            if interval > 0 and last_started_at:
                try:
                    target = datetime.fromisoformat(last_started_at) + timedelta(seconds=interval)
                except ValueError:
                    target = None
                if target is not None:
                    wait_seconds = (target - datetime.now()).total_seconds()
                    if wait_seconds > 0:
                        next_at = target.isoformat(timespec="seconds")
                        log.info("Waiting %ds before next wishlist (until %s)",
                                 int(wait_seconds), next_at)
                        _progress_update(
                            waiting=True,
                            current_label=f"Waiting until {next_at[11:19]} for next wishlist",
                            next_starts_at=next_at,
                        )
                        # Sleep in slices so the progress endpoint stays fresh.
                        end = time.monotonic() + wait_seconds
                        while True:
                            remaining = end - time.monotonic()
                            if remaining <= 0:
                                break
                            time.sleep(min(remaining, 5.0))

            last_started_at = _now()
            label = w.get("label") or w["url"]
            _progress_update(
                current_label=label,
                current_url=w["url"],
                waiting=False,
                next_starts_at=None,
                last_started_at=last_started_at,
            )
            log.info("Scraping wishlist %s (%s)", w["id"], w["url"])
            try:
                if pw_ctx is not None:
                    from .scraper_playwright import fetch_wishlist_playwright
                    items = fetch_wishlist_playwright(
                        w["url"], list_label=label, context=pw_ctx["context"]
                    )
                else:
                    items = fetch_wishlist(w["url"], list_label=label)
            except LoginExpired as e:
                last_error = "login expired — open Login tab and re-authenticate"
                log.warning("Login expired on wishlist %s: %s", w["id"], e)
                counts[w["url"]] = 0
                _clear_pending_shrink(w["id"])
                # No point continuing once the saved session is dead. Clear the
                # queue so we don't auto-resume into the same dead session on the
                # next restart — the daily cron will retry with a fresh run.
                _progress_update(pending_ids=[])
                break
            except BotDetected as e:
                # Don't ingest — preserve previous count + timestamp. Any
                # failure also breaks the shrink-confirmation chain: a short
                # scrape can only be confirmed by the VERY NEXT completed one.
                last_error = f"bot-blocked: {label}"
                log.warning("Bot-blocked on wishlist %s: %s", w["id"], e)
                counts[w["url"]] = 0
                _clear_pending_shrink(w["id"])
                _complete_wishlist(w["id"])
            except FetchFailed as e:
                # Same: HTTP/network failure on first page — keep prior state.
                last_error = f"fetch-failed: {label}: {e}"
                log.warning("Fetch failed on wishlist %s: %s", w["id"], e)
                counts[w["url"]] = 0
                _clear_pending_shrink(w["id"])
                _complete_wishlist(w["id"])
            except Exception as e:
                last_error = f"scrape failed: {label}: {e}"
                log.exception("Scrape failed for %s: %s", w["url"], e)
                counts[w["url"]] = 0
                _clear_pending_shrink(w["id"])
                _complete_wishlist(w["id"])
            else:
                try:
                    ingest_wishlist(w["id"], items)
                except SuspiciousShrink as e:
                    # Scrape "succeeded" but came back too short to trust. Same
                    # contract as the fetch failures above: keep the prior state,
                    # leave last_scraped_at alone so the row reads as stale.
                    last_error = f"short-scrape refused: {label}: {e}"
                    log.warning("Short scrape refused on wishlist %s: %s", w["id"], e)
                    counts[w["url"]] = 0
                    _complete_wishlist(w["id"])
                else:
                    _mark_scraped(w["id"])
                    counts[w["url"]] = len(items)
                    log.info("Ingested %d items for wishlist %s", len(items), w["id"])
                    _complete_wishlist(w["id"], items_added=len(items))

        if last_error:
            _progress_update(error=last_error)
    except Exception as e:
        _progress_update(error=str(e))
        raise
    finally:
        if pw_ctx is not None:
            try:
                pw_ctx["context"].close()
            except Exception:
                log.exception("Failed to close Playwright context cleanly")
            try:
                pw_ctx["browser"].close()
            except Exception:
                log.exception("Failed to close Playwright browser cleanly")
            try:
                pw_ctx["playwright"].stop()
            except Exception:
                log.exception("Failed to stop Playwright cleanly")
        _progress_update(
            running=False,
            finished_at=_now(),
            current_label=None,
            current_url=None,
            waiting=False,
            next_starts_at=None,
        )
    return counts


def resume_if_interrupted() -> None:
    """On startup, re-launch a scrape that a restart killed mid-run.

    The signal is a persisted run with a non-empty `pending_ids` queue: that
    only survives an abrupt process death (a normal finish drains the queue, and
    a login-expiry giving up clears it). Stale runs past RESUME_MAX_AGE_SEC are
    discarded — the daily cron covers those.
    """
    _load_progress()
    with _progress_lock:
        pending = list(_progress.get("pending_ids") or [])
        started_at = _progress.get("started_at")
    if not pending:
        return

    try:
        age = (datetime.now() - datetime.fromisoformat(started_at)).total_seconds()
    except (TypeError, ValueError):
        age = float("inf")
    if age > RESUME_MAX_AGE_SEC:
        log.warning("Discarding stale interrupted scrape (age %.0fs, %d pending)",
                    age, len(pending))
        _progress_update(running=False, pending_ids=[], waiting=False)
        return

    log.info("Found interrupted scrape with %d wishlist(s) pending; resuming", len(pending))
    threading.Thread(
        target=lambda: run_full_scrape(resume=True),
        name="scrape-resume",
        daemon=True,
    ).start()


def _load_progress() -> None:
    """Load persisted progress into `_progress` (best-effort) so a restart can
    see whether the previous run finished, and the status endpoint reflects it."""
    try:
        if not PROGRESS_PATH.is_file():
            return
        data = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("Failed to load persisted scrape progress from %s", PROGRESS_PATH)
        return
    if not isinstance(data, dict):
        return
    with _progress_lock:
        for k in _progress:
            if k in data:
                _progress[k] = data[k]


def _open_playwright_context_or_none() -> Optional[dict]:
    """Open a Playwright BrowserContext for this scrape run, or return None
    if Playwright path is disabled / unavailable (fall back to httpx).

    On import or runtime failure we log loudly and return None — the run
    proceeds via httpx instead of crashing.
    """
    if not use_playwright():
        return None
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as e:
        log.warning("Playwright import failed (%s); falling back to httpx", e)
        return None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=PLAYWRIGHT_HEADLESS)
        context = browser.new_context(storage_state=str(STORAGE_STATE))
        return {"playwright": pw, "browser": browser, "context": context}
    except Exception as e:
        log.warning("Playwright launch failed (%s); falling back to httpx", e)
        return None


# ---------- query helpers (latest snapshot per ASIN that's on a wishlist) ----------

_LATEST_BASE = """
WITH latest AS (
    SELECT s.*
    FROM price_snapshot s
    JOIN (
        SELECT asin, MAX(observed_at) AS max_t
        FROM price_snapshot
        GROUP BY asin
    ) m ON m.asin = s.asin AND m.max_t = s.observed_at
),
prev AS (
    SELECT s.asin, s.current_price_cents AS prev_price_cents
    FROM price_snapshot s
    JOIN (
        SELECT asin, MAX(observed_at) AS max_t
        FROM price_snapshot s2
        WHERE s2.observed_at < (
            SELECT MAX(observed_at) FROM price_snapshot s3 WHERE s3.asin = s2.asin
        )
        GROUP BY asin
    ) p ON p.asin = s.asin AND p.max_t = s.observed_at
),
high AS (
    SELECT asin, MAX(current_price_cents) AS highest_price_cents
    FROM price_snapshot
    WHERE current_price_cents IS NOT NULL
    GROUP BY asin
)
SELECT DISTINCT
    b.asin, b.title, b.author, b.product_url, b.purchased,
    l.current_price_cents, l.list_price_cents, l.availability, l.observed_at,
    pr.prev_price_cents,
    h.highest_price_cents
FROM latest l
JOIN book b ON b.asin = l.asin
JOIN wishlist_book wb ON wb.asin = l.asin
LEFT JOIN prev pr ON pr.asin = l.asin
LEFT JOIN high h ON h.asin = l.asin
"""


def _row_to_book(row, basis: Basis) -> BookRow:
    cur = row["current_price_cents"]
    if basis == "prev":
        base = row["prev_price_cents"]
    else:
        base = row["list_price_cents"]

    drop_dollar: Optional[float] = None
    drop_pct: Optional[float] = None
    if cur is not None and base is not None and base > cur:
        drop_dollar = (base - cur) / 100.0
        drop_pct = round((base - cur) * 100.0 / base, 2)

    return BookRow(
        asin=row["asin"],
        title=row["title"],
        author=row["author"],
        product_url=row["product_url"],
        current_price_cents=cur,
        list_price_cents=row["list_price_cents"],
        prev_price_cents=row["prev_price_cents"],
        availability=row["availability"],
        observed_at=row["observed_at"],
        drop_dollar=drop_dollar,
        drop_pct=drop_pct,
        purchased=bool(_row_get(row, "purchased")),
        highest_price_cents=_row_get(row, "highest_price_cents"),
    )


def _row_get(row, key, default=None):
    """Safely read a column from a sqlite3.Row; returns default if absent."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def deals(min_dollar: float, min_pct: float, basis: Basis) -> list[BookRow]:
    """Books currently on a wishlist whose latest snapshot beats the filters."""
    with connect() as conn:
        rows = conn.execute(_LATEST_BASE).fetchall()
    out: list[BookRow] = []
    for r in rows:
        if r["availability"] != "available":
            continue
        if r["purchased"]:
            continue
        b = _row_to_book(r, basis)
        if b.drop_dollar is None:
            continue
        if b.drop_dollar < min_dollar:
            continue
        if b.drop_pct is None or b.drop_pct < min_pct:
            continue
        out.append(b)
    out.sort(key=lambda x: (x.drop_pct or 0), reverse=True)
    return out


def all_books_by_price() -> tuple[list[BookRow], dict]:
    """Every available book on any wishlist, sorted by current price ascending.

    Returns (rows, summary) where summary has min/max price (cents) and count.
    Books currently without a purchase price are excluded — they live on /no-price.
    """
    with connect() as conn:
        rows = conn.execute(_LATEST_BASE).fetchall()
    out: list[BookRow] = []
    for r in rows:
        if r["availability"] != "available" or r["current_price_cents"] is None:
            continue
        if r["purchased"]:
            continue
        out.append(_row_to_book(r, "list"))
    out.sort(key=lambda b: b.current_price_cents or 0)

    summary: dict = {"count": len(out), "min_cents": None, "max_cents": None}
    if out:
        summary["min_cents"] = out[0].current_price_cents
        summary["max_cents"] = out[-1].current_price_cents
    return out, summary


def no_price_books() -> dict[str, list[BookRow]]:
    """Books on wishlists whose latest snapshot is unavailable, split by reason."""
    with connect() as conn:
        rows = conn.execute(_LATEST_BASE).fetchall()
    groups: dict[str, list[BookRow]] = {"kindle_unavailable": [], "page_404": []}
    for r in rows:
        if r["availability"] == "available":
            continue
        if r["purchased"]:
            continue
        b = _row_to_book(r, "list")
        groups.setdefault(r["availability"], []).append(b)
    return groups


def price_drop_history(
    min_dollar: float, min_pct: float, basis: Basis, limit: int = 5000
) -> list[BookRow]:
    """Every (asin, snapshot) pair where the snapshot dropped vs. baseline."""
    sql = """
    SELECT
        b.asin, b.title, b.author, b.product_url, b.purchased,
        s.current_price_cents, s.list_price_cents, s.availability, s.observed_at,
        (
            SELECT s2.current_price_cents
            FROM price_snapshot s2
            WHERE s2.asin = s.asin AND s2.observed_at < s.observed_at
            ORDER BY s2.observed_at DESC LIMIT 1
        ) AS prev_price_cents
    FROM price_snapshot s
    JOIN book b ON b.asin = s.asin
    JOIN wishlist_book wb ON wb.asin = s.asin
    WHERE b.purchased = 0
    ORDER BY s.observed_at DESC
    LIMIT 5000
    """
    with connect() as conn:
        rows = conn.execute(sql).fetchall()
    out: list[BookRow] = []
    seen_ids: set[tuple[str, str]] = set()
    for r in rows:
        key = (r["asin"], r["observed_at"])
        if key in seen_ids:
            continue
        seen_ids.add(key)
        b = _row_to_book(r, basis)
        if b.drop_dollar is None:
            continue
        if b.drop_dollar < min_dollar:
            continue
        if b.drop_pct is None or b.drop_pct < min_pct:
            continue
        out.append(b)
        if len(out) >= limit:
            break
    return out


def purchased_books() -> list[BookRow]:
    """Books marked as already purchased — independent of current wishlist membership."""
    sql = """
    WITH latest AS (
        SELECT s.*
        FROM price_snapshot s
        JOIN (
            SELECT asin, MAX(observed_at) AS max_t
            FROM price_snapshot
            GROUP BY asin
        ) m ON m.asin = s.asin AND m.max_t = s.observed_at
    )
    SELECT
        b.asin, b.title, b.author, b.product_url, b.purchased,
        l.current_price_cents, l.list_price_cents, l.availability, l.observed_at,
        NULL AS prev_price_cents
    FROM book b
    JOIN latest l ON l.asin = b.asin
    WHERE b.purchased = 1
    ORDER BY b.last_seen DESC
    """
    with connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_book(r, "list") for r in rows]


def set_book_purchased(asin: str, purchased: bool) -> bool:
    """Flip the purchased flag on a single book; returns the new value."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE book SET purchased = ? WHERE asin = ?",
            (1 if purchased else 0, asin),
        )
        if cur.rowcount == 0:
            raise KeyError(f"unknown asin: {asin}")
    return purchased
