#!/usr/bin/env python
"""De-duplicate the BookBub deals database (keep the most recent deal per book).

A book can be featured on several days, and each day's page is stored, so the
same book can appear as multiple `deal` rows (one per date). This one-off
collapses those repeats: it first backs up the deals database to a timestamped
WAL-safe snapshot (``data/deals_pre_dedup_<YYYYMMDD-HHMMSS>.db`` via the
sqlite backup API, gitignored), then removes every but the most recent deal
per book via ``deals_db.deduplicate`` (identity = ``deals_db.book_identity``:
the Amazon ASIN from ``amazon_url``, falling back to normalised
title+author for deals with no Amazon link). The kept row is never modified.

Usage (from the repo root):

    python scripts/dedup_deals.py [--db PATH] [--backup PATH] [--check]

Options:
    --db        Deals database to de-duplicate (default: DEALS_DB = data/deals.db).
    --backup    Backup path (default: <db dir>/deals_pre_dedup_<YYYYMMDD-HHMMSS>.db).
    --check     Dry run: print the stats and the rows that would be removed,
                modify nothing and create no backup.

Exit codes: 0 = success (including --check); 1 = error.
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# `python scripts/dedup_deals.py` puts scripts/ on sys.path[0], so make the
# repo root importable the same way a caller at the root would have it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import deals_db  # noqa: E402
from app.config import DEALS_DB  # noqa: E402


def _group_by_identity(conn: sqlite3.Connection) -> dict:
    """Group the deal rows by ``deals_db.book_identity``.

    Returns ``{identity: [(date, id), ...]}``.
    """
    rows = conn.execute("SELECT id, date, title, author, amazon_url FROM deal").fetchall()
    groups: dict = {}
    for row_id, date, title, author, amazon_url in rows:
        groups.setdefault(deals_db.book_identity(title, author, amazon_url), []).append(
            (date or "", row_id))
    return groups


def _default_backup_path(db: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return db.parent / f"deals_pre_dedup_{ts}.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="De-duplicate the deals DB, keeping the most recent deal per book.")
    parser.add_argument("--db", type=Path, default=DEALS_DB,
                        help=f"Deals database to de-duplicate (default: {DEALS_DB}).")
    parser.add_argument("--backup", type=Path, default=None,
                        help="Backup path (default: <db dir>/deals_pre_dedup_<YYYYMMDD-HHMMSS>.db).")
    parser.add_argument("--check", action="store_true",
                        help="Dry run: show the stats and rows that would be removed; modify nothing.")
    args = parser.parse_args(argv)

    db = args.db
    if not db.exists():
        print(f"ERROR: deals database not found: {db}", file=sys.stderr)
        return 1

    try:
        conn = sqlite3.connect(str(db))
        groups = _group_by_identity(conn)

        # The keeper of each group is the max (date, id) — newest date, tie ->
        # highest id (same rule as deals_db.deduplicate).
        keeper = {ident: max(members) for ident, members in groups.items()}
        remove_ids = sorted(i for ident, members in groups.items()
                            for _d, i in members if (_d, i) != keeper[ident])

        total = sum(len(m) for m in groups.values())
        repeated = {ident: m for ident, m in groups.items() if len(m) > 1}
        print(f"deals DB: {db}")
        print(f"rows: {total} | distinct books: {len(groups)} | "
              f"repeated books: {len(repeated)} (would remove {len(remove_ids)} rows)")

        if args.check:
            for ident in sorted(repeated):
                members = sorted(repeated[ident])
                keep_d, keep_i = keeper[ident]
                for d, i in members:
                    if i == keep_i:
                        continue
                    title, author, bub = conn.execute(
                        "SELECT title, author, bookbub_url FROM deal WHERE id=?", (i,)).fetchone()
                    print(f"  would remove id={i} {d} {title!r} — {author} "
                          f"(kept {keep_d}, identity={ident})")
            print("check only — nothing was modified, no backup created.")
            conn.close()
            return 0

        backup_path = args.backup or _default_backup_path(db)
        # WAL-consistent snapshot via the sqlite backup API (not a raw file
        # copy, which can be inconsistent while a -wal file is outstanding).
        src = sqlite3.connect(str(db))
        dst = sqlite3.connect(str(backup_path))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
            src.close()
        print(f"backup: {backup_path}")

        removed = deals_db.deduplicate(conn)
        conn.commit()
        conn.close()

        print(f"removed {removed} duplicate rows; kept the most recent deal per book.")
        print(f"rows now: {total - removed} == distinct books: {len(groups)}")
        return 0
    except sqlite3.Error as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
