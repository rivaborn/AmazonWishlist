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

That status dict is also the progress feed for the Settings tab's bar and
activity window: the run is divided into a known number of steps
(:func:`_total_steps`) and every milestone appends a timestamped line to a
bounded ``log``. Two primitives write it -- :func:`_phase` starts a step (it
banks the previous one on the bar) and :func:`_note` adds detail to the
current step -- and the long middle of the run reports through them via the
``on_progress`` callback of ``build_grimmory_db.build``. It is in-memory only
and one run is minutes, not hours, so unlike the wishlist scrape there is no
on-disk mirror and no resume: a restart mid-run simply loses the commentary.
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

# The activity window keeps the tail of a run, not all of it: the log is
# rendered in full on every poll, so it has to stay small.
_MAX_LOG_LINES = 300

_STATUS = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_success_at": None,
    "last_result": None,   # {grimmory_books, marked_purchased, deals_refreshed}
    "last_error": None,
    "phase": None,         # label of the step in flight, None when idle
    "step": 0,             # steps COMPLETED (so step/total_steps drives the bar)
    "total_steps": 0,
    "log": [],             # [{"at": "HH:MM:SS", "level": ..., "msg": ...}]
    "run_id": 0,           # bumped per run so a poller can spot a fresh one
}
_LOCK = threading.Lock()


def owned_update_status() -> dict:
    """Snapshot of the last/pending run for the Settings page (never blocks)."""
    with _LOCK:
        status = dict(_STATUS)
        # The worker thread appends to this list as we serialise it, so hand
        # back a copy -- a shared list can mutate mid-JSON-encode.
        status["log"] = list(_STATUS["log"])
    status["elapsed_sec"] = _elapsed_sec(status)
    return status


def _elapsed_sec(status: dict):
    """How long the run has taken, or None when none has run.

    Computed here rather than in the browser because both timestamps are naive
    server-LOCAL time (``datetime.now()``): subtracting them server-side is
    correct whatever timezone the browser is in.
    """
    if not status["started_at"]:
        return None
    try:
        start = datetime.fromisoformat(status["started_at"])
        end = (
            datetime.fromisoformat(status["finished_at"])
            if status["finished_at"] and not status["running"]
            else datetime.now()
        )
    except ValueError:
        return None
    return max(0.0, (end - start).total_seconds())


def _total_steps() -> int:
    """How many steps a run has: sign in, list the libraries, fetch each target
    library, write grimmory.db, match the wishlists, refresh the deal flags.

    The library count is configuration (``GRIMMORY_LIBRARIES``), not something
    discovered mid-run, so the bar is determinate from the first paint.
    """
    return 5 + len(grimmory.target_library_names())


def _append(msg: str, level: str) -> None:
    """Add one activity line. Caller must hold ``_LOCK``."""
    _STATUS["log"].append(
        {
            "at": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        }
    )
    excess = len(_STATUS["log"]) - _MAX_LOG_LINES
    if excess > 0:
        del _STATUS["log"][:excess]


def _phase(label: str) -> None:
    """Start a step: bank the previous one on the bar, then head the log with it.

    Counting on entry rather than on exit means each step needs exactly one
    call; the run's final step is banked by ``_run`` setting ``step`` to
    ``total_steps`` when it completes.
    """
    with _LOCK:
        if _STATUS["phase"] is not None:
            _STATUS["step"] += 1
        _STATUS["phase"] = label
        _append(label, "phase")
    log.info("owned-update: %s", label)


def _note(msg: str, level: str = "info") -> None:
    """Add detail under the current step without moving the bar."""
    with _LOCK:
        _append(msg, level)
    # Everything in the activity window also lands in scrape.log, which is the
    # only record once the process restarts and the in-memory log is gone.
    if level == "error":
        log.warning("owned-update: %s", msg)
    else:
        log.info("owned-update: %s", msg)


def _relay(kind: str, msg: str) -> None:
    """``on_progress`` adapter for ``build_grimmory_db.build``."""
    if kind == "phase":
        _phase(msg)
    else:
        _note(msg)


def _mark_owned_purchased() -> int:
    """Flag every wishlist book owned in Grimmory as purchased; returns count."""
    g = sqlite3.connect(f"file:{Path(config.GRIMMORY_DB).as_posix()}?mode=ro", uri=True)
    try:
        grimm = g.execute("SELECT title, author FROM book").fetchall()
    finally:
        g.close()
    index = deals_db._build_owned_index(grimm)
    _note(f"indexed {len(grimm):,} catalog books for title+author matching")
    d = sqlite3.connect(config.DB_PATH)
    try:
        books = d.execute(
            "SELECT asin, title, author, purchased FROM book"
        ).fetchall()
        already = sum(1 for b in books if b[3])
        to_mark = [
            asin for (asin, title, author, p) in books
            if not p and deals_db._is_owned(index, title, author)
        ]
        _note(
            f"checked {len(books):,} wishlist books "
            f"({already:,} already purchased): {len(to_mark):,} newly owned"
        )
        if to_mark:
            d.executemany("UPDATE book SET purchased = 1 WHERE asin = ?",
                          [(a,) for a in to_mark])
            d.commit()
            _note(f"moved {len(to_mark):,} book(s) to the Purchased tab")
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
        _note(f"re-flagged {n:,} BookBub deal row(s)")
        return n
    finally:
        conn.close()


def update_owned_books_sync() -> dict:
    """The full operation (blocks until done). Raises on failure.

    Narrates itself through :func:`_phase` / :func:`_note` as it goes, so the
    step count here has to stay in step with :func:`_total_steps`: two phases
    of our own plus the three ``build_grimmory_db.build`` reports (list, one
    per library, write).
    """
    _phase("Signing in to Grimmory")
    token = grimmory.login()                       # GRIMMORY_USERNAME/PASSWORD
    _note(f"signed in to {config.GRIMMORY_URL}")
    # rebuild grimmory.db, reporting the library fetches as they happen
    per_library = _build_grimmory_db(
        token, config.GRIMMORY_DB, on_progress=_relay
    )
    _phase("Matching wishlist books against the library")
    marked = _mark_owned_purchased()
    _phase("Refreshing the BookBub deals owned flags")
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
    total = _total_steps()
    with _LOCK:
        if _STATUS["running"]:
            return False
        # A fresh run starts a fresh bar and a fresh activity log; the previous
        # run's outcome (last_success_at / last_result) is kept on show until
        # this one produces its own.
        _STATUS.update(
            running=True,
            started_at=datetime.now().isoformat(timespec="seconds"),
            finished_at=None,
            last_error=None,
            phase=None,
            step=0,
            total_steps=total,
            log=[],
            run_id=_STATUS["run_id"] + 1,
        )

    def _run() -> None:
        try:
            result = update_owned_books_sync()
        except Exception as e:  # surface a clean error to the Settings page
            log.exception("owned-books update failed")
            _note(str(e), "error")
            with _LOCK:
                _STATUS.update(
                    running=False,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                    phase=None,
                    last_error=str(e),
                )
            return
        _note(
            f"done: {result['grimmory_books']:,} catalog books, "
            f"{result['marked_purchased']:,} marked purchased, "
            f"{result['deals_refreshed']:,} deals refreshed",
            "done",
        )
        with _LOCK:
            _STATUS.update(
                running=False,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                phase=None,
                step=_STATUS["total_steps"],   # bank the final step
                last_success_at=datetime.now().isoformat(timespec="seconds"),
                last_result=result,
                last_error=None,
            )

    threading.Thread(target=_run, daemon=True, name="owned-update").start()
    return True
