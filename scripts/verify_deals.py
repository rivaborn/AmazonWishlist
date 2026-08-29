#!/usr/bin/env python
"""Verify that stored BookBub deals are still live on Amazon.

For every PENDING deal in the deals DB (``deal_status IS NULL`` with an
Amazon ASIN) this orchestrator:

1. ensures the NordVPN tunnel is connected (host CLI; starting country from
   ``NORDVPN_START_COUNTRY``) — or, in ``--netns`` tunnel mode (the Ubuntu
   deployment: this process runs INSIDE a network namespace whose only route
   is the NordLynx/WireGuard tunnel), that the namespace has live egress,
   rebuilding it once via the tunnel unit when it is missing/dead. Tunnel
   mode is FAIL-CLOSED: the namespace's only route is the tunnel (it can
   never fall back to the host's plain IP), egress is re-verified before
   EVERY Amazon read, and the run ABORTS the moment the tunnel loses live
   egress — Amazon is never fetched unless the tunnel is verifiably up,
2. opens the book's ``amazon.com/dp/<ASIN>`` page in a Playwright context
   built from a rotated browser fingerprint (``app.fingerprint``),
3. reads the current price (``app.amazon_price``), classifies the deal
   against the stored ``deal_price`` (``app.deals_db.classify_deal``:
   at/below = ``current``, above = ``expired``, unreadable = ``unknown``),
   and records the outcome via ``mark_verified`` (``deal_status`` /
   ``current_price`` / ``verified_at`` in DEALS_DB — the source of truth for
   resuming; ``data/verify_progress.json`` is an advisory telemetry mirror),
4. every ``--rotate-every`` books (default 10) refreshes the exit IP — host
   CLI: connect to a new country/city; ``--netns``: best-effort tunnel
   rebuild for a fresh session/exit IP (a not-permitted or failed rebuild
   leaves the stable IP and the run continues) — AND switches to a fresh
   fingerprint that differs in User-Agent/locale/viewport, so the IP and the
   browser identity change together,
5. paces per-book reads with a random jitter delay (``VERIFY_DELAY_MIN/MAX``)
   and retries transient failures (block page / navigation failure / no
   price) with backoff up to ``VERIFY_MAX_RETRIES`` — a book that stays
   unreadable is recorded ``unknown`` (never guessed).

``--check`` is a dry run: it prints the pending count + effective config and
exits 0 without touching the VPN, the browser, or the DB.

The operator runs the real pass over the real ``data/deals.db``; step
verification runs a small bounded pass over a TEMP copy (``--db``) so the
real DB is not mutated during review.

Tunnel mode (Ubuntu deployment): pass ``--netns NS`` (``amazon-wishlist-verify.service``
and ``scripts/vpn_verify.sh`` pass ``--netns wlvpn``) while running INSIDE the
namespace. The host-CLI path (login/connect/rotate) is then unused and no
NordVPN credentials are needed — the session was pre-negotiated by
``amazon-wishlist-vpn.service`` as the operator user. Within the namespace the
tunnel's exit IP is fixed for its life, so a per-N "rotation" is a best-effort
rebuild (fresh IP when permitted; the fingerprint still rotates either way).

Usage (from repo root, VPN + credentials available):
    python scripts/verify_deals.py --check
    python scripts/verify_deals.py --limit 25 --rotate-every 10
    NORDVPN_USERNAME=… NORDVPN_PASSWORD=… python scripts/verify_deals.py
    # Ubuntu tunnel mode (the process runs inside the namespace):
    python scripts/verify_deals.py --netns wlvpn --limit 25 --rotate-every 10
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import amazon_price, deals_db, fingerprint as fp, nordvpn  # noqa: E402
from app.config import (  # noqa: E402
    DEALS_DB,
    LLM_MODEL,
    NORDVPN_COUNTRIES,
    NORDVPN_ROTATE_EVERY,
    NORDVPN_START_COUNTRY,
    VERIFY_DELAY_MAX,
    VERIFY_DELAY_MIN,
    VERIFY_MAX_RETRIES,
    VERIFY_PROGRESS,
    VERIFY_RETRY_BACKOFF,
    VERIFY_TUNNEL_RETRIES,
    VERIFY_TUNNEL_RETRY_DELAY,
    WISHLIST_VPN_UNIT,
)

log = logging.getLogger("verify_deals")


def _selection(args):
    """Row selector for this run.

    ``--recheck`` (the daily updater's mode) re-verifies everything that is
    not terminal: ``current`` + ``unknown`` + unchecked. Default mode only
    checks the never-checked deals. ``expired`` is excluded either way.
    """
    return deals_db.recheck_deals if getattr(args, "recheck", False) else deals_db.pending_deals


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _write_progress(data: dict) -> None:
    """Atomically mirror run telemetry to VERIFY_PROGRESS (advisory only)."""
    try:
        VERIFY_PROGRESS.parent.mkdir(parents=True, exist_ok=True)
        tmp = VERIFY_PROGRESS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, VERIFY_PROGRESS)
    except Exception as exc:  # telemetry must never take the run down
        log.warning("could not write progress mirror %s: %s", VERIFY_PROGRESS, exc)


def _load_progress() -> dict | None:
    """Read the advisory progress mirror (None when missing/unreadable)."""
    try:
        return json.loads(VERIFY_PROGRESS.read_text(encoding="utf-8"))
    except Exception:
        return None


def _transient(result: amazon_price.PriceResult) -> bool:
    """True when a failed read is transient (worth a backoff + retry).

    A block page or a failed navigation is transient. A page that loaded but
    yielded no price is also retried (rendering may not have settled); only
    the final attempt's result is recorded as ``unknown``.
    """
    if result.price_cents is not None:
        return False
    return (
        result.note.startswith("blocked:")
        or result.note.startswith("navigation failed:")
        or "no price" in result.note
    )


def _read_with_retries(
    read, deal: dict, f: fp.Fingerprint, summary: dict
) -> amazon_price.PriceResult:
    """Call ``read`` for one book, retrying transient failures with backoff."""
    attempts = 0
    while True:
        result = read(deal["amazon_url"], fingerprint=f, asin=deal["asin"])
        if not _transient(result) or attempts >= VERIFY_MAX_RETRIES:
            return result
        attempts += 1
        summary["retries"] = summary.get("retries", 0) + 1
        log.warning(
            "  book %s (%s): transient (%s); retry %d/%d in %.0fs",
            deal["id"], deal["asin"], result.note, attempts, VERIFY_MAX_RETRIES,
            VERIFY_RETRY_BACKOFF,
        )
        time.sleep(VERIFY_RETRY_BACKOFF)


def _ensure_tunnel(ns: str) -> tuple[bool, str | None]:
    """Tunnel mode: make sure the namespace has live egress.

    Returns ``(live, egress_ip)``. When the namespace is missing or dead it is
    rebuilt once (best-effort ``systemctl restart`` of the tunnel unit — needs
    root or a scoped sudoers rule); still dead after that is an environment
    prerequisite failure the caller reports and exits 1. The host nordvpn CLI
    is not involved: the session was pre-negotiated by
    amazon-wishlist-vpn.service as the operator user, so no credentials are
    needed here.
    """
    ip = nordvpn.tunnel_egress_ip()
    if ip:
        return True, ip
    why = "missing" if not nordvpn.netns_exists(ns) else "present but not egressing"
    log.warning("netns %r is %s; attempting a one-time tunnel rebuild...", ns, why)
    if not nordvpn.rebuild_tunnel():
        log.warning("tunnel rebuild not permitted/failed (needs root); re-checking egress anyway")
    ip2 = nordvpn.tunnel_egress_ip()
    if ip2:
        return True, ip2
    return False, None


def _tunnel_live(ns: str) -> bool:
    """Tunnel mode, FAIL-CLOSED gate: is the namespace egressing through Nord *right now*?

    Liveness is a PLAIN curl (``tunnel_egress_ip``), NOT ``ip netns exec``: this
    process runs INSIDE the namespace as a non-root user whose only route is the
    tunnel, so a plain curl either returns the tunnel's egress IP (live) or
    fails (dead) — and the leak-proof namespace means it can never fall back to
    the host's IP. ``ip netns exec`` would additionally need root, which the
    verifier does not have. A dead/stale tunnel therefore never causes Amazon
    traffic to egress from the host's plain IP.

    A transient blip (one failed check) is retried several times over a short
    window (VERIFY_TUNNEL_RETRIES / _RETRY_DELAY, default ~60s) before we give
    up, so a momentary Nord rekey/reconnect does not abort a multi-hour run —
    but while egress is down NO Amazon read happens (the gate blocks first) and
    the run stops rather than continue once the window is exhausted.
    """
    for _ in range(VERIFY_TUNNEL_RETRIES):
        if nordvpn.tunnel_egress_ip():
            return True
        time.sleep(VERIFY_TUNNEL_RETRY_DELAY)
    return nordvpn.tunnel_egress_ip() is not None


def _resolve_scope(conn, db_path: Path, args) -> list[dict]:
    """Pick this run's work set (the "scope"), for resumability.

    The scope is the set of pending deal ids captured when the run started; it
    is mirrored (advisory) into VERIFY_PROGRESS, keyed by (db, limit) so a
    bounded test pass never poisons the full run. Re-running the SAME command
    resumes the saved scope and skips rows already verified in the DB (the
    source of truth) — so a finished scope is a fast no-op. A different
    --db/--limit (or --fresh) starts a new scope.
    """
    key = [str(db_path), args.limit, bool(getattr(args, "recheck", False))]
    meta = None if args.fresh else _load_progress()
    if meta and meta.get("key") == key and meta.get("scope"):
        scope = set(meta["scope"])
        work = [d for d in _selection(args)(conn) if d["id"] in scope]
        if not work:
            print(f"nothing to verify: all {len(scope)} deal(s) in this run's scope "
                  f"are already verified (re-run of a completed scope). "
                  f"Use --fresh to start a new scope.")
            return []
        print(f"resuming saved scope: {len(work)}/{len(scope)} deal(s) still pending")
        return work
    pending = _selection(args)(conn, limit=args.limit)
    _write_progress(
        {
            "key": key,
            "db": str(db_path),
            "scope": [d["id"] for d in pending],
            "processed": 0,
            "summary": {"current": 0, "expired": 0, "unknown": 0, "rotations": 0},
            "last_rotated_ip": None,
            "at": _now(),
        }
    )
    return pending


def _run(args) -> int:
    db_path = Path(args.db)
    conn = deals_db.connect(db_path)
    deals_db.ensure_schema(conn)  # idempotent (adds the verification columns if missing)
    try:
        pending = _resolve_scope(conn, db_path, args)
        if not pending:
            return 0

        mode = (f"netns {args.netns} (tunnel mode)" if args.netns
                else f"pool: {', '.join(NORDVPN_COUNTRIES)}")
        which = "recheckable (non-expired)" if args.recheck else "pending"
        print(f"pending: {len(pending)} {which} deals | rotate every {args.rotate_every} books "
              f"| {mode} | jitter {VERIFY_DELAY_MIN:g}-{VERIFY_DELAY_MAX:g}s "
              f"| LLM: {LLM_MODEL or 'off'}")

        # (1) Ensure the VPN egress is up.
        if args.netns:
            # Tunnel mode: this process runs INSIDE the namespace; the host CLI
            # (and its credentials) are unused.
            live, last_rotated_ip = _ensure_tunnel(args.netns)
            if not live:
                print(f"tunnel error: namespace {args.netns!r} has no live egress and a rebuild "
                      f"did not bring it up. Bring the tunnel up first: "
                      f"systemctl start {WISHLIST_VPN_UNIT}")
                return 1
            log.info("tunnel mode: netns %s live (egress ip %s)", args.netns, last_rotated_ip or "?")
        else:
            # Host-CLI mode (dev boxes): the existing nordvpn CLI path.
            try:
                st = nordvpn.status()
            except nordvpn.NordvpnError as exc:
                print(f"nordvpn error: {exc}")
                return 1
            if not st.connected:
                if args.nord_user or args.nord_pass or os.environ.get(nordvpn.ENV_USERNAME):
                    nordvpn.login(args.nord_user, args.nord_pass)
                nordvpn.connect(NORDVPN_START_COUNTRY)
            log.info("tunnel up via %s (ip %s)", st.country or NORDVPN_START_COUNTRY, nordvpn.ip())
            last_rotated_ip = nordvpn.ip()

        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True, args=amazon_price.LAUNCH_ARGS)
        try:
            def read(url, *, fingerprint, asin=None):
                # One context per book: the fingerprint (UA/locale/viewport) is
                # context-level, so each book gets its own; the browser is shared.
                ctx = browser.new_context(
                    user_agent=fingerprint.user_agent,
                    locale=fingerprint.locale,
                    viewport={"width": fingerprint.width, "height": fingerprint.height},
                )
                try:
                    return amazon_price.read_page(ctx.new_page(), url, asin=asin)
                finally:
                    ctx.close()

            # (2..6) Process each book in order.
            summary = {"current": 0, "expired": 0, "unknown": 0, "rotations": 0}
            processed = 0
            last_fp: fp.Fingerprint | None = None
            for deal in pending:
                # Tunnel mode FAIL-CLOSED gate: never read an Amazon page unless
                # the tunnel is verifiably live right now. If it is gone,
                # abort the whole run before touching Amazon — a dead/stale
                # tunnel must never be the reason a page is fetched. Rows
                # already verified are committed and resume via deal_status.
                if args.netns and not _tunnel_live(args.netns):
                    print(f"tunnel error: namespace {args.netns!r} lost live egress after "
                          f"{processed} verified book(s). Stopping before any further Amazon "
                          f"access outside the tunnel — fix it (systemctl restart "
                          f"{WISHLIST_VPN_UNIT}) and re-run; verified rows are kept.")
                    return 1

                if last_fp is None:
                    f = fp.next_fingerprint()
                else:
                    f = fp.fresh_fingerprint(last_fp)
                last_fp = f

                result = _read_with_retries(read, deal, f, summary)
                status, _cents = deals_db.classify_deal(deal["deal_price"], result.price_text)
                current_price = result.price_text if status != "unknown" else None
                deals_db.mark_verified(
                    conn, deal["id"], status=status, current_price=current_price, at=_now()
                )
                conn.commit()
                summary[status] += 1
                processed += 1
                cur = result.price_text if result.price_cents is not None else "?"
                print(f"[{processed}/{len(pending)}] id={deal['id']} {deal['asin']} "
                      f"deal={deal['deal_price']} current={cur} -> {status} "
                      f"(via {result.source or 'unreadable'}) {deal['title'][:60]}")
                _write_progress(
                    {
                        "key": [str(db_path), args.limit],
                        "db": str(db_path),
                        "scope": [d["id"] for d in pending],
                        "processed": processed,
                        "summary": summary,
                        "last_rotated_ip": last_rotated_ip,
                        "at": _now(),
                    }
                )

                # (4) Refresh the exit IP + fingerprint every N books.
                if processed % args.rotate_every == 0:
                    if args.netns:
                        # Tunnel mode: rebuild the tunnel for a fresh session/exit IP.
                        # Best-effort — a not-permitted rebuild (no root) or a failed
                        # one leaves the stable IP; the fresh fingerprint still changes
                        # and the run continues (recorded assumption, see docstring).
                        new_ip = nordvpn.tunnel_rotate(args.netns)
                        if new_ip is None:
                            log.warning("tunnel rotate not permitted/failed; continuing on the "
                                        "current IP (fingerprint-only rotation)")
                        else:
                            last_rotated_ip = new_ip
                            summary["rotations"] += 1
                            print(f"-- rebuilt tunnel, egress IP -> {new_ip} "
                                  f"(rotation #{summary['rotations']}); fresh fingerprint")
                    else:
                        try:
                            new_ip = nordvpn.rotate()
                        except nordvpn.NordvpnError as exc:
                            log.warning("rotate failed (%s); continuing on the current IP", exc)
                        else:
                            last_rotated_ip = new_ip
                            summary["rotations"] += 1
                            print(f"-- rotated exit IP -> {new_ip} "
                                  f"(rotation #{summary['rotations']}); fresh fingerprint")

                # (5) Pacing jitter between books (not after the last one).
                if processed < len(pending):
                    time.sleep(random.uniform(VERIFY_DELAY_MIN, VERIFY_DELAY_MAX))

            # (6) Final summary.
            print(
                f"done: {summary['current']} current, {summary['expired']} expired, "
                f"{summary['unknown']} unknown | {summary['rotations']} IP rotations"
            )
            _write_progress(
                {
                    "key": [str(db_path), args.limit],
                    "db": str(db_path),
                    "scope": [d["id"] for d in pending],
                    "processed": processed,
                    "summary": summary,
                    "last_rotated_ip": last_rotated_ip,
                    "at": _now(),
                }
            )
            return 0
        finally:
            try:
                browser.close()
            finally:
                pw.stop()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verify stored BookBub deals against current Amazon prices "
        "(NordVPN tunnel + fingerprint rotation)."
    )
    ap.add_argument("--limit", type=int, default=None,
                    help="Verify at most N pending deals (default: all pending).")
    ap.add_argument("--rotate-every", type=int, default=NORDVPN_ROTATE_EVERY,
                    help=f"Rotate the NordVPN exit IP + fingerprint every N books "
                         f"(default {NORDVPN_ROTATE_EVERY}, from NORDVPN_ROTATE_EVERY).")
    ap.add_argument("--db", default=str(DEALS_DB),
                    help=f"Deals DB path (default: {DEALS_DB}).")
    ap.add_argument("--netns", default="", metavar="NS",
                    help="Tunnel mode: run INSIDE the given network namespace "
                         "(e.g. 'wlvpn', via amazon-wishlist-verify.service or "
                         "scripts/vpn_verify.sh). Empty (default) = host-CLI mode, "
                         "where the nordvpn CLI is connected/rotated directly.")
    ap.add_argument("--nord-user", default=None,
                    help=f"Username (default: {nordvpn.ENV_USERNAME} env).")
    ap.add_argument("--nord-pass", default=None,
                    help=f"Password (default: {nordvpn.ENV_PASSWORD} env).")
    ap.add_argument("--check", action="store_true",
                    help="Dry run: print the pending count + config, connect nothing, exit 0.")
    ap.add_argument("--fresh", action="store_true",
                    help="Start a new run scope, ignoring a saved one for the same --db/--limit.")
    ap.add_argument("--recheck", action="store_true",
                    help="Re-verify deals that are still 'current' or 'unknown' (plus the "
                         "unchecked ones) instead of only the never-checked deals; 'expired' "
                         "is terminal and is never re-checked. Used by the daily updater "
                         "(scripts/bookbub_daily.py).")
    args = ap.parse_args()
    if args.rotate_every < 1:
        ap.error("--rotate-every must be >= 1")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.check:
        conn = deals_db.connect(args.db)
        try:
            n = len(_selection(args)(conn, limit=args.limit))
        finally:
            conn.close()
        which = "recheckable (non-expired)" if args.recheck else "pending"
        print(f"check: {n} {which} deal(s) with an ASIN in {args.db}")
        print(f"check: rotate every {args.rotate_every} | start country {NORDVPN_START_COUNTRY} "
              f"| pool {', '.join(NORDVPN_COUNTRIES)} | jitter {VERIFY_DELAY_MIN:g}-{VERIFY_DELAY_MAX:g}s "
              f"| retries {VERIFY_MAX_RETRIES} (backoff {VERIFY_RETRY_BACKOFF:g}s) "
              f"| LLM: {LLM_MODEL or 'off'} | CLI present: {nordvpn.cli_present()}")
        return 0

    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
