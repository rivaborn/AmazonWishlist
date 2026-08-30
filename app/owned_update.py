"""Refresh grimmory.db from Grimmory and reconcile owned books.

This is the "Update Owned Books" operation: it (1) re-pulls the local
``grimmory.db`` snapshot from the Grimmory (BookLore) app, (2) flags every
wishlist book owned in Grimmory as ``purchased`` (so they move to the
Purchased tab and drop out of the deal views), and (3) re-derives the BookBub
deals ``owned_in_grimmory`` flags.

It runs on the HOST (the webapp context — NOT the wlvpn netns, from which the
Grimmory server is unreachable). Triggered from the Settings tab ("Update
Owned Books" button) and by the scheduler's monthly cron. Requires the
GRIMMORY_USERNAME / GRIMMORY_PASSWORD env settings (read by
``app.grimmory.login``); without them the run reports a clean error and
changes nothing.

The run is long (re-fetching ~37k library books), so the scheduler/settings
trigger spawns it in a daemon thread, guarded by a lock (only one run at a
time), and mirrors status to an in-memory dict the Settings page reads.
"""
import logging
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger("owned_update")

# Make scripts/ importable so we can reuse build_grimmory_db.build() (the
# script's import root is its own parent, so importing it also works).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

from . import config, deals_db, grimmory  # noqa: E402
from build_grimmory_db import build as _build_grimmory_db  # noqa: E402

_STATUS = {
    "running": False,
    "started_at": None,
    "last_success_at": None,
    "last_result": None,   # {grimmory_books, marked_purchased, deals_refreshed}
    "last_error": None,
}
_LOCK = threading.Lock()


def owned_update_status() -> dict:
    """Snapshot of the last/pending run for the Settings page (never blocks)."""
    with _LOCK:
        return dict(_STATUS)


def _mark_owned_purchased() -> int:
    """Flag every wishlist book owned in Grimmory as purchased; returns count."""
    g = sqlite3.connect(f"file:{Path(config.GRIMMORY_DB).as_posix()}?mode=ro", uri=True)
    try:
        grimm = g.execute("SELECT title, author FROM book").fetchall()
    finally:
        g.close()
    index = deals_db._build_owned_index(grimm)
    d = sqlite3.connect(config.DB_PATH)
    try:
        books = d.execute(
            "SELECT asin, title, author, purchased FROM book"
        ).fetchall()
        to_mark = [
            asin for (asin, title, author, p) in books
            if not p and deals_db._is_owned(index, title, author)
        ]
        if to_mark:
            d.executemany("UPDATE book SET purchased = 1 WHERE asin = ?",
                          [(a,) for a in to_mark])
            d.commit()
        return len(to_mark)
    finally:
        d.close()


def _refresh_deals_owned() -> int:
    """Re-derive the BookBub deals owned_in_grimmory flags; returns rows written."""
    conn = deals_db.connect(config.DEALS_DB)
    deals_db.ensure_schema(conn)
    try:
        n = deals_db.refresh_owned(conn, config.GRIMMORY_DB)
        conn.commit()
        return n
    finally:
        conn.close()


def update_owned_books_sync() -> dict:
    """The full operation (blocks until done). Raises on failure."""
    token = grimmory.login()                       # GRIMMORY_USERNAME/PASSWORD
    per_library = _build_grimmory_db(token, config.GRIMMORY_DB)  # rebuild grimmory.db
    marked = _mark_owned_purchased()
    deals = _refresh_deals_owned()
    return {
        "grimmory_books": sum(per_library.values()) if per_library else 0,
        "marked_purchased": marked,
        "deals_refreshed": deals,
    }


def trigger_owned_update() -> bool:
    """Start the update in a background thread if one isn't already running.

    Returns True when a run was started, False when one is already in flight.
    """
    with _LOCK:
        if _STATUS["running"]:
            return False
        _STATUS.update(running=True, started_at=datetime.now().isoformat(timespec="seconds"),
                       last_error=None)

    def _run() -> None:
        try:
            result = update_owned_books_sync()
        except Exception as e:  # surface a clean error to the Settings page
            log.exception("owned-books update failed")
            with _LOCK:
                _STATUS.update(running=False, last_error=str(e))
            return
        with _LOCK:
            _STATUS.update(
                running=False,
                last_success_at=datetime.now().isoformat(timespec="seconds"),
                last_result=result,
                last_error=None,
            )

    threading.Thread(target=_run, daemon=True, name="owned-update").start()
    return True
