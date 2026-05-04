"""End-to-end smoke test that exercises every page + the ingest + drop math
against a fake scraped payload (no network)."""
import os
import shutil
import tempfile
from pathlib import Path

# Use a throwaway DB
_tmp = Path(tempfile.mkdtemp(prefix="wishlist-smoke-"))
os.environ["WISHLIST_DB"] = str(_tmp / "test.db")
os.environ["WISHLIST_LOG"] = str(_tmp / "test.log")

from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.models import ScrapedItem
from app.services import (
    add_wishlist,
    deals,
    ingest_wishlist,
    no_price_books,
    price_drop_history,
)


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
