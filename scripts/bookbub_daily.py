#!/usr/bin/env python
"""Daily BookBub updater — fetch today's deals, store them, re-verify the rest.

Runs 18:00 local every day via ``amazon-wishlist-bookbub.timer`` (which places
it inside the ``wlvpn`` NordVPN/WireGuard namespace so the Amazon price reads
egress only through the tunnel — the same fail-closed guarantee as the one-shot
live-deal verifier).

One run does the whole daily cycle:

1. **Fetch + store** — pull the day's BookBub ebook deals
   (``bookbub.fetch_daily_deals``: login via the daily email's outbound link,
   Playwright clears the Cloudflare managed challenge) and upsert them into
   ``DEALS_DB`` via ``deals_db.store_deals``. The upsert is idempotent on
   ``(date, bookbub_url)`` and computes ``owned_in_grimmory`` against
   ``GRIMMORY_DB`` and sets ``no_amazon_link``. (requirements 3 + 4)

2. **Re-verify** — invoke ``scripts/verify_deals.py`` in ``--recheck`` mode
   (as a module) so every deal that is NOT yet ``expired`` — i.e.
   ``current`` / ``unknown`` / unchecked — is re-read against its current
   Amazon price and re-marked current/expired/unknown. ``expired`` deals are
   terminal and are never re-checked. (requirement 6)

Newly-stored deals flow into the BookBub Deals tab automatically as soon as
they are marked ``current`` (the tab lists ``current_deals``). (requirement 5)

``--check`` is a dry run: it prints today's date, whether a BookBub login link
is set, and the recheckable (non-expired) deal count read straight from the
DB. It touches no network, browser, or tunnel.

Login-link prerequisite: ``BOOKBUB_LOGIN_LINK`` (or ``--link``) is a per-day
rotating token from the daily BookBub email. When it is ABSENT the real run
exits 2 with a clear message and changes nothing; a stale/expired link makes
the fetch fail (logged) but the re-verify step still runs, and the overall
exit code is 1 so the operator notices. A missing ``data/grimmory.db`` leaves
the owned flag NULL (such deals still show).

Usage (from the repo root):
    python scripts/bookbub_daily.py --check
    BOOKBUB_LOGIN_LINK='<today's outbound link>' python scripts/bookbub_daily.py
    # Ubuntu tunnel mode (run inside the namespace via the systemd unit):
    python scripts/bookbub_daily.py --netns wlvpn

Exit codes: 0 = success (and a clean --check); 1 = fetch failed or the verify
pass failed; 2 = no BookBub login link (real run only).
"""

import argparse
import logging
import sys
from pathlib import Path

# `python scripts/bookbub_daily.py` puts scripts/ on sys.path[0] (so the sibling
# `verify_deals` module is importable) and the repo root on the same path (so
# `app` is importable) — the same convention as build_bookbub_deals.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import bookbub, deals_db  # noqa: E402
from app.config import (  # noqa: E402
    BOOKBUB_LOGIN_LINK,
    DEALS_DB,
    GRIMMORY_DB,
    NORDVPN_ROTATE_EVERY,
)

log = logging.getLogger("bookbub_daily")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Daily BookBub updater: fetch + store today's deals, then "
                    "re-verify every non-expired deal against live Amazon prices."
    )
    ap.add_argument("--db", default=str(DEALS_DB),
                    help=f"Deals DB path (default: {DEALS_DB}).")
    ap.add_argument("--date", default=None,
                    help="Day to pull, YYYYMMDD (default: today).")
    ap.add_argument("--link", default=None,
                    help="BookBub outbound auto-login link (else BOOKBUB_LOGIN_LINK env).")
    ap.add_argument("--netns", default="", metavar="NS",
                    help="Tunnel mode for the verify pass: run INSIDE the given network "
                         "namespace (e.g. 'wlvpn'). Forwarded to verify_deals.py.")
    ap.add_argument("--rotate-every", type=int, default=NORDVPN_ROTATE_EVERY,
                    help="Nord exit-IP/fingerprint rotation interval forwarded to "
                         "verify_deals.py (default NORDVPN_ROTATE_EVERY).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Forwarded to verify_deals.py: verify at most N deals.")
    ap.add_argument("--check", action="store_true",
                    help="Dry run: print the date, login-link status, and the "
                         "recheckable deal count from the DB. No network/browser/tunnel.")
    args = ap.parse_args()
    if args.rotate_every < 1:
        ap.error("--rotate-every must be >= 1")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    date = bookbub._normalise_date(args.date)
    link = args.link or BOOKBUB_LOGIN_LINK

    # --check: dry run. Reads only the DB; no network, browser, or tunnel.
    if args.check:
        n = 0
        if Path(args.db).exists():
            conn = deals_db.connect(args.db)
            try:
                deals_db.ensure_schema(conn)
                n = len(deals_db.recheck_deals(conn, limit=args.limit))
            finally:
                conn.close()
        link_state = "set" if link else "no BOOKBUB_LOGIN_LINK set"
        print(f"check: date {date} | {link_state} | {n} recheckable (non-expired) "
              f"deal(s) with an ASIN in {args.db} | netns {args.netns or '(host CLI)'}")
        return 0

    # Real run: the login link is a hard prerequisite.
    if not link:
        log.error(
            "no BookBub login link: set BOOKBUB_LOGIN_LINK (today's outbound link "
            "from the daily BookBub email) or pass --link. Exiting 2; nothing "
            "fetched and the deals DB was left untouched."
        )
        return 2

    # (1) Fetch + store today's deals. A fetch failure (stale/expired link,
    #     Cloudflare block, or a genuinely empty day) is logged, not fatal to
    #     the re-verify step; it just makes the overall exit code 1.
    fetch_failed = False
    deals = []
    try:
        deals = bookbub.fetch_daily_deals(date, link=link)
        log.info("fetched %d deal(s) for %s", len(deals), date)
    except bookbub.BookbubError as e:
        fetch_failed = True
        log.warning("BookBub fetch failed for %s: %s (continuing to re-verify)", date, e)

    try:
        stored, owned, no_amazon = deals_db.store_deals(
            deals, date, deals_path=args.db, grimmory_path=GRIMMORY_DB
        )
        log.info("stored %d deal(s) for %s in %s (%d owned in grimmory, %d no-amazon-link)",
                 stored, date, args.db, owned, no_amazon)
    except Exception as e:  # a store failure is fatal — the re-verify would churn stale rows
        log.error("failed to store deals in %s: %s", args.db, e)
        return 1

    # (2) Re-verify every non-expired deal (expired is never re-checked).
    #     verify_deals.main() parses sys.argv[1:], so swap it for our own args.
    import verify_deals
    verify_argv = ["--recheck", "--db", args.db, "--rotate-every", str(args.rotate_every)]
    if args.netns:
        verify_argv += ["--netns", args.netns]
    if args.limit is not None:
        verify_argv += ["--limit", str(args.limit)]
    saved_argv = sys.argv
    sys.argv = ["verify_deals.py", *verify_argv]
    try:
        verify_rc = verify_deals.main()
    finally:
        sys.argv = saved_argv

    if verify_rc == 0 and not fetch_failed:
        log.info("daily BookBub updater: done for %s", date)
        return 0
    if verify_rc != 0:
        log.error("verify pass exited %d for %s", verify_rc, date)
        return verify_rc
    # fetch failed but verify succeeded: report the fetch problem (exit 1).
    log.error("fetch failed for %s (link stale/expired or empty day); verify ran. "
              "Refreshing BOOKBUB_LOGIN_LINK will fix the fetch.", date)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
