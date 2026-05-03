import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS wishlist (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    url       TEXT    NOT NULL UNIQUE,
    label     TEXT,
    added_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS book (
    asin         TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    author       TEXT,
    product_url  TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wishlist_book (
    wishlist_id INTEGER NOT NULL REFERENCES wishlist(id) ON DELETE CASCADE,
    asin        TEXT    NOT NULL REFERENCES book(asin),
    PRIMARY KEY (wishlist_id, asin)
);

CREATE TABLE IF NOT EXISTS price_snapshot (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    asin                TEXT    NOT NULL REFERENCES book(asin),
    observed_at         TEXT    NOT NULL,
    current_price_cents INTEGER,
    list_price_cents    INTEGER,
    availability        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snap_asin_time
    ON price_snapshot(asin, observed_at DESC);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()
