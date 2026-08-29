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

from .config import (
    DEAL_STATUS_CURRENT,
    DEAL_STATUS_EXPIRED,
    DEAL_STATUS_UNKNOWN,
)

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
    "asin_from_amazon_url",
    "pending_deals",
    "mark_verified",
    "parse_price_cents",
    "classify_deal",
    "current_deals",
    "sort_deals",
    "set_hidden",
    "recheck_deals",
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
    audited_at         TEXT,
    deal_status        TEXT,                                   -- NULL=unchecked, else current|expired|unknown
    current_price      TEXT,                                   -- last read Amazon price text
    verified_at        TEXT,                                   -- ISO time of the last live check
    hidden             INTEGER NOT NULL DEFAULT 0              -- 1 when the user hid it from the BookBub Deals tab
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_deal_date_bub ON deal(date, bookbub_url);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the deals schema if missing (idempotent)."""
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """In-place upgrades for older deals databases (mirrors app/db.py).

    Each step is a no-op if the column already exists, so this is safe to run
    on a fresh DB (where the columns come from SCHEMA_SQL) or an existing one.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(deal)").fetchall()}
    for col in ("deal_status", "current_price", "verified_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE deal ADD COLUMN {col} TEXT")
    if "hidden" not in cols:
        conn.execute("ALTER TABLE deal ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")


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


def asin_from_amazon_url(amazon_url: str | None) -> str | None:
    """The 10-char Amazon ASIN in ``amazon_url`` (``/dp/XXXXXXXXXX``), or None.

    None when the URL is missing or carries no ASIN (an unresolved BookBub
    intermediate link, or a no-Amazon deal).
    """
    if not amazon_url:
        return None
    m = _ASIN_RE.search(amazon_url)
    return m.group(1) if m else None


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


# --------------------------------------------------------------------------- #
# Live-deal verification (price check against current Amazon)
# --------------------------------------------------------------------------- #
_CURRENCY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")


def _num_to_cents(token: str) -> int:
    """'$X' token (comma-grouped, optional 1-2 digit cents) -> integer cents."""
    token = token.replace(",", "")
    if "." in token:
        dollars, frac = token.split(".", 1)
        frac = (frac + "00")[:2]
        return int(dollars or 0) * 100 + int(frac)
    return int(token or 0) * 100


def parse_price_cents(text: str | None) -> int | None:
    """Parse a price string into integer cents, or None when unreadable.

    Handles "``$2.99``", "``$1,299.99``", "``Free``" / "``Free with Kindle
    Unlimited``" (→ 0), a bare "``0``" (→ 0) and price ranges (the first bound
    is used).
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    if "free" in s.lower():
        return 0
    if re.fullmatch(r"0(?:\.0{1,2})?", s):
        return 0
    m = _CURRENCY_RE.search(s)
    if m:
        return _num_to_cents(m.group(1))
    return None


def pending_deals(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Unverified deals that have an Amazon ASIN, in row order.

    Returns ``{id, asin, amazon_url, deal_price, title}`` for rows where
    ``deal_status IS NULL`` and whose ``amazon_url`` contains an ASIN.
    ``limit`` caps the number of dicts returned (after the ASIN filter).
    """
    rows = conn.execute(
        "SELECT id, amazon_url, deal_price, title FROM deal "
        "WHERE deal_status IS NULL AND amazon_url IS NOT NULL ORDER BY id"
    ).fetchall()
    out: list[dict] = []
    for row_id, url, deal_price, title in rows:
        asin = asin_from_amazon_url(url)
        if asin:
            out.append(
                {
                    "id": row_id,
                    "asin": asin,
                    "amazon_url": url,
                    "deal_price": deal_price,
                    "title": title,
                }
            )
            if limit is not None and len(out) >= limit:
                break
    return out


def mark_verified(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    status: str,
    current_price: str | None,
    at: str,
) -> None:
    """Record a live-check result for a deal row (the caller commits)."""
    conn.execute(
        "UPDATE deal SET deal_status = ?, current_price = ?, verified_at = ? WHERE id = ?",
        (status, current_price, at, row_id),
    )


