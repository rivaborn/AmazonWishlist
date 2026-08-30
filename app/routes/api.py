import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, Body, Depends, Form, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from .. import config, deals_db, services, settings

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


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_hhmm(value: Optional[str], field: str) -> Optional[tuple[int, int]]:
    """'HH:MM' -> (hour, minute), or None when the field was not supplied.

    Blank strings count as not supplied (a settings form may omit a field to
    leave it untouched). Anything supplied but unparseable/out-of-range is a
    400 — a silent default would silently retime a daily job.
    """
    if value is None or not value.strip():
        return None
    m = _TIME_RE.match(value.strip())
    if not m:
        raise HTTPException(400, f"{field} must be HH:MM")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(400, f"{field} must be HH:MM (hour 0-23, minute 0-59)")
    return (hour, minute)


@router.post("/settings")
def save_settings(
    scrape_time: Optional[str] = Form(None),
    bookbub_time: Optional[str] = Form(None),
    cover_size: Optional[str] = Form(None),
    tooltip_size: Optional[str] = Form(None),
):
    """Save app settings (from the Settings tab and the display-size dropdowns).

    Role-aware:

    * PRIMARY — any of the fields may be supplied; only supplied values are
      written. Times are persisted as scrape_hour/scrape_minute and
      bookbub_hour/bookbub_minute (server local) and take effect from the next
      daily run via scheduler.reschedule_jobs(); cover/tooltip sizes apply on
      the next page load.
    * SECONDARY (mirror) — only the LOCAL display preferences
      (``cover_size`` / ``tooltip_size``) may be written: they are stored in
      this instance's own settings table (never mirrored), so a mirror can size
      its covers/tooltips independently of the primary. The run-time fields
      (``scrape_time`` / ``bookbub_time``) are rejected with 403 because a
      mirror never schedules a run, and the Settings page itself stays
      primary-only.

    PRG: the Settings TAB form (which always includes the time fields)
    redirects 303 -> /settings; a display-only POST from the size dropdowns
    returns 200 with no redirect — so fetch() sees an ok response on a mirror
    too (a 303 -> /settings would be followed by fetch() to GET /settings,
    which is 403 on a mirror, making r.ok false and the dropdown never apply).
    """
    secondary = config.is_secondary()
    if secondary and (scrape_time is not None or bookbub_time is not None):
        raise HTTPException(
            403,
            "run-time settings are primary-only; a mirror may only set its local "
            "display preferences (cover_size / tooltip_size)",
        )
    had_times = scrape_time is not None or bookbub_time is not None
    time_changed = False
    if not secondary:
        st = _parse_hhmm(scrape_time, "scrape_time")
        if st is not None:
            settings.set("scrape_hour", st[0])
            settings.set("scrape_minute", st[1])
            time_changed = True
        bt = _parse_hhmm(bookbub_time, "bookbub_time")
        if bt is not None:
            settings.set("bookbub_hour", bt[0])
            settings.set("bookbub_minute", bt[1])
            time_changed = True
    if cover_size is not None and cover_size.strip():
        cs = cover_size.strip()
        if cs not in config.BOOKBUB_COVER_SIZE_OPTIONS:
            raise HTTPException(
                400, f"cover_size must be one of {config.BOOKBUB_COVER_SIZE_OPTIONS}"
            )
        settings.set("cover_size", cs)
    if tooltip_size is not None and tooltip_size.strip():
        ts = tooltip_size.strip()
        if ts not in config.BOOKBUB_TOOLTIP_SIZE_OPTIONS:
            raise HTTPException(
                400, f"tooltip_size must be one of {config.BOOKBUB_TOOLTIP_SIZE_OPTIONS}"
            )
        settings.set("tooltip_size", ts)
    if not secondary and time_changed:
        from .. import scheduler

        scheduler.reschedule_jobs()
    if had_times:
        return RedirectResponse(url="/settings", status_code=303)
    return JSONResponse({"ok": True})
