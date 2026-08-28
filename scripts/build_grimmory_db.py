#!/usr/bin/env python
"""One-off build of the gitignored Grimmory book catalog (data/grimmory.db).

Fetches every book from the configured Grimmory libraries (GRIMMORY_LIBRARIES
in app/config, default "Amazon fksogbetun,Amazon rivaborn") via the
BookLore v1 API (see app/grimmory.py) and writes them to a standalone SQLite
database with one `book` table:

    library_id, library_name, title, author, publisher, published_date, isbn

Usage (from the repo root; credentials come from the environment, never
from this file):

    GRIMMORY_USERNAME=... GRIMMORY_PASSWORD=... python scripts/build_grimmory_db.py

The `book` table is rebuilt on every run, so the DB always reflects the
current library state. The rebuild runs in a single transaction against a
staging table that is then renamed over the old one, so a failure partway
through leaves the previous data intact -- and the script exits non-zero
with a clear message on any error (bad login, missing target library,
HTTP failure).
"""
import sqlite3
import sys
from pathlib import Path

# `python scripts/build_grimmory_db.py` puts scripts/ on sys.path[0], so make
# the repo root importable the same way a caller at the root would have it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import grimmory  # noqa: E402
from app.config import GRIMMORY_DB  # noqa: E402

_COLUMNS = (
    "library_id",
    "library_name",
    "title",
    "author",
    "publisher",
    "published_date",
    "isbn",
)

_SCHEMA = """
CREATE TABLE {table} (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id     INTEGER,
    library_name   TEXT,
    title          TEXT,
    author         TEXT,
    publisher      TEXT,
    published_date TEXT,
    isbn           TEXT
);
"""


def _table_exists(conn, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def build(token: str, db_path: Path) -> dict:
    """Fetch every target library's books and (re)write the book table.

    Returns {library_name: book_count}. The whole rebuild is one
    transaction; on any error it rolls back and the old table survives.
    """
    libraries = grimmory.list_libraries(token)
    by_name = {lib.get("name"): lib for lib in libraries if isinstance(lib, dict)}

    targets = grimmory.target_library_names()
    missing = [name for name in targets if name not in by_name or by_name[name].get("id") is None]
    if missing:
        available = ", ".join(sorted(str(n) for n in by_name)) or "<none>"
        raise grimmory.GrimmoryError(
            "target library(ies) not found on Grimmory: "
            + ", ".join(missing)
            + f" (available: {available})"
        )

    rows = []
    per_library = {}
    for name in targets:
        lib = by_name[name]
        books = grimmory.fetch_library_books(token, lib.get("id"))
        rows.extend(grimmory.books_to_rows(books, lib.get("id"), name))
        per_library[name] = len(books)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN")
        # Stage in a scratch table when the real one already exists so a
        # failed rebuild never leaves the good table half-written.
        staging = "book" if not _table_exists(conn, "book") else "book_new"
        conn.execute(_SCHEMA.format(table=staging))
        conn.executemany(
            f"INSERT INTO {staging} ({', '.join(_COLUMNS)}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [tuple(row[col] for col in _COLUMNS) for row in rows],
        )
        if staging != "book":
            conn.execute("DROP TABLE book")
            conn.execute(f"ALTER TABLE {staging} RENAME TO book")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return per_library


def main() -> int:
    try:
        token = grimmory.login()
        per_library = build(token, GRIMMORY_DB)
    except (grimmory.GrimmoryError, sqlite3.Error) as e:
        print(f"GRIMMORY DB BUILD FAILED: {e}", file=sys.stderr)
        return 1
    for name in grimmory.target_library_names():
        print(f"{name}: {per_library[name]} books")
    print(f"wrote {sum(per_library.values())} books to {GRIMMORY_DB}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
