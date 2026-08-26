"""Mirror mode: export the DB on the primary, apply it on the secondary.

Both ends of the wire live in this one file so the payload formats cannot drift
apart. The primary serves `export_*` over `/api/sync/*`; the secondary calls
`apply_*` with what it fetched (see `sync_client.py`).

The whole design rests on one property of this schema: `price_snapshot` is
append-only with an AUTOINCREMENT id, and SQLite is single-writer. Nothing in
the app ever updates or deletes a `price_snapshot` or `book` row, so a reader
that observes MAX(id) = M has necessarily observed every row <= M -- there is
no allocated-but-uncommitted gap, which is the classic hazard with autoincrement
watermarks on other engines. That makes

    WHERE id > since AND id <= M ORDER BY id

a stable, contiguous, gap-free window no matter what the primary does
concurrently, which in turn means the secondary needs no cursor table: its own
MAX(price_snapshot.id) IS the cursor. A retention/purge job on `price_snapshot`,
or a second process writing to the same DB, would break this silently.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime

from .config import (
    INGEST_SHRINK_FLOOR,
    SYNC_PAGE_LIMIT,
    SYNC_PAGE_LIMIT_MAX,
    SYNC_STATE_PATH,
)
from .db import connect

log = logging.getLogger(__name__)

# Bumped only on an incompatible payload change. The applier refuses anything else.
SYNC_FORMAT = 1

# Snapshot rows travel as positional arrays, not objects: they are the entire
# bulk of the transfer (~85 bytes/row here vs ~170 as dicts), and one shared
# tuple is what keeps exporter and applier from disagreeing about the order.
SNAPSHOT_COLUMNS = (
    "id",
    "asin",
    "observed_at",
    "current_price_cents",
    "list_price_cents",
    "availability",
)

WISHLIST_COLUMNS = (
    "id",
    "url",
    "label",
    "added_at",
    "last_scraped_at",
    "previous_item_count",
    "pending_shrink_count",
)

BOOK_COLUMNS = (
    "asin",
    "title",
    "author",
    "product_url",
    "first_seen",
    "last_seen",
    "purchased",
)


class SyncRefused(Exception):
    """A catalog was fetched successfully but was not applied.

    The secondary replaces its wishlist membership wholesale, exactly as
    `ingest_wishlist` does, so the same rule applies: a short or empty payload
    must never be allowed to clobber a good mirror. A primary that is
    half-migrated, pointed at the wrong host, or serving through a bug would
    otherwise wipe every page on the secondary in one transaction.

    Refusing once and accepting a shrink that a SECOND consecutive catalog
    agrees with keeps this a guard rather than a trap -- a genuinely pruned set
    of wishlists mirrors through on the next sync instead of stranding.
    """


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


# ---------- export side (runs on the primary) ----------


def export_catalog() -> dict:
    """The whole small half of the DB, plus a coherent snapshot watermark.

    `db.connect()` sets isolation_level=None, so every SELECT is otherwise its
    own snapshot: without the explicit BEGIN, `wishlist_book` could be read
    after an ingest that references an ASIN we already read past in `book`, and
    the secondary would take a FOREIGN KEY error on apply.

    `max_snapshot_id` MUST be read inside that same transaction. Sampled any
    earlier, the catalog could carry a book whose only snapshots are > M, and
    `services._LATEST_BASE` INNER-JOINs the latest snapshot -- so that book
    would silently vanish from every page until the next sync.
    """
    with connect() as conn:
        conn.execute("BEGIN")
        try:
            wishlists = [
                dict(r)
                for r in conn.execute(
                    f"SELECT {', '.join(WISHLIST_COLUMNS)} FROM wishlist ORDER BY id"
                ).fetchall()
            ]
            books = [
                dict(r)
                for r in conn.execute(
                    f"SELECT {', '.join(BOOK_COLUMNS)} FROM book ORDER BY asin"
                ).fetchall()
            ]
            wishlist_books = [
                [r["wishlist_id"], r["asin"]]
                for r in conn.execute(
                    "SELECT wishlist_id, asin FROM wishlist_book "
                    "ORDER BY wishlist_id, asin"
                ).fetchall()
            ]
            max_id = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM price_snapshot"
            ).fetchone()["m"]
        finally:
            conn.execute("COMMIT")

    return {
        "format": SYNC_FORMAT,
        # The primary's own wall clock. `services._now()` is naive server-local
        # time, so the secondary cannot compare mirrored timestamps against its
        # own clock without this. See services.list_wishlists(now=...).
        "source_now": _now(),
        "max_snapshot_id": max_id,
        "wishlists": wishlists,
        "books": books,
        "wishlist_books": wishlist_books,
    }


def clamp_limit(limit: int | None) -> int:
    if not limit or limit < 1:
        return SYNC_PAGE_LIMIT
    return min(int(limit), SYNC_PAGE_LIMIT_MAX)


def export_snapshots(since_id: int, max_id: int, limit: int | None = None) -> dict:
    """One ascending page of the append-only snapshot log, capped at `max_id`."""
    lim = clamp_limit(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {', '.join(SNAPSHOT_COLUMNS)}
            FROM price_snapshot
            WHERE id > ? AND id <= ?
            ORDER BY id
            LIMIT ?
            """,
            (since_id, max_id, lim),
        ).fetchall()

    out = [[r[c] for c in SNAPSHOT_COLUMNS] for r in rows]
    next_since = out[-1][0] if out else since_id
    return {
        "format": SYNC_FORMAT,
        "since_id": since_id,
        "max_id": max_id,
        "count": len(out),
        "rows": out,
        # `has_more` is derived from the cap, not from len(rows) == limit, so a
        # page that happens to land exactly on the boundary still terminates.
        "has_more": bool(out) and next_since < max_id,
        "next_since_id": next_since,
    }