def recheck_deals(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Deals the daily updater must (re-)verify: everything EXCEPT expired.

    Like :func:`pending_deals` but selects rows whose ``deal_status`` is NULL
    (never checked) or ``current`` / ``unknown`` (still believed live, or
    unreadable last time — worth another look). An ``expired`` deal is
    terminal and is NEVER re-checked (requirement: "Expired deals are never
    checked again"). Same ``{id, asin, amazon_url, deal_price, title}`` shape
    as ``pending_deals``, ASIN-filtered the same way, ``limit`` applied after
    the ASIN filter.
    """
    rows = conn.execute(
        "SELECT id, amazon_url, deal_price, title FROM deal "
        "WHERE (deal_status IS NULL OR deal_status IN (?, ?)) "
        "AND amazon_url IS NOT NULL ORDER BY id",
        (DEAL_STATUS_CURRENT, DEAL_STATUS_UNKNOWN),
    ).fetchall()
    out: list[dict] = []
    for row_id, url, deal_price, title in rows:
        asin = asin_from_amazon_url(url)
        if asin:
            out.append(
                {
                    "id": row_id,
                    "asin": asin,
                    "amazon_url": url,
                    "deal_price": deal_price,
                    "title": title,
                }
            )
            if limit is not None and len(out) >= limit:
                break
    return out


def _format_deal_date(date: str | None) -> str:
    """``YYYYMMDD`` -> ``YYYY-MM-DD`` for display; anything else passes through."""
    if date and len(date) == 8 and date.isdigit():
        return f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    return date or ""


def current_deals(conn: sqlite3.Connection, show_hidden: bool = False) -> list[dict]:
    """Live BookBub deals for the web app's BookBub Deals tab.

    Returns ``{id, title, author, date, deal_price, deal_price_cents,
    original_price, amazon_url, hidden}`` dicts for every row the app
    presents as an in-flight deal:
    ``deal_status`` is ``current`` (verified live on Amazon), ``amazon_url``
    is present (the tab links the title to Amazon), and the book is **not**
    owned in Grimmory (``owned_in_grimmory`` is 0 or NULL/unknown — avoid
    showing books the user already owns). Newest first
    (``date DESC, id DESC``); ``date`` is reformatted from ``YYYYMMDD`` to
    ``YYYY-MM-DD`` for display. This excludes expired deals, ``unknown``
    deals (unreadable — treated as unverified), unchecked deals
    (``deal_status`` NULL), and books already owned. ``deal_price_cents`` is
    the numeric value of ``deal_price`` in cents (``None`` when unparseable,
    ``Free!`` → 0); it exists so price sorting is numeric, not textual.
    Rows the user hid (``hidden`` = 1) are excluded unless ``show_hidden``
    is true; the ``hidden`` flag is also returned in each dict so the UI can
    render the per-row hide checkbox.
    """
    rows = conn.execute(
        "SELECT id, date, title, author, deal_price, original_price, amazon_url, hidden "
        "FROM deal WHERE deal_status = ? AND amazon_url IS NOT NULL "
        "AND owned_in_grimmory IS NOT 1 "
        "AND (hidden = 0 OR ? = 1) "
        "ORDER BY date DESC, id DESC",
        (DEAL_STATUS_CURRENT, int(show_hidden)),
    ).fetchall()
    out: list[dict] = []
    for row_id, date, title, author, deal_price, original_price, amazon_url, hidden in rows:
        out.append(
            {
                "id": row_id,
                "title": title,
                "author": author,
                "date": _format_deal_date(date),
                "deal_price": deal_price,
                "deal_price_cents": parse_price_cents(deal_price),
                "original_price": original_price,
                "amazon_url": amazon_url,
                "hidden": hidden,
            }
        )
    return out


def set_hidden(conn: sqlite3.Connection, row_id: int, hidden: bool) -> bool:
    """Set or clear a deal row's hidden flag (the caller commits).

    Returns True when the row existed and was updated, False when the id is
    unknown (so the caller can answer 404).
    """
    cur = conn.execute(
        "UPDATE deal SET hidden = ? WHERE id = ?",
        (1 if hidden else 0, row_id),
    )
    return cur.rowcount > 0


def sort_deals(rows: list[dict], sort: str = "date", direction: str = "desc") -> list[dict]:
    """Return a NEW list of ``current_deals`` dicts ordered for the web tab.

    ``sort`` is ``"date"`` (the ``YYYY-MM-DD`` ``date`` string, lexicographic =
    chronological) or ``"price"`` (the numeric ``deal_price_cents``). ``direction``
    is ``"asc"`` or ``"desc"``. For ``price`` sorting, rows whose
    ``deal_price_cents`` is None (an unparseable deal price) are always placed
    last, regardless of direction. A new list is returned; the caller's list
    and its dicts are never mutated. Unknown ``sort`` values fall back to
    ``date``.
    """
    ordered = list(rows)  # shallow copy — reorder, never touch the input order
    if sort == "price":
        with_cents = [r for r in ordered if r.get("deal_price_cents") is not None]
        no_cents = [r for r in ordered if r.get("deal_price_cents") is None]
        with_cents.sort(key=lambda r: r["deal_price_cents"], reverse=(direction == "desc"))
        return with_cents + no_cents
    # sort == "date" (default fallback)
    ordered.sort(key=lambda r: (r.get("date") or ""), reverse=(direction == "desc"))
    return ordered


def classify_deal(deal_price: str | None, current_price: str | None) -> tuple[str, int | None]:
    """Classify a deal by comparing the stored deal price to the current price.

    Returns ``(status, current_cents)`` where status is one of
    ``DEAL_STATUS_CURRENT`` / ``DEAL_STATUS_EXPIRED`` / ``DEAL_STATUS_UNKNOWN``:

    * current price unparseable → unknown (never guessed)
    * current price free/0 → current
    * deal price unparseable → unknown
    * current price > deal price → expired
    * otherwise (at or below the deal price) → current
    """
    cur = parse_price_cents(current_price)
    if cur is None:
        return (DEAL_STATUS_UNKNOWN, None)
    if cur == 0:
        return (DEAL_STATUS_CURRENT, 0)
    deal = parse_price_cents(deal_price)
    if deal is None:
        return (DEAL_STATUS_UNKNOWN, cur)
    if cur > deal:
        return (DEAL_STATUS_EXPIRED, cur)
    return (DEAL_STATUS_CURRENT, cur)
