"""Deals database: store, audit, and refresh BookBub daily-deal records.

This is the backing store for the BookBub deals workflow (see
``scripts/build_bookbub_deals.py``). It is a standalone SQLite file
(``data/deals.db`` by default, overridable via ``DEALS_DB``), deliberately
separate from ``wishlist.db`` and ``grimmory.db``.

Design notes
------------
* Every BookBub deal for a day is stored (audit retention) — including deals
  with no Amazon Kindle link and books not owned in the Grimmory library.
* ``amazon_url`` is NULL when the book has no Amazon edition;
  ``no_amazon_link`` mirrors that (``1`` when ``amazon_url`` IS NULL).
* ``owned_in_grimmory`` is an *approximate* normalised title+author match
  against ``grimmory.db`` (``1`` owned / ``0`` not owned / ``NULL`` when
  grimmory.db is unavailable). It is kept so a human can audit match accuracy.
* Rows are keyed by ``(date, bookbub_url)``. Re-running the same date upserts
  (refreshes) that day's rows and never grows duplicates; rows for other dates
  are never deleted.
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from pathlib import Path

__all__ = [
    "SCHEMA_SQL",
    "ensure_schema",
    "connect",
    "normalise",
    "owned_lookup",
    "upsert_deals",
    "store_deals",
    "book_identity",
    "deduplicate",
]

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS deal (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    date               TEXT NOT NULL,
    title              TEXT NOT NULL,
    author             TEXT,
    deal_price         TEXT,
    original_price     TEXT,
    bookbub_url        TEXT,
    amazon_url         TEXT,                                  -- NULL when no Amazon edition
    no_amazon_link     INTEGER NOT NULL DEFAULT 0,             -- 1 when amazon_url IS NULL
    owned_in_grimmory  INTEGER,                                -- 1 owned / 0 not / NULL = grimmory unavailable
    audited_at         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_deal_date_bub ON deal(date, bookbub_url);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the deals schema if missing (idempotent)."""
    conn.executescript(SCHEMA_SQL)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the deals database (WAL, FK on). The caller commits and closes."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalise(text: str | None) -> str:
    """Lowercase, collapse whitespace, and strip common punctuation.

    Applied identically to the deal side and the grimmory side so the two
    formats of the same title/author converge (e.g. ``"Don't"`` -> ``"don t"``,
    ``"e-book"`` -> ``"e book"``). Approximate on purpose: the resulting match
    is stored in the DB so a human can audit its accuracy.
    """
    if not text:
        return ""
    t = text.lower()
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t)
    return t.strip()


def owned_lookup(deals, grimmory_path: str | Path) -> dict:
    """Map each deal's ``bookbub_url`` -> owned (``1``/``0``), or ``None``.

    Reads ``grimmory.db``'s ``book(title, author)`` rows (read-only) and marks
    a deal owned (``1``) when *any* grimmory book matches on **normalised
    title AND normalised author**; otherwise ``0``. If the grimmory DB file is
    missing, every deal maps to ``None`` (stored as NULL — the audit column is
    left blank rather than raising a hard failure).
    """
    grimmory_path = Path(grimmory_path)
    if not grimmory_path.exists():
        return {d.url: None for d in deals}

    conn = sqlite3.connect(f"file:{grimmory_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT title, author FROM book").fetchall()
    finally:
        conn.close()

    owned_pairs = {(normalise(t), normalise(a)) for (t, a) in rows}
    result = {}
    for d in deals:
        result[d.url] = 1 if (normalise(d.title), normalise(d.author)) in owned_pairs else 0
    return result


# --------------------------------------------------------------------------- #
# Upsert
# --------------------------------------------------------------------------- #
_UPSERT_SQL = """
INSERT INTO deal (
    date, title, author, deal_price, original_price,
    bookbub_url, amazon_url, no_amazon_link,
    owned_in_grimmory, audited_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(date, bookbub_url) DO UPDATE SET
    title              = excluded.title,
    author             = excluded.author,
    deal_price         = excluded.deal_price,
    original_price     = excluded.original_price,
    amazon_url         = excluded.amazon_url,
    no_amazon_link     = excluded.no_amazon_link,
    owned_in_grimmory  = excluded.owned_in_grimmory,
    audited_at         = excluded.audited_at
"""


