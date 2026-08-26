"""Secondary side of mirror mode: pull the primary's data over HTTP.

Order is fixed by the foreign keys and by what the read queries need:

    catalog (wishlist -> book -> wishlist_book)  then  snapshots, ascending

A catalog that is refused or fails ABORTS the run -- fetching snapshots for
ASINs whose `book` rows were never applied just takes a foreign-key error
mid-page. Because pages apply in strict ascending id order, an interrupted sync
always leaves a contiguous PREFIX of the primary's snapshot history, never a set
with holes punched in it. That matters: both `_LATEST_BASE`'s `prev` CTE and
`price_drop_history` derive their baseline from "the previous observed_at", so a
hole would fabricate a price drop. A truncated tail merely shows older prices,
and the next sync fills it in from MAX(id).
"""

from __future__ import annotations

import logging
import threading

import httpx

from . import config, sync

log = logging.getLogger(__name__)

# Non-blocking: an overlapping sync is skipped, not queued. The scheduler's
# interval job and a manual POST /api/sync/run can otherwise collide.
_run_lock = threading.Lock()


def run_sync() -> dict:
    """Pull everything new from the primary. Returns a result summary."""
    if not config.is_secondary():
        log.warning("run_sync() called on a %s instance — ignoring", config.ROLE)
        return {"ok": False, "error": "not a secondary"}
    if not config.PRIMARY_URL:
        log.warning("WISHLIST_ROLE=secondary but WISHLIST_PRIMARY_URL is unset")
        sync.update_sync_state(last_error="WISHLIST_PRIMARY_URL is not set")
        return {"ok": False, "error": "WISHLIST_PRIMARY_URL is not set"}

    if not _run_lock.acquire(blocking=False):
        log.info("Sync already running; skipping this trigger")
        return {"ok": False, "error": "already running"}

    started = sync._now()
    sync.update_sync_state(running=True, last_attempt_at=started, last_error=None)
    try:
        result = _pull()
        sync.update_sync_state(
            running=False,
            last_success_at=sync._now(),
            synced_at_local=sync._now(),
            last_error=None,
            snapshots_pulled=result["snapshots"],
            watermark=sync.local_watermark(),
        )
        log.info(
            "Sync complete: %s wishlists, %s books, %s new snapshots (watermark %s)",
            result["wishlists"],
            result["books"],
            result["snapshots"],
            sync.local_watermark(),
        )
        return {"ok": True, **result}
    except sync.SyncRefused as e:
        log.warning("Sync refused: %s", e)
        sync.update_sync_state(running=False, last_error=str(e))
        return {"ok": False, "error": str(e)}
    except Exception as e:  # network, HTTP, malformed payload
        log.exception("Sync failed")
        sync.update_sync_state(running=False, last_error=f"{type(e).__name__}: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        _run_lock.release()


def _pull() -> dict:
    timeout = httpx.Timeout(config.SYNC_TIMEOUT, connect=10.0)
    with httpx.Client(
        base_url=config.PRIMARY_URL, timeout=timeout, follow_redirects=True
    ) as client:
        r = client.get("/api/sync/catalog")
        r.raise_for_status()
        catalog = r.json()

        applied = sync.apply_catalog(catalog)  # raises SyncRefused -> aborts the run
        max_id = applied["max_snapshot_id"]

        pulled = 0
        since = sync.local_watermark()
        # The cap is the catalog's max, so the window is exactly the one the
        # catalog is consistent with; rows the primary adds mid-sync are simply
        # next run's work.
        while since < max_id:
            r = client.get(
                "/api/sync/snapshots",
                params={
                    "since_id": since,
                    "max_id": max_id,
                    "limit": config.SYNC_PAGE_LIMIT,
                },
            )
            r.raise_for_status()
            page = r.json()
            if not page.get("count"):
                break
            pulled += sync.apply_snapshots(page)
            since = page["next_since_id"]
            sync.update_sync_state(snapshots_pulled=pulled, watermark=since)
            if not page.get("has_more"):
                break

    return {
        "wishlists": applied["wishlists"],
        "books": applied["books"],
        "memberships": applied["memberships"],
        "snapshots": pulled,
    }
