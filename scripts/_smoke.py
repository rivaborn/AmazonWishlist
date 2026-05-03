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

    # Hit every page through the HTTP layer
    paths = [
        "/",
        "/deals",
        "/no-price",
        "/price-drops",
        "/wishlists",
        "/deals?min_dollar=2&min_pct=20&basis=list",
    ]
    with TestClient(app) as c:
        for p in paths:
            r = c.get(p, follow_redirects=True)
            print(f"{p:50s} -> {r.status_code} ({len(r.text)} bytes)")
            assert r.status_code == 200, (p, r.status_code, r.text[:200])

    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
    raise SystemExit(rc)