# ---------- apply side (runs on the secondary) ----------


def local_watermark() -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM price_snapshot"
        ).fetchone()["m"]


def _check_format(payload: dict) -> None:
    fmt = payload.get("format")
    if fmt != SYNC_FORMAT:
        raise SyncRefused(
            f"payload format {fmt!r} != expected {SYNC_FORMAT} — upgrade both instances"
        )


def apply_catalog(payload: dict) -> dict:
    """Replace wishlists + membership wholesale and upsert every book.

    Everything happens in ONE transaction. A half-applied catalog leaves zero
    `wishlist_book` rows, and every read query INNER-JOINs that table, so the
    whole UI would render blank; inside a transaction, WAL readers keep seeing
    the pre-transaction state until the commit lands.
    """
    _check_format(payload)

    wishlists = payload.get("wishlists") or []
    books = payload.get("books") or []
    wishlist_books = payload.get("wishlist_books") or []
    incoming_members = len(wishlist_books)
    source_max = int(payload.get("max_snapshot_id") or 0)

    state = get_sync_state()

    with connect() as conn:
        stored_members = conn.execute(
            "SELECT COUNT(*) AS c FROM wishlist_book"
        ).fetchone()["c"]
        local_max = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM price_snapshot"
        ).fetchone()["m"]

        # Tripwire: the source went backwards. Its DB was rebuilt, restored from
        # an older backup, or WISHLIST_PRIMARY_URL now names a different host.
        # Left alone, `since_id=<our huge max>` returns nothing forever and the
        # mirror freezes on stale data behind a green "sync OK".
        if source_max < local_max:
            raise SyncRefused(
                f"source max_snapshot_id {source_max} is behind ours ({local_max}) — "
                "the primary's database was rebuilt or the peer changed. "
                "Delete this instance's data/wishlist.db and restart for a full resync."
            )

        pending = state.get("pending_shrink_members")
        short = stored_members > 0 and incoming_members < stored_members * INGEST_SHRINK_FLOOR
        agrees = pending is not None and min(incoming_members, pending) >= (
            max(incoming_members, pending) * INGEST_SHRINK_FLOOR
        )
        if short and not agrees:
            update_sync_state(
                pending_shrink_members=incoming_members,
                last_error=(
                    f"catalog refused: {incoming_members} memberships vs {stored_members} stored"
                ),
                last_attempt_at=_now(),
            )
            raise SyncRefused(
                f"catalog carried {incoming_members} wishlist memberships against "
                f"{stored_members} stored (floor {INGEST_SHRINK_FLOOR}); refused once. "
                "A second consecutive catalog that agrees will be accepted."
            )

        conn.execute("BEGIN")
        try:
            # Cascades wishlist_book. Safe with AUTOINCREMENT: sqlite_sequence is
            # not reset by a DELETE, and re-inserting the primary's explicit ids
            # cannot collide on wishlist.url because the table is empty here.
            conn.execute("DELETE FROM wishlist")
            conn.executemany(
                f"INSERT INTO wishlist ({', '.join(WISHLIST_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(WISHLIST_COLUMNS))})",
                [tuple(w.get(c) for c in WISHLIST_COLUMNS) for w in wishlists],
            )
            # Deliberately NOT COALESCE(excluded.author, book.author) the way
            # ingest_wishlist does: there the scrape may have simply lost the
            # byline, here the primary is authoritative including a real NULL.
            conn.executemany(
                f"""
                INSERT INTO book ({', '.join(BOOK_COLUMNS)})
                VALUES ({', '.join('?' * len(BOOK_COLUMNS))})
                ON CONFLICT(asin) DO UPDATE SET
                    title       = excluded.title,
                    author      = excluded.author,
                    product_url = excluded.product_url,
                    first_seen  = excluded.first_seen,
                    last_seen   = excluded.last_seen,
                    purchased   = excluded.purchased
                """,
                [tuple(b.get(c) for c in BOOK_COLUMNS) for b in books],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO wishlist_book (wishlist_id, asin) VALUES (?, ?)",
                [(wid, asin) for wid, asin in wishlist_books],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    update_sync_state(
        pending_shrink_members=None,
        source_now=payload.get("source_now"),
        source_max_snapshot_id=source_max,
        catalog_applied_at=_now(),
        wishlists=len(wishlists),
        books=len(books),
        memberships=incoming_members,
    )
    return {
        "wishlists": len(wishlists),
        "books": len(books),
        "memberships": incoming_members,
        "max_snapshot_id": source_max,
    }


def apply_snapshots(payload: dict) -> int:
    """Insert one ascending page of mirrored snapshots. Returns rows added.

    Explicit primary ids, `INSERT OR IGNORE` so a replayed page is a no-op.
    Note that OR IGNORE swallows the duplicate-id case but still RAISES on a
    foreign-key violation -- that is deliberate: applying snapshots before the
    catalog fails loudly instead of silently dropping them.
    """
    _check_format(payload)
    rows = payload.get("rows") or []
    if not rows:
        return 0

    placeholders = ", ".join("?" * len(SNAPSHOT_COLUMNS))
    added = 0
    with connect() as conn:
        conn.execute("BEGIN")
        try:
            for row in rows:
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO price_snapshot "
                    f"({', '.join(SNAPSHOT_COLUMNS)}) VALUES ({placeholders})",
                    tuple(row),
                )
                if cur.rowcount:
                    added += 1
                else:
                    _warn_id_collision(conn, row)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return added


