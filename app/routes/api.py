from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from .. import services

router = APIRouter(prefix="/api")
_executor = ThreadPoolExecutor(max_workers=1)


@router.post("/wishlists")
def add_wishlist(url: str = Form(...), label: Optional[str] = Form(None)):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    services.add_wishlist(url, label.strip() if label else None)
    return RedirectResponse(url="/wishlists", status_code=303)


@router.post("/wishlists/{wishlist_id}/delete")
def delete_wishlist(wishlist_id: int):
    services.remove_wishlist(wishlist_id)
    return RedirectResponse(url="/wishlists", status_code=303)


@router.post("/scrape/run")
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
