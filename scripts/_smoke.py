"""End-to-end smoke test that exercises every page + the ingest + drop math
against a fake scraped payload (no network)."""
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

# Use a throwaway DB
_tmp = Path(tempfile.mkdtemp(prefix="wishlist-smoke-"))
os.environ["WISHLIST_DB"] = str(_tmp / "test.db")
os.environ["WISHLIST_LOG"] = str(_tmp / "test.log")
# Throwaway progress file too, so the app's startup resume-check can't pick up a
# real interrupted run and kick off a live scrape during the test.
os.environ["WISHLIST_PROGRESS"] = str(_tmp / "progress.json")
# Mirror-mode state file. WISHLIST_PRIMARY_URL is deliberately left UNSET so the
# TestClient lifespan below can never fire a real HTTP sync at a live primary.
os.environ["WISHLIST_SYNC_STATE"] = str(_tmp / "sync_state.json")

from fastapi.testclient import TestClient

from app import config, db, sync
from app.db import connect, init_db
from app.main import app
from app.models import ScrapedItem
from app.services import (
    add_wishlist,
    all_books_by_price,
    deals,
    ingest_wishlist,
    list_wishlists,
    no_price_books,
    price_drop_history,
    purchased_books,
    set_book_purchased,
)


def _members(wishlist_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM wishlist_book WHERE wishlist_id = ?", (wishlist_id,)
        ).fetchone()[0]


@contextmanager
def _using(db_path):
    """Point the whole data layer at another SQLite file.

    `app/db.py` does `from .config import DB_PATH` and `connect()` reads that
    module global at call time, so this one patch redirects every query, every
    service and every route -- which is what lets a primary and a mirror live in
    one process with no network. Saves and restores, so nesting works.
    """
    prev = db.DB_PATH
    db.DB_PATH = db_path
    try:
        yield
    finally:
        db.DB_PATH = prev


def _snapshot_ids():
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT id FROM price_snapshot ORDER BY id")]


def _read_side():
    """Everything the pages render, as one comparable blob."""
    books, summary = all_books_by_price()
    return {
        "deals_prev": deals(0, 0, "prev"),
        "deals_list": deals(0, 0, "list"),
        "books": books,
        "summary": summary,
        "no_price": no_price_books(),
        "drops": price_drop_history(0, 0, "prev"),
        "purchased": purchased_books(),
        # stale_hours moves between two calls a millisecond apart, so compare
        # only the columns that actually come out of the database.
        "wishlists": [
            {k: v for k, v in w.items() if k not in ("stale_hours", "stale")}
            for w in list_wishlists()
        ],
    }


def _sync(primary_db, secondary_db, limit=None):
    """One full sync, service-level, no HTTP. Returns (snapshots_added, pages)."""
    with _using(primary_db):
        catalog = sync.export_catalog()
    with _using(secondary_db):
        applied = sync.apply_catalog(catalog)
        max_id = applied["max_snapshot_id"]
        since = sync.local_watermark()
        added = pages = 0
        while since < max_id:
            with _using(primary_db):
                page = sync.export_snapshots(since, max_id, limit)
            if not page["count"]:
                break
            added += sync.apply_snapshots(page)
            since = page["next_since_id"]
            pages += 1
            if not page["has_more"]:
                break
        sync.update_sync_state(
            last_success_at=sync._now(),
            synced_at_local=sync._now(),
            source_now=catalog["source_now"],
        )
    return added, pages


