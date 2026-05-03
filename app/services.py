"""Business logic: ingest snapshots, query views, compute drops."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Literal, Optional

from .db import connect
from .models import BookRow, ScrapedItem
from .scraper import fetch_wishlist

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
            "SELECT id, url, label, added_at, last_scraped_at FROM wishlist ORDER BY added_at"
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
    )
    counts: dict[str, int] = {}
    try:
        for w in wishlists:
            label = w.get("label") or w["url"]
            _progress_update(current_label=label, current_url=w["url"])
            log.info("Scraping wishlist %s (%s)", w["id"], w["url"])
            try:
                items = fetch_wishlist(w["url"])
            except Exception as e:
                log.exception("Scrape failed for %s: %s", w["url"], e)
                counts[w["url"]] = 0
                with _progress_lock:
                    _progress["done"] += 1
                continue
            ingest_wishlist(w["id"], items)
            _mark_scraped(w["id"])
            counts[w["url"]] = len(items)
            log.info("Ingested %d items for wishlist %s", len(items), w["id"])
            with _progress_lock:
                _progress["done"] += 1
                _progress["items_total"] += len(items)
    except Exception as e:
        _progress_update(error=str(e))
        raise
    finally:
        _progress_update(
            running=False,
            finished_at=_now(),
            current_label=None,
            current_url=None,
        )
    return counts


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
    b.asin, b.title, b.author, b.product_url,
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
    )


def deals(min_dollar: float, min_pct: float, basis: Basis) -> list[BookRow]:
    """Books currently on a wishlist whose latest snapshot beats the filters."""
    with connect() as conn:
        rows = conn.execute(_LATEST_BASE).fetchall()
    out: list[BookRow] = []
    for r in rows:
        if r["availability"] != "available":
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


def no_price_books() -> dict[str, list[BookRow]]:
    """Books on wishlists whose latest snapshot is unavailable, split by reason."""
    with connect() as conn:
        rows = conn.execute(_LATEST_BASE).fetchall()
    groups: dict[str, list[BookRow]] = {"kindle_unavailable": [], "page_404": []}
    for r in rows:
        if r["availability"] == "available":
            continue
        b = _row_to_book(r, "list")
        groups.setdefault(r["availability"], []).append(b)
    return groups


def price_drop_history(
    min_dollar: float, min_pct: float, basis: Basis, limit: int = 500
) -> list[BookRow]:
    """Every (asin, snapshot) pair where the snapshot dropped vs. baseline."""
    sql = """
    SELECT
        b.asin, b.title, b.author, b.product_url,
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
