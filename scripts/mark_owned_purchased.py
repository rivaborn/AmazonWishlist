#!/usr/bin/env python
"""Mark books you own (in Grimmory) as `purchased` in the wishlist DB.

For every `book` row in wishlist.db, match its (title, author) against
``grimmory.db`` using the SAME ownership matching the BookBub deals use
(a normalised title with parenthetical subtitles stripped + a normalised
author — `deals_db._owned_title_key` / `deals_db.normalise`). Books that are
owned get ``purchased = 1``, so they drop off the deal views and show under
/purchased. Useful after importing more of your library into Grimmory.

Dry-run by default (prints what WOULD be marked); pass ``--apply`` to write.
Reading ``grimmory.db`` is read-only (mode=ro).

Usage (from the repo root):
    python scripts/mark_owned_purchased.py                 # dry run
    python scripts/mark_owned_purchased.py --apply          # write
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, deals_db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wishlist-db", default=str(config.DB_PATH),
                    help=f"wishlist DB path (default: {config.DB_PATH})")
    ap.add_argument("--grimmory-db", default=str(config.GRIMMORY_DB),
                    help=f"grimmory DB path (default: {config.GRIMMORY_DB})")
    ap.add_argument("--apply", action="store_true",
                    help="write purchased=1 (default is a dry-run)")
    args = ap.parse_args()

    if not Path(args.grimmory_db).exists():
        print(f"ERROR: no grimmory DB at {args.grimmory_db}", file=sys.stderr)
        return 2
    if not Path(args.wishlist_db).exists():
        print(f"ERROR: no wishlist DB at {args.wishlist_db}", file=sys.stderr)
        return 2

    # Index grimmory rows with the shared ownership matcher (paren-stripped +
    # normalised title/author, exact-or-prefix) so the wishlist scan stays
    # consistent with the BookBub deals owned-lookup.
    g = sqlite3.connect(f"file:{Path(args.grimmory_db).as_posix()}?mode=ro", uri=True)
    try:
        grimm = g.execute("SELECT title, author FROM book").fetchall()
    finally:
        g.close()
    index = deals_db._build_owned_index(grimm)
    print(f"grimmory owned-set: {len(grimm)} book(s)")

    d = sqlite3.connect(args.wishlist_db)
    try:
        books = d.execute(
            "SELECT asin, title, author, purchased FROM book ORDER BY asin"
        ).fetchall()
    finally:
        d.close()
    total = len(books)
    already = sum(1 for (_a, _t, _au, p) in books if p)
    to_mark = [
        asin for (asin, title, author, p) in books
        if not p and deals_db._is_owned(index, title, author)
    ]
    print(f"wishlist books: {total} total, {already} already purchased")
    print(f"owned and NOT yet purchased: {len(to_mark)}")
    print(f"  (after this, {already + len(to_mark)} / {total} would be purchased)")

    if args.apply:
        d = sqlite3.connect(args.wishlist_db)
        try:
            d.executemany("UPDATE book SET purchased = 1 WHERE asin = ?",
                          [(a,) for a in to_mark])
            d.commit()
        finally:
            d.close()
        print(f"MARKED {len(to_mark)} book(s) as purchased")
    else:
        print("DRY RUN — nothing written. Re-run with --apply to mark them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