def upsert_deals(conn: sqlite3.Connection, deals, date: str, owned_map: dict, audited_at: str) -> int:
    """Insert or refresh each deal for ``date`` (idempotent on ``(date, bookbub_url)``).

    Re-running the same date updates that day's rows and never grows
    duplicates; rows for other dates are untouched (audit retention).
    ``owned_map`` maps ``bookbub_url`` -> ``1``/``0``/``None`` (see
    :func:`owned_lookup`); ``None`` is stored as NULL. Returns the number of
    deals written.
    """
    n = 0
    for d in deals:
        amazon_url = d.amazon_url or None
        no_amazon_link = 0 if amazon_url else 1
        owned = owned_map.get(d.url)  # 1 / 0 / None
        conn.execute(
            _UPSERT_SQL,
            (
                date,
                d.title,
                d.author,
                d.price,
                d.original_price,
                d.url,
                amazon_url,
                no_amazon_link,
                owned,
                audited_at,
            ),
        )
        n += 1
    return n


def store_deals(deals, date: str, *, deals_path: str | Path, grimmory_path: str | Path,
                audited_at: str | None = None) -> tuple[int, int, int]:
    """Store the deals for ``date`` in ``deals_path`` (idempotent upsert).

    Opens the deals database, ensures the schema, computes the
    owned-in-grimmory audit (``None``/NULL when ``grimmory_path`` is absent),
    and upserts every deal for ``date``. ``audited_at`` defaults to the current
    local time (ISO, second precision). Returns ``(stored, owned, no_amazon)``.
    Raises on a database error.
    """
    owned_map = owned_lookup(deals, grimmory_path)
    if audited_at is None:
        audited_at = _dt.datetime.now().isoformat(timespec="seconds")
    conn = connect(deals_path)
    try:
        ensure_schema(conn)
        stored = upsert_deals(conn, deals, date, owned_map, audited_at)
        conn.commit()
    finally:
        conn.close()
    owned = sum(1 for v in owned_map.values() if v == 1)
    no_amazon = sum(1 for d in deals if not d.amazon_url)
    return stored, owned, no_amazon


# --------------------------------------------------------------------------- #
# Deduplication (a book re-featured on multiple dates)
# --------------------------------------------------------------------------- #
_ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")


def book_identity(title: str | None, author: str | None, amazon_url: str | None) -> tuple:
    """Identity for "the same book", used for deduplication and auditing.

    Prefers the canonical Amazon ASIN (``/dp/XXXXXXXXXX``) from
    ``amazon_url`` — a book re-featured on different dates shares its ASIN
    (the ``?_bbid=…&tag=…`` tracking suffix is ignored). Falls back to the
    normalised ``(title, author)`` pair when there is no Amazon link.
    """
    if amazon_url:
        m = _ASIN_RE.search(amazon_url)
        if m:
            return ("asin", m.group(1))
    return ("meta", normalise(title), normalise(author))


def deduplicate(conn: sqlite3.Connection) -> int:
    """Remove repeated books, keeping the most recent deal for each.

    Groups rows by :func:`book_identity`; in every group of duplicates the
    row with the largest ``date`` (YYYYMMDD, lexicographic = chronological)
    is kept — a same-date tie keeps the highest ``id`` (most recent insert).
    Only DELETEs (never updates or merges, so the kept row retains all its
    own columns and date); idempotent — a second call removes 0. Returns the
    number of rows removed; the caller commits.
    """
    rows = conn.execute("SELECT id, date, title, author, amazon_url FROM deal").fetchall()
    latest: dict = {}  # identity -> (date, id) of the keeper
    for row_id, date, title, author, amazon_url in rows:
        ident = book_identity(title, author, amazon_url)
        cand = (date or "", row_id)
        if ident not in latest or cand > latest[ident]:
            latest[ident] = cand
    keep = {row_id for _ident, (_date, row_id) in latest.items()}
    removed = {row_id for (row_id, _d, _t, _a, _u) in rows} - keep
    for row_id in removed:
        conn.execute("DELETE FROM deal WHERE id = ?", (row_id,))
    return len(removed)
