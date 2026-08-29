from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, Body, Depends, Form, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from .. import config, deals_db, services

router = APIRouter(prefix="/api")
_executor = ThreadPoolExecutor(max_workers=1)


def _require_primary() -> None:
    """Every mutating endpoint. A secondary's DB is a mirror: anything written
    here would be silently reverted by the next catalog apply, and a local
    snapshot insert would corrupt the sync watermark outright (see app/sync.py).
    Wishlists and purchased flags are edited on the primary and mirror down."""
    if config.is_secondary():
        raise HTTPException(
            403, "this instance is a read-only mirror; make changes on the primary"
        )


@router.post("/wishlists", dependencies=[Depends(_require_primary)])
def add_wishlist(url: str = Form(...), label: Optional[str] = Form(None)):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    services.add_wishlist(url, label.strip() if label else None)
    return RedirectResponse(url="/wishlists", status_code=303)


@router.post("/wishlists/{wishlist_id}/delete", dependencies=[Depends(_require_primary)])
def delete_wishlist(wishlist_id: int):
    services.remove_wishlist(wishlist_id)
    return RedirectResponse(url="/wishlists", status_code=303)


@router.post("/scrape/run", dependencies=[Depends(_require_primary)])
def run_scrape_now():
    progress = services.get_progress()
    if progress["running"]:
        return JSONResponse({"started": False, "progress": progress}, status_code=200)
    _executor.submit(services.run_full_scrape)
    return JSONResponse(
        {"started": True, "progress": services.get_progress()}, status_code=202
    )


@router.get("/scrape/status")
def scrape_status():
    return services.get_progress()


@router.post("/books/{asin}/purchased", dependencies=[Depends(_require_primary)])
def set_purchased(asin: str, body: dict = Body(...)):
    if "purchased" not in body:
        raise HTTPException(400, "missing 'purchased' field")
    purchased = bool(body["purchased"])
    try:
        services.set_book_purchased(asin, purchased)
    except KeyError:
        raise HTTPException(404, f"unknown asin: {asin}")
    return {"asin": asin, "purchased": purchased}


@router.post("/deals/{row_id}/hidden", dependencies=[Depends(_require_primary)])
def set_deal_hidden(row_id: int, body: dict = Body(...)):
    """Hide/show one BookBub deal row (the BookBub Deals tab's row checkbox).

    The flag lives in deals.db — a per-instance UI preference that the mirror
    never syncs — so, like the purchased toggle, it is primary-only.
    """
    if "hidden" not in body:
        raise HTTPException(400, "missing 'hidden' field")
    hidden = bool(body["hidden"])
    conn = deals_db.connect(config.DEALS_DB)
    try:
        deals_db.ensure_schema(conn)  # idempotent (adds the hidden column if missing)
        ok = deals_db.set_hidden(conn, row_id, hidden)
        conn.commit()
    finally:
        conn.close()
    if not ok:
        raise HTTPException(404, f"unknown deal id: {row_id}")
    return {"id": row_id, "hidden": hidden}
