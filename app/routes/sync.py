"""Mirror-mode routes.

Export endpoints (served by the primary, consumed by the secondary):
  GET  /api/sync/catalog    → wishlists + books + membership + max_snapshot_id
  GET  /api/sync/snapshots  → one ascending page of the price_snapshot log
  GET  /api/sync/deals      → the whole BookBub deals table + covers (base64)

Local endpoints (about THIS instance's own mirroring):
  GET  /api/sync/status     → last sync, watermark, error
  POST /api/sync/run        → trigger a pull now (secondary only)

There is no auth here, matching the rest of the app — which already binds
0.0.0.0 and serves every page unauthenticated. These two endpoints do hand out
the entire database, so port 9060 must be firewalled to the peer / VPN subnet.
"""

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from .. import config, sync, sync_client

router = APIRouter(prefix="/api/sync")
_executor = ThreadPoolExecutor(max_workers=1)


@router.get("/catalog")
def sync_catalog():
    return sync.export_catalog()


@router.get("/snapshots")
def sync_snapshots(
    since_id: int = Query(0, ge=0),
    max_id: int = Query(..., ge=0),
    limit: int = Query(None),
):
    return sync.export_snapshots(since_id, max_id, limit)


@router.get("/deals")
def sync_deals():
    """The primary's whole BookBub deals DB + covers (the mirror pull)."""
    return sync.export_deals()


@router.get("/status")
def sync_status():
    return sync.get_sync_status()


@router.post("/run")
def sync_run_now():
    if not config.is_secondary():
        raise HTTPException(403, "this instance is a primary; nothing to sync from")
    _executor.submit(sync_client.run_sync)
    return JSONResponse({"started": True, "status": sync.get_sync_status()}, 202)
