"""End-to-end smoke test that exercises every page + the ingest + drop math
against a fake scraped payload (no network)."""
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Use a throwaway DB
_tmp = Path(tempfile.mkdtemp(prefix="wishlist-smoke-"))
os.environ["WISHLIST_DB"] = str(_tmp / "test.db")
os.environ["WISHLIST_LOG"] = str(_tmp / "test.log")
# Throwaway progress file too, so the app's startup resume-check can't pick up a
# real interrupted run and kick off a live scrape during the test.
os.environ["WISHLIST_PROGRESS"] = str(_tmp / "progress.json")

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import app
from app.models import ScrapedItem
from app.services import (
    add_wishlist,
    deals,
    ingest_wishlist,
    no_price_books,
    price_drop_history,
)


def _members(wishlist_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM wishlist_book WHERE wishlist_id = ?", (wishlist_id,)
        ).fetchone()[0]


def main() -> int:
    init_db()
    wid = add_wishlist("https://www.amazon.com/hz/wishlist/ls/FAKETEST", "smoke")

    # Day 1: book at $9.99 with $14.99 list price; second book unavailable.
    day1 = [
        ScrapedItem(
            asin="B0FAKE0001",
            title="Test Book One",
            author="Anon Author",
            product_url="https://www.amazon.com/dp/B0FAKE0001",
            current_price_cents=999,
            list_price_cents=1499,
            availability="available",
        ),
        ScrapedItem(
            asin="B0FAKE0002",
            title="Out Of Print Book",
            author=None,
            product_url="https://www.amazon.com/dp/B0FAKE0002",
            current_price_cents=None,
            list_price_cents=None,
            availability="kindle_unavailable",
        ),
    ]
    ingest_wishlist(wid, day1)

    # Day 2: book one drops to $4.99
    day2 = [
        ScrapedItem(
            asin="B0FAKE0001",
            title="Test Book One",
            author="Anon Author",
            product_url="https://www.amazon.com/dp/B0FAKE0001",
            current_price_cents=499,
            list_price_cents=1499,
            availability="available",
        ),
        ScrapedItem(
            asin="B0FAKE0002",
            title="Out Of Print Book",
            author=None,
            product_url="https://www.amazon.com/dp/B0FAKE0002",
            current_price_cents=None,
            list_price_cents=None,
            availability="kindle_unavailable",
        ),
    ]
    ingest_wishlist(wid, day2)

    d_prev = deals(0, 0, "prev")
    d_list = deals(0, 0, "list")
    assert len(d_prev) == 1 and d_prev[0].asin == "B0FAKE0001", d_prev
    assert abs(d_prev[0].drop_dollar - 5.0) < 0.001
    assert len(d_list) == 1 and abs(d_list[0].drop_dollar - 10.0) < 0.001

    # Filter cuts off the 33% drop vs prev when min_pct=80
    assert deals(0, 80, "prev") == []

    np = no_price_books()
    assert len(np["kindle_unavailable"]) == 1 and np["kindle_unavailable"][0].asin == "B0FAKE0002"
    assert np["page_404"] == []

    history = price_drop_history(0, 0, "prev")
    assert any(r.asin == "B0FAKE0001" for r in history), history

    # last_scraped_at + last_item_count are exposed by list_wishlists
    from app.services import _mark_scraped, all_books_by_price, get_progress, list_wishlists
    _mark_scraped(wid)
    rows = list_wishlists()
    assert rows[0]["last_scraped_at"] is not None, rows
    # day2 had two items: one available, one kindle_unavailable -> wishlist_book has 2
    assert rows[0]["last_item_count"] == 2, rows
    # previous_item_count = membership count captured before day2 ingest = 2 (from day1)
    assert rows[0]["previous_item_count"] == 2, rows

    # All-books view: only the available one with a price; summary shows it
    books, summary = all_books_by_price()
    assert len(books) == 1 and books[0].asin == "B0FAKE0001", books
    assert summary["count"] == 1
    assert summary["min_cents"] == 499 and summary["max_cents"] == 499, summary
    # Highest price reflects MAX across all snapshots: day1 was 999, day2 was 499
    assert books[0].highest_price_cents == 999, books[0]
    assert d_prev[0].highest_price_cents == 999, d_prev[0]
    # progress snapshot is callable and shape-stable
    snap = get_progress()
    for k in ("running", "started_at", "finished_at", "total", "done",
              "current_label", "current_url", "items_total", "error"):
        assert k in snap, snap

    # ---- pagination: the cap is a failure, a natural end is not ----
    from app.scraper import FetchFailed, _check_pagination_complete
    from app.config import MAX_PAGES_PER_WISHLIST

    # Stopped because Amazon offered no next page -> complete, no raise.
    _check_pagination_complete("https://x/list", MAX_PAGES_PER_WISHLIST, None, 500)
    # Stopped on the page budget with more pages still offered -> partial.
    try:
        _check_pagination_complete("https://x/list", MAX_PAGES_PER_WISHLIST, "https://x/p2", 500)
    except FetchFailed:
        pass
    else:
        raise AssertionError("hitting the page cap mid-list must raise FetchFailed")

    # ---- end-of-list vs zero-rows: duplicates stop cleanly, empties fail ----
    from app.scraper import _PaginationTracker
    from app.config import MAX_STALE_PAGES

    t = _PaginationTracker("https://x/list")
    for i in range(MAX_STALE_PAGES - 1):
        assert t.note_page(page_count=i + 2, new_count=0, row_count=10, item_count=100) is False
    assert t.note_page(page_count=MAX_STALE_PAGES + 1, new_count=0, row_count=10, item_count=100) is True

    t = _PaginationTracker("https://x/list")
    try:
        for i in range(MAX_STALE_PAGES):
            t.note_page(page_count=i + 2, new_count=0, row_count=0, item_count=100)
    except FetchFailed:
        pass
    else:
        raise AssertionError("consecutive zero-row pages must raise FetchFailed")

    # A page that adds something resets both counters.
    t = _PaginationTracker("https://x/list")
    t.note_page(page_count=2, new_count=0, row_count=0, item_count=100)
    t.note_page(page_count=3, new_count=5, row_count=10, item_count=105)
    assert t.empty_pages == 0 and t.stale_pages == 0

    # ---- block-page classifier: three shapes, three verdicts ----
    from app.scraper import _classify_block_page

    robot_stub = "<html><body>To discuss automated access to Amazon data please contact api-services-support@amazon.com.</body></html>"
    dog_503 = (
        "<html><head><title>Sorry! Something went wrong!</title></head><body>"
        '<a href="/ref=cs_503_logo">Amazon.com</a>'
        '<img src="https://images-na.ssl-images-amazon.com/images/G/01/error/500_503.png">'
        "</body></html>"
    )
    benign_empty = "<html><body><ul id='g-items'></ul></body></html>"
    huge_mention = "<html><body>" + ("x" * 40_000) + " ref=cs_503_link </body></html>"

    assert _classify_block_page(robot_stub) == "antibot"
    assert _classify_block_page(dog_503) == "error503"
    assert _classify_block_page(benign_empty) is None, "empty end-of-list page must not classify as a block"
    assert _classify_block_page(huge_mention) is None, "size gate must stop marker mentions in real pages"

    # ---- ingest refuses a short scrape once, then accepts the confirmed shrink ----
    from app.services import SuspiciousShrink
    shrink_wid = add_wishlist("https://www.amazon.com/hz/wishlist/ls/FAKESHRINK", "shrink")
    full = [
        ScrapedItem(
            asin=f"B0SHRINK{i:03d}",
            title=f"Book {i}",
            author=None,
            product_url=f"https://www.amazon.com/dp/B0SHRINK{i:03d}",
            current_price_cents=500 + i,
            list_price_cents=None,
            availability="available",
        )
        for i in range(100)
    ]
    ingest_wishlist(shrink_wid, full)
    assert _members(shrink_wid) == 100

    # 50 of 100 is under the 0.8 floor -> refused, membership untouched.
    try:
        ingest_wishlist(shrink_wid, full[:50])
    except SuspiciousShrink:
        pass
    else:
        raise AssertionError("a scrape at 50% of stored membership must be refused")
    assert _members(shrink_wid) == 100, "refused scrape must not touch membership"

    # A second short scrape confirms it -> accepted.
    ingest_wishlist(shrink_wid, full[:50])
    assert _members(shrink_wid) == 50, "a confirmed shrink must be accepted"

    # A normal-sized scrape clears the marker, so the guard re-arms.
    ingest_wishlist(shrink_wid, full[:50])
    try:
        ingest_wishlist(shrink_wid, full[:20])
    except SuspiciousShrink:
        pass
    else:
        raise AssertionError("guard must re-arm after a normal scrape")
    assert _members(shrink_wid) == 50

    # A second short scrape that DISAGREES with the recorded one is refused
    # again (two different truncation points are not a confirmation)...
    try:
        ingest_wishlist(shrink_wid, full[:35])  # short vs 50, disagrees with 20
    except SuspiciousShrink:
        pass
    else:
        raise AssertionError("a disagreeing second short scrape must be refused")
    assert _members(shrink_wid) == 50
    # ...and once two consecutive shorts agree, the shrink is accepted.
    ingest_wishlist(shrink_wid, full[:35])
    assert _members(shrink_wid) == 35, "an agreeing confirmed shrink must be accepted"

    # ---- staleness is derived and exposed ----
    from app.config import STALE_AFTER_HOURS
    fresh = [r for r in list_wishlists() if r["id"] == wid][0]
    assert fresh["stale"] is False, fresh
    assert fresh["stale_hours"] is not None and fresh["stale_hours"] < 1, fresh

    old_ts = (datetime.now() - timedelta(hours=STALE_AFTER_HOURS + 12)).isoformat()
    with connect() as conn:
        conn.execute("UPDATE wishlist SET last_scraped_at = ? WHERE id = ?", (old_ts, wid))
    stale_row = [r for r in list_wishlists() if r["id"] == wid][0]
    assert stale_row["stale"] is True, stale_row
    assert stale_row["stale_hours"] > STALE_AFTER_HOURS, stale_row
    # The counts beside it are unchanged -- which is exactly why the flag exists.
    assert stale_row["last_item_count"] == stale_row["previous_item_count"] == 2, stale_row
    _mark_scraped(wid)

    # A wishlist never successfully scraped goes stale from its added_at.
    never_wid = add_wishlist("https://www.amazon.com/hz/wishlist/ls/FAKENEVER", "never")
    old_added = (datetime.now() - timedelta(hours=STALE_AFTER_HOURS + 5)).isoformat()
    with connect() as conn:
        conn.execute("UPDATE wishlist SET added_at = ? WHERE id = ?", (old_added, never_wid))
    never_row = [r for r in list_wishlists() if r["id"] == never_wid][0]
    assert never_row["last_scraped_at"] is None and never_row["stale"] is True, never_row

    # Hit every page through the HTTP layer
    paths = [
        "/",
        "/deals",
        "/books",
        "/no-price",
        "/price-drops",
        "/wishlists",
        "/login",
        "/api/login/status",
        "/deals?min_dollar=2&min_pct=20&basis=list",
    ]
    with TestClient(app) as c:
        for p in paths:
            r = c.get(p, follow_redirects=True)
            print(f"{p:50s} -> {r.status_code} ({len(r.text)} bytes)")
            assert r.status_code == 200, (p, r.status_code, r.text[:200])

        # /api/scrape/status returns the progress shape, even with no scrape yet
        r = c.get("/api/scrape/status")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "running" in data and "done" in data and "total" in data, data
        print(f"/api/scrape/status                                 -> {r.status_code} ({data})")

    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
    raise SystemExit(rc)
