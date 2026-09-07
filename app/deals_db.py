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
import unicodedata
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
    "refresh_owned",
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
    "identity_keys",
    "hidden_keys",
    "is_hidden",
    "hidden_book_rows",
    "replace_hidden_books",
    "set_hidden",
    "recheck_deals",
    "update_cover_desc",
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
    hidden             INTEGER NOT NULL DEFAULT 0,             -- LEGACY, never read or written any more: hides live in hidden_book
    cover              TEXT,                                   -- book cover filename in data/covers/ (NULL when never captured)
    description        TEXT,                                   -- Amazon book description captured during verification (NULL when never captured)
    stars              REAL,                                   -- Amazon star rating (0-5, e.g. 4.5) captured during verification (NULL when never captured)
    ratings            INTEGER                                 -- Amazon rating count captured during verification (NULL when never captured)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_deal_date_bub ON deal(date, bookbub_url);

-- A book the user dismissed from the BookBub Deals tab, keyed by BOOK
-- IDENTITY rather than by deal row. BookBub re-features the same book on
-- later dates; every date is its own `deal` row (uq_deal_date_bub) and
-- `deduplicate()` then deletes the older ones -- so the per-row `deal.hidden`
-- flag this table replaces was erased every time a book came round again, and
-- the deal reappeared on the tab. One row per key from `identity_keys()` (the
-- ASIN key AND the normalised title+author key), so a hide also survives an
-- amazon_url that gains or loses its /dp/ ASIN between dates. `title`/`author`
-- are stored for human audit only; nothing matches on them.
CREATE TABLE IF NOT EXISTS hidden_book (
    book_key   TEXT PRIMARY KEY,
    title      TEXT,
    author     TEXT,
    hidden_at  TEXT NOT NULL
);
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True when ``name`` is an existing table in this database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the deals schema if missing (idempotent).

    The ``hidden_book`` existence check must happen BEFORE the schema script
    runs (which creates the table): it is what makes the legacy-flag backfill
    in :func:`_migrate` a one-shot, so a book the user un-hides afterwards is
    not silently re-hidden by the next call.
    """
    fresh_hidden_book = not _table_exists(conn, "hidden_book")
    conn.executescript(SCHEMA_SQL)
    _migrate(conn, backfill_hidden=fresh_hidden_book)


def _migrate(conn: sqlite3.Connection, backfill_hidden: bool = False) -> None:
    """In-place upgrades for older deals databases (mirrors app/db.py).

    Each step is a no-op if the column already exists, so this is safe to run
    on a fresh DB (where the columns come from SCHEMA_SQL) or an existing one.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(deal)").fetchall()}
    for col in ("deal_status", "current_price", "verified_at", "cover", "description"):
        if col not in cols:
            conn.execute(f"ALTER TABLE deal ADD COLUMN {col} TEXT")
    if "stars" not in cols:
        conn.execute("ALTER TABLE deal ADD COLUMN stars REAL")
    if "ratings" not in cols:
        conn.execute("ALTER TABLE deal ADD COLUMN ratings INTEGER")
    if "hidden" not in cols:
        conn.execute("ALTER TABLE deal ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
    if backfill_hidden:
        _backfill_hidden_books(conn)


def _backfill_hidden_books(conn: sqlite3.Connection) -> int:
    """One-shot: carry the legacy per-row ``deal.hidden`` flags into
    ``hidden_book``. Returns the number of keys written.

    Called only from the :func:`ensure_schema` pass that CREATES
    ``hidden_book``, so it can never re-hide a book the user un-hid later.
    Commits itself: most callers of ``ensure_schema`` are read paths that
    close the connection without committing, which would roll the backfill
    back while leaving the (already committed) empty table in place -- i.e.
    every existing hide lost on the first page load after the upgrade.
    """
    rows = conn.execute(
        "SELECT title, author, amazon_url FROM deal WHERE hidden = 1"
    ).fetchall()
    at = _dt.datetime.now().isoformat(timespec="seconds")
    n = 0
    for title, author, amazon_url in rows:
        n += _hide_keys(conn, title, author, amazon_url, at)
    conn.commit()
    return n


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
# Parenthetical groups removed before the ownership title comparison — Amazon/
# Grimmory titles frequently append the series name or subtitle as a trailing
# parenthetical ("Stone's Throw (A Jesse Stone Novel)") that the BookBub deal
# title omits. Stripping just the parenthetical is a SAFE relaxation: unlike a
# word-prefix match it does not collapse "The Hunter" against "The Hunter's
# Wife" (a real false positive) — the two titles only converge once the
# additive parenthetical is gone.
_PARENS_RE = re.compile(r"\s*\([^)]*\)\s*")


def _owned_title_key(text: str | None) -> str:
    """Normalised title for the owned-in-grimmory match (parentheticals
    stripped). Plain :func:`normalise` keeps them, so ``"X (Subtitle)"`` would
    never equal ``"X"`` and owned books with an appended series/subtitle
    slipped through the exact match.
    """
    if not text:
        return ""
    return normalise(_PARENS_RE.sub(" ", text))


def normalise(text: str | None) -> str:
    """Lowercase, strip diacritics, collapse whitespace, strip punctuation.

    Applied identically to the deal side and the grimmory side so the two
    formats of the same title/author converge (e.g. ``"Don't"`` -> ``"don t"``,
    ``"e-book"`` -> ``"e book"``, ``"Inés"`` -> ``"ines"``). Diacritics are
    removed via NFKD + dropping combining marks, so an accented "Inés" matches
    an unaccented "Ines". Approximate on purpose: the resulting match is stored
    in the DB so a human can audit its accuracy.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    # Drop combining marks (the diacritics left over after NFKD decomposition),
    # keeping the base letters — so accented vs plain forms converge.
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t)
    return t.strip()


def _author_keys(author: str | None) -> list[str]:
    """Author keys for the ownership match: the name as written, plus the
    ``"Surname, Given"`` -> ``"Given Surname"`` flip.

    The two sides spell the same author differently. Grimmory carries a large
    minority of its authors surname-first ("Matheson, Richard"), inherited from
    the Calibre libraries behind it; BookBub always writes them out ("Richard
    Matheson"). The match requires the author to be EQUAL -- that strictness is
    what stops unrelated books with the same title being called owned -- so
    every one of those entries silently failed to match, and the owned book
    stayed on the deals tab. Measured against the live data on 2026-09-06:
    1,615 of 37,771 catalog rows are stored surname-first, and 14 of 688 deal
    rows were owned but missed for this reason alone.

    Only a SINGLE comma is treated as an inversion, and both halves must be
    non-empty. A BookBub credit can also use a comma to separate co-authors
    ("Jennifer Reingold, Daniel Reingold"); flipping that yields a key that
    simply matches nothing, which is why the flip is additive (an extra key)
    and never replaces the name as written.
    """
    keys = [_owned_title_key(author)]
    if author and author.count(",") == 1:
        last, first = author.split(",", 1)
        if last.strip() and first.strip():
            flipped = _owned_title_key(f"{first} {last}")
            if flipped and flipped != keys[0]:
                keys.append(flipped)
    return [k for k in keys if k]


def _build_owned_index(grimmory_rows) -> dict:
    """Index grimmory rows for owned-lookup: ``author_key -> [title_key, ...]``.

    Titles/authors are run through :func:`_owned_title_key` (parenthetical
    stripped, diacritics removed, normalised). Grouping by author keeps each
    deal's lookup small (only same-author Grimmory titles are compared). Each
    title is filed under every key :func:`_author_keys` gives for its author,
    so a surname-first catalog entry is reachable from the way BookBub writes
    the same name.
    """
    idx: dict = {}
    for title, author in grimmory_rows:
        title_key = _owned_title_key(title)
        for author_key in _author_keys(author):
            idx.setdefault(author_key, []).append(title_key)
    return idx


def _is_owned(index: dict, title, author) -> bool:
    """True when (title, author) matches a grimmory entry in ``index``.

    Requires the normalised (paren/diacritic-stripped, :func:`_owned_title_key`)
    author to be EQUAL in one of the forms :func:`_author_keys` allows -- the
    name as written or with a single "Surname, Given" comma flipped -- and the
    normalised title to be EQUAL *or* a word-aligned prefix of the other, so a
    short BookBub title ("Witch World: High Hallack Cycle") matches a Grimmory
    title that expands it with a colon/parenthetical subtitle ("...: The Jargoon
    Pard, …"). The equal-author requirement keeps unrelated title collisions
    (e.g. two different "Across the Universe" books) from being hidden; the flip
    is tried on BOTH sides because either source can be the one holding the
    inverted spelling.
    """
    tk = _owned_title_key(title)
    for author_key in _author_keys(author):
        for gtk in index.get(author_key, ()):
            if tk == gtk or tk.startswith(gtk + " ") or gtk.startswith(tk + " "):
                return True
    return False


def owned_lookup(deals, grimmory_path: str | Path) -> dict:
    """Map each deal's ``bookbub_url`` -> owned (``1``/``0``), or ``None``.

    Reads ``grimmory.db``'s ``book(title, author)`` rows (read-only) and marks
    a deal owned (``1``) when *any* grimmory book matches its normalised author
    and an exact-or-prefix normalised title (see :func:`_is_owned`); otherwise
    ``0``. If the grimmory DB file is missing, every deal maps to ``None``
    (stored as NULL — the audit column is left blank rather than raising a hard
    failure).
    """
    grimmory_path = Path(grimmory_path)
    if not grimmory_path.exists():
        return {d.url: None for d in deals}

    conn = sqlite3.connect(f"file:{grimmory_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT title, author FROM book").fetchall()
    finally:
        conn.close()

    index = _build_owned_index(rows)
    return {d.url: 1 if _is_owned(index, d.title, d.author) else 0 for d in deals}


def refresh_owned(conn: sqlite3.Connection, grimmory_path: str | Path) -> int:
    """Recompute ``owned_in_grimmory`` for EVERY deal row (the caller commits).

    Reads each row's ``(id, bookbub_url, title, author)`` and re-derives
    ``owned_in_grimmory`` against ``grimmory.db`` via :func:`owned_lookup`
    (the same normalised title+author matching the upsert uses), so ownership
    stays current for rows that were stored before the book was added to the
    library. When ``grimmory.db`` is absent every flag is set to NULL
    (ownership unknown) rather than left stale — the tab still shows such
    deals (``owned_in_grimmory IS NOT 1``). Returns the number of rows written
    (all of them); the caller commits.

    Used by the daily updater (``scripts/bookbub_daily.py``): ``store_deals``
    only computes ownership for the date it just (re)stores, so re-applying to
    every row each run keeps the flag fresh. The updater rebuilds
    ``grimmory.db`` from the Grimmory server immediately before calling this,
    so the file it reads is the current library. (Only the updater's Amazon
    re-verify pass runs inside the wlvpn netns, where Grimmory is unreachable;
    the fetch, the rebuild and this refresh all run on the host.)
    """
    import types

    rows = conn.execute(
        "SELECT id, bookbub_url, title, author FROM deal"
    ).fetchall()
    if not rows:
        return 0
    deal_objs = [
        types.SimpleNamespace(url=r[1], title=r[2], author=r[3]) for r in rows
    ]
    owned_map = owned_lookup(deal_objs, grimmory_path)
    n = 0
    for r in rows:
        row_id, bookbub_url = r[0], r[1]
        conn.execute(
            "UPDATE deal SET owned_in_grimmory = ? WHERE id = ?",
            (owned_map.get(bookbub_url), row_id),
        )
        n += 1
    return n


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
# Hidden books (a per-BOOK dismissal, not a per-row flag)
# --------------------------------------------------------------------------- #
def identity_keys(title: str | None, author: str | None,
                  amazon_url: str | None) -> list[str]:
    """Every ``hidden_book.book_key`` under which this book can be hidden.

    Always the normalised ``meta:<title>|<author>`` key; plus, when
    ``amazon_url`` carries a ``/dp/`` ASIN, ``asin:<ASIN>`` first. Derived from
    the same normalisation :func:`book_identity` uses, so "the same book" means
    the same thing to a hide as it does to :func:`deduplicate` -- which matters,
    because dedup deletes the very rows a hide has to outlive.

    A hide is written under ALL of these keys and looked up against ALL of
    them (unlike ``book_identity``, which returns the ASIN alone when there is
    one). That is deliberate: a deal's ``amazon_url`` can be an unresolved
    BookBub intermediate link on one date and the canonical product URL on the
    next, so keying on the ASIN alone would lose the hide in exactly the case
    this table exists to cover. ``normalise`` strips punctuation, so a "|" can
    never appear inside a key part and the two halves cannot run together.
    """
    keys = [f"meta:{normalise(title)}|{normalise(author)}"]
    asin = asin_from_amazon_url(amazon_url)
    if asin:
        keys.insert(0, f"asin:{asin}")
    return keys


def hidden_keys(conn: sqlite3.Connection) -> set[str]:
    """Every hidden book key, as a set for :func:`is_hidden` lookups.

    Read once per query rather than joined per row: the table is a few hundred
    rows at most, and the ``meta`` half of a key needs Python's
    :func:`normalise`, which SQLite cannot express.
    """
    return {r[0] for r in conn.execute("SELECT book_key FROM hidden_book")}


def is_hidden(keys: set[str], title: str | None, author: str | None,
              amazon_url: str | None) -> bool:
    """True when this deal's book is in ``keys`` (from :func:`hidden_keys`)."""
    return any(k in keys for k in identity_keys(title, author, amazon_url))


def _hide_keys(conn: sqlite3.Connection, title: str | None, author: str | None,
               amazon_url: str | None, at: str) -> int:
    """Write every key for one book into ``hidden_book`` (the caller commits).

    Re-hiding an already hidden book refreshes the stored title/author and
    keeps the original ``hidden_at``. Returns the number of keys written.
    """
    keys = identity_keys(title, author, amazon_url)
    for key in keys:
        conn.execute(
            "INSERT INTO hidden_book (book_key, title, author, hidden_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(book_key) DO UPDATE SET "
            "title = excluded.title, author = excluded.author",
            (key, title, author, at),
        )
    return len(keys)


def hidden_book_rows(conn: sqlite3.Connection) -> list[dict]:
    """Every ``hidden_book`` row as a dict, in key order (for the mirror)."""
    return [
        {"book_key": k, "title": t, "author": a, "hidden_at": h}
        for k, t, a, h in conn.execute(
            "SELECT book_key, title, author, hidden_at FROM hidden_book "
            "ORDER BY book_key"
        )
    ]


def replace_hidden_books(conn: sqlite3.Connection, rows) -> int:
    """Replace the whole ``hidden_book`` table with ``rows`` (the caller
    commits, and is expected to already be inside a transaction).

    Whole-table, like the deals mirror around it: the table is tiny, carries
    no ids anything else references, and a hide the primary cleared has to
    disappear here too. Returns the number of rows written.
    """
    conn.execute("DELETE FROM hidden_book")
    payload = [
        (
            r.get("book_key"),
            r.get("title"),
            r.get("author"),
            r.get("hidden_at") or _dt.datetime.now().isoformat(timespec="seconds"),
        )
        for r in rows
        if r.get("book_key")
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO hidden_book (book_key, title, author, hidden_at) "
        "VALUES (?, ?, ?, ?)",
        payload,
    )
    return len(payload)


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
    """Unverified, non-hidden deals that have an Amazon ASIN, in row order.

    Returns ``{id, asin, amazon_url, deal_price, title}`` for rows where
    ``deal_status IS NULL`` and whose ``amazon_url`` contains an ASIN.
    ``limit`` caps the number of dicts returned (after the ASIN filter).
    Hidden books are skipped for the same reason as in :func:`recheck_deals`:
    ``current_deals`` never shows them, so verifying them buys nothing.
    """
    hidden = hidden_keys(conn)
    rows = conn.execute(
        "SELECT id, amazon_url, deal_price, title, cover, author FROM deal "
        "WHERE deal_status IS NULL AND amazon_url IS NOT NULL "
        "ORDER BY id"
    ).fetchall()
    out: list[dict] = []
    for row_id, url, deal_price, title, cover, author in rows:
        if is_hidden(hidden, title, author, url):
            continue
        asin = asin_from_amazon_url(url)
        if asin:
            out.append(
                {
                    "id": row_id,
                    "asin": asin,
                    "amazon_url": url,
                    "deal_price": deal_price,
                    "title": title,
                    "cover": cover,
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
    """Deals the daily updater must (re-)verify: everything except expired and hidden.

    Like :func:`pending_deals` but selects rows whose ``deal_status`` is NULL
    (never checked) or ``current`` / ``unknown`` (still believed live, or
    unreadable last time — worth another look). Same
    ``{id, asin, amazon_url, deal_price, title}`` shape as ``pending_deals``,
    ASIN-filtered the same way, ``limit`` applied after the ASIN filter.

    Two terminal exclusions, both meaning "this row can never be shown again,
    so re-reading Amazon for it buys nothing":

    * An ``expired`` deal is terminal and is NEVER re-checked (requirement:
      "Expired deals are never checked again").
    * A ``hidden`` book was dismissed by the user (``hidden_book``, matched by
      book identity); ``current_deals`` leaves it off the tab, so its price is
      never displayed. These dominated the scope
      in practice — 271 of 390 recheckable rows on 2026-09-01 — so skipping
      them cuts roughly two thirds of the Amazon reads out of every nightly
      pass, which is both faster and a smaller anti-bot footprint.

    The trade-off, deliberately accepted: un-hiding a deal later surfaces
    whatever status it had when it was hidden, since nothing refreshed it in
    the meantime.
    """
    hidden = hidden_keys(conn)
    rows = conn.execute(
        "SELECT id, amazon_url, deal_price, title, cover, author FROM deal "
        "WHERE (deal_status IS NULL OR deal_status IN (?, ?)) "
        "AND amazon_url IS NOT NULL ORDER BY id",
        (DEAL_STATUS_CURRENT, DEAL_STATUS_UNKNOWN),
    ).fetchall()
    out: list[dict] = []
    for row_id, url, deal_price, title, cover, author in rows:
        if is_hidden(hidden, title, author, url):
            continue
        asin = asin_from_amazon_url(url)
        if asin:
            out.append(
                {
                    "id": row_id,
                    "asin": asin,
                    "amazon_url": url,
                    "deal_price": deal_price,
                    "title": title,
                    "cover": cover,
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


def update_cover_desc(
    conn: sqlite3.Connection,
    row_id: int,
    cover: str | None,
    description: str | None,
) -> None:
    """Persist the captured book cover filename + Amazon description for a row
    (the caller commits).

    ``cover`` is the filename under ``data/covers/`` (``None`` when nothing was
    captured); ``description`` is the captured description text (``None`` when
    absent from the page). Called from the verification loop — best-effort, so
    it never raises for missing/short values.
    """
    conn.execute(
        "UPDATE deal SET cover = ?, description = ? WHERE id = ?",
        (cover or None, description or None, row_id),
    )


def update_rating(
    conn: sqlite3.Connection,
    row_id: int,
    stars: float | int | str | None,
    ratings: int | str | None,
) -> None:
    """Persist the Amazon star rating + rating count captured during a check
    (the caller commits).

    ``stars`` is the rating in 0-5 (e.g. 4.5), ``ratings`` the number of
    ratings. Either may be None when the page didn't carry it — a None is
    stored as NULL, never clobbering the other field. Called from the
    verification loop — best-effort.
    """
    conn.execute(
        "UPDATE deal SET stars = ?, ratings = ? WHERE id = ?",
        (float(stars) if stars not in (None, "") else None,
         int(str(ratings).replace(",", "")) if ratings not in (None, "") else None,
         row_id),
    )


def current_deals(
    conn: sqlite3.Connection, show_hidden: bool = False, min_stars: float = 0.0
) -> list[dict]:
    """Live BookBub deals for the web app's BookBub Deals tab.

    Returns ``{id, title, author, date, deal_price, deal_price_cents,
    original_price, amazon_url, hidden, cover, description, stars,
    ratings}`` dicts for every row the app presents as an in-flight deal:
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
    Deals whose BOOK the user hid (a :func:`hidden_keys` match, not the legacy
    ``deal.hidden`` column) are excluded unless ``show_hidden`` is true; the
    resulting ``hidden`` 1/0 is returned in each dict so the UI can render the
    hide checkbox. Matching by book means a hide still holds after BookBub
    re-features the book on a later date under a new row id. ``cover`` is the captured cover image
    filename (``data/covers/``; None when never captured) and ``description``
    the captured Amazon description text (shown as a hover tooltip), both
    None when the page has never been verified. ``stars`` (float 0-5) and
    ``ratings`` (int) are the Amazon rating captured on the last check (None
    when a page never carried them). ``min_stars`` filters to rows whose
    ``stars >= min_stars`` (rows with no rating are excluded when
    ``min_stars`` > 0; ``min_stars`` 0 or negative shows all).
    """
    hidden_set = hidden_keys(conn)
    rows = conn.execute(
        "SELECT id, date, title, author, deal_price, original_price, amazon_url, "
        "cover, description, stars, ratings "
        "FROM deal WHERE deal_status = ? AND amazon_url IS NOT NULL "
        "AND owned_in_grimmory IS NOT 1 "
        "AND (? <= 0 OR (stars IS NOT NULL AND stars >= ?)) "
        "ORDER BY date DESC, id DESC",
        (DEAL_STATUS_CURRENT, min_stars, min_stars),
    ).fetchall()
    out: list[dict] = []
    for (row_id, date, title, author, deal_price, original_price, amazon_url,
         cover, description, stars, ratings) in rows:
        hidden = 1 if is_hidden(hidden_set, title, author, amazon_url) else 0
        if hidden and not show_hidden:
            continue
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
                "cover": cover,
                "description": description,
                "stars": stars,
                "ratings": ratings,
            }
        )
    return out


def set_hidden(conn: sqlite3.Connection, row_id: int, hidden: bool) -> bool:
    """Hide or un-hide the BOOK behind deal ``row_id`` (the caller commits).

    Addressed by row id because that is what the tab's checkbox knows, but the
    hide is stored against the row's book identity (:func:`identity_keys`), so
    it covers every ``deal`` row for that book -- past, present, and the rows
    tomorrow's BookBub fetch will create. Un-hiding drops every key for the
    book, so it un-hides all of them too.

    Returns True when the row existed, False when the id is unknown (so the
    caller can answer 404).
    """
    row = conn.execute(
        "SELECT title, author, amazon_url FROM deal WHERE id = ?", (row_id,)
    ).fetchone()
    if row is None:
        return False
    title, author, amazon_url = row
    if hidden:
        _hide_keys(
            conn, title, author, amazon_url,
            _dt.datetime.now().isoformat(timespec="seconds"),
        )
    else:
        keys = identity_keys(title, author, amazon_url)
        placeholders = ", ".join("?" * len(keys))
        conn.execute(
            f"DELETE FROM hidden_book WHERE book_key IN ({placeholders})", keys
        )
    return True


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