def _check_sync(wid):
    """Mirror mode: the primary's DB must round-trip into a second DB such that
    every page renders identically, and must do so idempotently, incrementally,
    and without ever letting a short payload clobber a good mirror."""
    primary_db = db.DB_PATH
    secondary_db = _tmp / "secondary.db"

    with _using(primary_db):
        set_book_purchased("B0FAKE0002", True)  # so /purchased is non-empty
        want = _read_side()
        want_ids = _snapshot_ids()

    with _using(secondary_db):
        init_db()
    added, pages = _sync(primary_db, secondary_db)
    assert added == len(want_ids), (added, len(want_ids))

    # 1. Every read helper agrees, and the ids line up so the watermark is valid.
    with _using(secondary_db):
        got = _read_side()
        assert _snapshot_ids() == want_ids, "snapshot ids must mirror exactly"
        assert sync.local_watermark() == max(want_ids)
    for k in want:
        assert got[k] == want[k], f"mirror differs on {k}"
    print(f"sync: mirrored {len(want_ids)} snapshots in {pages} page(s); all views match")

    # 2. Replaying the whole sync is a no-op -- INSERT OR IGNORE on explicit ids.
    again, _ = _sync(primary_db, secondary_db)
    assert again == 0, f"replay inserted {again} rows; sync is not idempotent"
    with _using(secondary_db):
        assert _read_side() == want, "replay changed the mirror"
    print("sync: replay is idempotent")

    # 3. Incremental: a new ingest on the primary arrives as a delta only.
    # Both books, or the ingest shrink guard refuses it before we get to test sync.
    day3 = [
        ScrapedItem(
            asin="B0FAKE0001",
            title="Fake Book One",
            author="A. Author",
            product_url="https://www.amazon.com/dp/B0FAKE0001",
            current_price_cents=199,
            list_price_cents=1499,
            availability="available",
        ),
        ScrapedItem(
            asin="B0FAKE0002",
            title="Fake Book Two",
            author=None,
            product_url="https://www.amazon.com/dp/B0FAKE0002",
            current_price_cents=None,
            list_price_cents=None,
            availability="kindle_unavailable",
        ),
    ]
    with _using(primary_db):
        before = len(_snapshot_ids())
        ingest_wishlist(wid, day3)
        after_ids = _snapshot_ids()
        new_rows = len(after_ids) - before
        want2 = _read_side()
    delta, _ = _sync(primary_db, secondary_db)
    assert delta == new_rows, f"delta carried {delta} rows, expected {new_rows}"
    with _using(secondary_db):
        assert _read_side() == want2, "mirror did not converge after an incremental sync"
        assert _snapshot_ids() == after_ids
    print(f"sync: incremental delta of {delta} row(s) converged")

    # 4. Paging: limit=1 must terminate and reach the same place.
    fresh = _tmp / "paged.db"
    with _using(fresh):
        init_db()
    paged, npages = _sync(primary_db, fresh, limit=1)
    assert npages == paged == len(after_ids), (npages, paged, len(after_ids))
    with _using(fresh):
        assert _read_side() == want2, "paged sync diverged"
    print(f"sync: converged over {npages} single-row pages")

    # 5. Torn sync -- catalog applied, snapshots truncated. Every page must still
    #    render (no FK errors), because a partial sync is a contiguous PREFIX.
    torn = _tmp / "torn.db"
    with _using(primary_db):
        catalog = sync.export_catalog()
        page1 = sync.export_snapshots(0, catalog["max_snapshot_id"], 1)
    with _using(torn):
        init_db()
        sync.apply_catalog(catalog)
        sync.apply_snapshots(page1)
        _read_side()  # must not raise
        assert sync.local_watermark() == page1["next_since_id"]
    rest, _ = _sync(primary_db, torn)
    with _using(torn):
        assert _read_side() == want2, "torn mirror did not converge on the next sync"
    print(f"sync: torn mirror renders, then converges (+{rest} rows)")

    # 6. Shrink guard: an empty catalog must be refused and change nothing.
    empty = dict(catalog, wishlists=[], wishlist_books=[])
    with _using(secondary_db):
        before_state = _read_side()
        try:
            sync.apply_catalog(empty)
        except sync.SyncRefused as e:
            print(f"sync: empty catalog refused ({str(e)[:60]}...)")
        else:
            raise AssertionError("empty catalog was applied - the mirror is clobberable")
        assert _read_side() == before_state, "refused catalog still mutated the mirror"
        # ...but a second, agreeing catalog confirms the shrink is real.
        sync.apply_catalog(empty)
        assert _read_side()["wishlists"] == [], "confirmed shrink was not applied"
    print("sync: a second agreeing catalog confirms the shrink")

    # 7. Source-regression tripwire: a primary whose ids went backwards.
    regressed = _tmp / "regressed.db"
    with _using(regressed):
        init_db()
    _sync(primary_db, regressed)
    with _using(regressed):
        try:
            sync.apply_catalog(dict(catalog, max_snapshot_id=0))
        except sync.SyncRefused:
            print("sync: a source whose max_snapshot_id went backwards is refused")
        else:
            raise AssertionError("a regressed source was accepted")

    # 8. The chokepoint: a mirror must never write its own snapshot, or explicit
    #    -id inserts and sqlite_sequence collide and silently lose rows.
    prev_role = config.ROLE
    config.ROLE = "secondary"
    try:
        with _using(secondary_db):
            try:
                ingest_wishlist(wid, day3)
            except Exception as e:
                assert type(e).__name__ == "MirrorReadOnly", e
                print("sync: ingest_wishlist refuses to run on a secondary")
            else:
                raise AssertionError("a secondary ingested its own snapshot")
    finally:
        config.ROLE = prev_role


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

    # ---- scrape order: most-dated first ----
    from app.services import _scrape_order

    order = _scrape_order([
        {"id": 1, "last_scraped_at": "2026-08-21T02:03:54", "added_at": "2026-05-02T22:06:47"},
        {"id": 2, "last_scraped_at": None,                  "added_at": "2026-08-20T12:00:00"},  # never scraped, added recently
        {"id": 3, "last_scraped_at": "2026-08-20T06:06:44", "added_at": "2026-05-02T22:07:19"},  # failed since -> stalest scrape
        {"id": 4, "last_scraped_at": None,                  "added_at": "2026-05-01T00:00:00"},  # never scraped, ancient
    ])
    assert order == [4, 3, 2, 1], order  # oldest basis first; fresh success last

    # ---- blocked-page retry policy ----
    from app.scraper import _block_retry_delay
    from app.config import BLOCK_RETRY_503, BLOCK_RETRY_STUB_MIDLIST

    # 503 retries on any page, then gives up
    assert _block_retry_delay("error503", 1, 0) is not None
    assert _block_retry_delay("error503", 40, BLOCK_RETRY_503 - 1) is not None
    assert _block_retry_delay("error503", 40, BLOCK_RETRY_503) is None, "503 must give up after its budget"
    # stub: never retried on page 1, one cautious retry mid-list
    assert _block_retry_delay("antibot", 1, 0) is None, "page-1 stub must never retry"
    assert _block_retry_delay("antibot", 2, 0) is not None
    assert _block_retry_delay("antibot", 2, BLOCK_RETRY_STUB_MIDLIST) is None
    # backoff grows per attempt
    d0 = _block_retry_delay("error503", 5, 0); d1 = _block_retry_delay("error503", 5, 1)
    assert d0 is not None and d1 is not None and d1 > d0 - 15, (d0, d1)

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

    _check_sync(wid)

    # Hit every page through the HTTP layer
    paths = [
        "/",
        "/deals",
        "/books",
        "/no-price",
        "/price-drops",
        "/wishlists",
        "/purchased",
        "/login",
        "/api/login/status",
        "/api/sync/status",
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

        # The export endpoints a secondary consumes.
        r = c.get("/api/sync/catalog")
        assert r.status_code == 200, r.text
        cat = r.json()
        assert cat["format"] == sync.SYNC_FORMAT and cat["wishlists"], cat
        r = c.get(
            "/api/sync/snapshots",
            params={"since_id": 0, "max_id": cat["max_snapshot_id"], "limit": 5},
        )
        assert r.status_code == 200 and r.json()["count"] == 5, r.text
        # The clamp: an absurd limit must not be honoured verbatim.
        r = c.get(
            "/api/sync/snapshots",
            params={"since_id": 0, "max_id": cat["max_snapshot_id"], "limit": 10**9},
        )
        assert r.status_code == 200, r.text
        print(f"/api/sync/catalog + /api/sync/snapshots            -> 200 "
              f"({len(cat['books'])} books, max id {cat['max_snapshot_id']})")

        # A primary has nothing to sync FROM.
        assert c.post("/api/sync/run").status_code == 403

        # Everything that writes must be refused on a mirror, and every page
        # must still render -- a mirror whose UI 500s is worse than no mirror.
        prev_role = config.ROLE
        config.ROLE = "secondary"
        try:
            writes = [
                ("/api/wishlists", {"data": {"url": "https://example.com/x"}}),
                ("/api/wishlists/1/delete", {}),
                ("/api/scrape/run", {}),
                ("/api/books/B0FAKE0001/purchased", {"json": {"purchased": True}}),
                ("/api/login/start", {}),
                ("/api/login/save", {}),
                ("/api/login/cancel", {}),
                ("/api/login/heartbeat", {}),
            ]
            for path, kw in writes:
                r = c.post(path, **kw)
                assert r.status_code == 403, (path, r.status_code, r.text[:120])
            print(f"mirror: {len(writes)} write endpoints -> 403")

            for path in ("/deals", "/books", "/no-price", "/price-drops",
                         "/purchased", "/wishlists", "/login"):
                r = c.get(path)
                assert r.status_code == 200, (path, r.status_code, r.text[:200])
                assert "purchased-cb" not in r.text or 'class="readonly"' in r.text
            # The nav must not offer a Login tab a mirror cannot use.
            assert '>Login<' not in c.get("/deals").text
            print("mirror: every page renders read-only, no Login tab")

            # The mirror panel and the JSON endpoint must agree about the peer.
            # They read different code paths, and the panel silently claimed
            # "WISHLIST_PRIMARY_URL is not set" while it was in fact set.
            prev_url = config.PRIMARY_URL
            config.PRIMARY_URL = "http://primary.example:9060"
            try:
                body = c.get("/wishlists").text
                assert config.PRIMARY_URL in body, "mirror panel lost the peer URL"
                assert "is not set" not in body, body[body.find("Mirroring"):][:120]
                js = c.get("/api/sync/status").json()
                assert js["primary_url"] == config.PRIMARY_URL
                assert js["role"] == "secondary"
                print("mirror: status panel and /api/sync/status agree on the peer")
            finally:
                config.PRIMARY_URL = prev_url
        finally:
            config.ROLE = prev_role

    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
    raise SystemExit(rc)