def _warn_id_collision(conn: sqlite3.Connection, row: list) -> None:
    """An ignored insert whose stored row differs is data loss, not a replay.

    Only reachable if something inserted a LOCAL snapshot on this mirror: an
    explicit-id insert pushes sqlite_sequence to the mirrored max, so a local
    row would take max+1 -- exactly the id the primary will next hand to a
    DIFFERENT row, which OR IGNORE then discards while the watermark sails past
    it. `services.ingest_wishlist` refuses to run on a secondary precisely to
    make this unreachable; this logs it if that guard is ever bypassed.
    """
    idx = {c: i for i, c in enumerate(SNAPSHOT_COLUMNS)}
    stored = conn.execute(
        "SELECT asin, observed_at FROM price_snapshot WHERE id = ?",
        (row[idx["id"]],),
    ).fetchone()
    if stored and (
        stored["asin"] != row[idx["asin"]]
        or stored["observed_at"] != row[idx["observed_at"]]
    ):
        log.error(
            "SYNC ID COLLISION: snapshot id %s holds %s@%s locally but the primary "
            "sent %s@%s. This mirror has local snapshots and is losing rows — "
            "delete data/wishlist.db and resync.",
            row[idx["id"]],
            stored["asin"],
            stored["observed_at"],
            row[idx["asin"]],
            row[idx["observed_at"]],
        )


# ---------- advisory sync state (telemetry only; the cursor is the DB) ----------

# Deliberately NOT the sync cursor. A file cursor and the data it describes are
# two separate writes: written after the commit it replays a page (harmless),
# written before it skips one permanently (silent, unrecoverable). MAX(id)
# cannot diverge from the rows it summarises, so that is the cursor and this is
# just what the UI shows.
_state_lock = threading.Lock()
_state: dict = {
    "running": False,
    "last_attempt_at": None,
    "last_success_at": None,
    "last_error": None,
    "catalog_applied_at": None,
    "source_now": None,
    "source_max_snapshot_id": None,
    "watermark": 0,
    "snapshots_pulled": 0,
    "wishlists": 0,
    "books": 0,
    "memberships": 0,
    "pending_shrink_members": None,
    "synced_at_local": None,
}
_state_loaded = False


def _persist_state_locked() -> None:
    try:
        tmp = SYNC_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_state), encoding="utf-8")
        os.replace(tmp, SYNC_STATE_PATH)
    except OSError:
        log.exception("Failed to persist sync state to %s", SYNC_STATE_PATH)


def _load_state_locked() -> None:
    global _state_loaded
    _state_loaded = True
    try:
        raw = json.loads(SYNC_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(raw, dict):
        for k in _state:
            if k in raw:
                _state[k] = raw[k]
    # A process that died mid-sync leaves this true on disk.
    _state["running"] = False


def get_sync_state() -> dict:
    with _state_lock:
        if not _state_loaded:
            _load_state_locked()
        return dict(_state)


def update_sync_state(**kwargs) -> None:
    with _state_lock:
        if not _state_loaded:
            _load_state_locked()
        _state.update(kwargs)
        _persist_state_locked()


def get_sync_status() -> dict:
    """What `GET /api/sync/status` and the wishlists page both render.

    Role and peer belong here rather than in the route, so the template and the
    JSON endpoint cannot disagree about them.
    """
    from . import config

    state = get_sync_state()
    state["role"] = config.ROLE
    state["primary_url"] = config.PRIMARY_URL
    state["watermark"] = local_watermark()
    age = None
    if state.get("last_success_at"):
        try:
            age = (
                datetime.now() - datetime.fromisoformat(state["last_success_at"])
            ).total_seconds()
        except (TypeError, ValueError):
            age = None
    state["age_sec"] = age
    return state
