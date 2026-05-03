"""Business logic: ingest snapshots, query views, compute drops."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Literal, Optional

from .config import (
    CHROMIUM_USER_DATA_DIR,
    PLAYWRIGHT_HEADLESS,
    SCRAPE_PER_WISHLIST_SECONDS,
    STORAGE_STATE,
    use_playwright,
)
from .db import connect
from .models import BookRow, ScrapedItem
from .scraper import BotDetected, FetchFailed, LoginExpired, fetch_wishlist

log = logging.getLogger(__name__)

Basis = Literal["prev", "list"]


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
}


def get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def _progress_update(**kwargs) -> None:
    with _progress_lock:
        _progress.update(kwargs)


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
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                w.id, w.url, w.label, w.added_at, w.last_scraped_at,
                (SELECT COUNT(*) FROM wishlist_book wb WHERE wb.wishlist_id = w.id) AS last_item_count
            FROM wishlist w
            ORDER BY w.added_at
            """
        ).fetchall()
        return [dict(r) for r in rows]


def _mark_scraped(wishlist_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE wishlist SET last_scraped_at = ? WHERE id = ?",
            (_now(), wishlist_id),
        )


def ingest_wishlist(wishlist_id: int, items: list[ScrapedItem]) -> None:
    """Replace the wishlist's membership and append a price snapshot per item."""
    now = _now()
    with connect() as conn:
        conn.execute("BEGIN")
        try:
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


def run_full_scrape() -> dict[str, int]:
    """Scrape every registered wishlist; return per-wishlist item counts.

    Updates the in-memory progress snapshot at every step so the UI can poll.
    Idempotent against concurrent calls only via the API guard
    (`POST /api/scrape/run` rejects if `running` is True).
    """
    wishlists = list_wishlists()
    interval = max(0, SCRAPE_PER_WISHLIST_SECONDS)
    _progress_update(
        running=True,
        started_at=_now(),
        finished_at=None,
        total=len(wishlists),
        done=0,
        current_label=None,
        current_url=None,
        items_total=0,
        error=None,
        waiting=False,
        next_starts_at=None,
    )
    counts: dict[str, int] = {}
    last_error: Optional[str] = None

    # Decide which scraper path to use *once* per run, opening one Playwright
    # context (if applicable) for the whole run instead of paying chromium
    # spin-up cost per wishlist.
    pw_ctx = _open_playwright_context_or_none()
    log.info("Scraper path: %s", "playwright" if pw_ctx is not None else "httpx")

    try:
        for idx, w in enumerate(wishlists):
            wishlist_started = time.monotonic()
            label = w.get("label") or w["url"]
            _progress_update(
                current_label=label,
                current_url=w["url"],
                waiting=False,
                next_starts_at=None,
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
                with _progress_lock:
                    _progress["done"] += 1
                # No point continuing once the saved session is dead.
                break
            except BotDetected as e:
                # Don't ingest — preserve previous count + timestamp.
                last_error = f"bot-blocked: {label}"
                log.warning("Bot-blocked on wishlist %s: %s", w["id"], e)
                counts[w["url"]] = 0
                with _progress_lock:
                    _progress["done"] += 1
            except FetchFailed as e:
                # Same: HTTP/network failure on first page — keep prior state.
                last_error = f"fetch-failed: {label}: {e}"
                log.warning("Fetch failed on wishlist %s: %s", w["id"], e)
                counts[w["url"]] = 0
                with _progress_lock:
                    _progress["done"] += 1
            except Exception as e:
                last_error = f"scrape failed: {label}: {e}"
                log.exception("Scrape failed for %s: %s", w["url"], e)
                counts[w["url"]] = 0
                with _progress_lock:
                    _progress["done"] += 1
            else:
                ingest_wishlist(w["id"], items)
                _mark_scraped(w["id"])
                counts[w["url"]] = len(items)
                log.info("Ingested %d items for wishlist %s", len(items), w["id"])
                with _progress_lock:
                    _progress["done"] += 1
                    _progress["items_total"] += len(items)

            # Pace: at most one wishlist start per `interval` seconds.
            is_last = idx == len(wishlists) - 1
            if not is_last and interval > 0:
                wait_seconds = (wishlist_started + interval) - time.monotonic()
                if wait_seconds > 0:
                    next_at = (
                        datetime.now() + timedelta(seconds=wait_seconds)
                    ).isoformat(timespec="seconds")
                    log.info("Waiting %ds before next wishlist (until %s)",
                             int(wait_seconds), next_at)
                    _progress_update(
                        waiting=True,
                        current_label=f"Waiting until {next_at[11:19]} for next wishlist",
                        next_starts_at=next_at,
                    )
                    # Sleep in slices so the progress endpoint stays fresh
                    # if the dict shape ever changes mid-wait.
                    end = time.monotonic() + wait_seconds
                    while True:
                        remaining = end - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(min(remaining, 5.0))
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
)
SELECT DISTINCT
    b.asin, b.title, b.author, b.product_url, b.purchased,
    l.current_price_cents, l.list_price_cents, l.availability, l.observed_at,
    pr.prev_price_cents
FROM latest l
JOIN book b ON b.asin = l.asin
JOIN wishlist_book wb ON wb.asin = l.asin
LEFT JOIN prev pr ON pr.asin = l.asin
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
