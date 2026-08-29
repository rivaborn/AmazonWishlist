from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config, deals_db, services, settings, sync
from ..pagination import DEFAULT_PER_PAGE, PER_PAGE_MAX, PER_PAGE_MIN, paginate

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _ctx(extra: dict) -> dict:
    """Role context every template needs, since they all extend base.html.

    `readonly` drives more than cosmetics: the purchased checkboxes are wired to
    a POST that a mirror answers with 403, and the shared handler in base.html
    reacts to a failure by silently un-ticking the box again. On a mirror the
    control must not render at all.
    """
    return {
        "role": config.ROLE,
        "readonly": config.is_secondary(),
        "primary_url": config.PRIMARY_URL,
        **extra,
    }


def _basis(value: str) -> str:
    return "list" if value == "list" else "prev"


def _bookbub_sort(value: str) -> str:
    return "price" if value == "price" else "date"


def _bookbub_dir(value: str) -> str:
    return "asc" if value == "asc" else "desc"


def _bookbub_per_page(value: int) -> int:
    """Snap an incoming per_page to the nearest allowed BookBub option.

    The tab offers a fixed dropdown (BOOKBUB_PER_PAGE_OPTIONS, default
    BOOKBUB_PER_PAGE_DEFAULT) instead of the shared 10..500 clamp the other
    pages use.
    """
    return min(config.BOOKBUB_PER_PAGE_OPTIONS, key=lambda o: abs(o - value))


def _per_page(value: int) -> int:
    return max(PER_PAGE_MIN, min(PER_PAGE_MAX, value))


@router.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/deals")


@router.get("/deals")
def deals_page(
    request: Request,
    min_dollar: float = 0.0,
    min_pct: float = 0.0,
    basis: str = "prev",
    page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE),
):
    b = _basis(basis)
    rows = services.deals(min_dollar, min_pct, b)  # type: ignore[arg-type]
    pagination = paginate(
        rows,
        page=page,
        per_page=_per_page(per_page),
        base_url="/deals",
        extra_query={"min_dollar": min_dollar, "min_pct": min_pct, "basis": b},
    )
    return templates.TemplateResponse(
        request,
        "deals.html",
        _ctx({
            "rows": pagination["rows"],
            "pagination": pagination,
            "min_dollar": min_dollar,
            "min_pct": min_pct,
            "basis": b,
            "active": "deals",
        }),
    )


@router.get("/bookbub-deals")
def bookbub_deals_page(
    request: Request,
    sort: str = Query("date", alias="sort"),
    direction: str = Query("desc", alias="dir"),
    show_hidden: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(config.BOOKBUB_PER_PAGE_DEFAULT),
):
    """Live BookBub deals (data/deals.db, deal_status='current').

    Read-only on both instances: the web app never mutates deals.db, and on a
    mirror the DB (plus its cover images) is mirrored from the primary by the
    daily sync (GET /api/sync/deals), so the tab duplicates the primary's
    page. The tab shows only verified live deals (expired/unknown/unchecked
    rows are filtered in the query, see deals_db.current_deals). Sortable by
    Deal price or Date of deal via `?sort=price|date` + `?dir=asc|desc` (both
    whitelisted, default date-desc = most recent first); sorting the full list
    before pagination keeps every page consistently ordered and the
    extra_query carries the active sort into every page link.
    `?show_hidden=1` also reveals rows the user has hidden (hidden rows are
    excluded by default). Each row shows the captured book cover (served from
    the local covers dir at /covers/<name>) by the title and the captured
    Amazon description as a hover tooltip. Page size comes from the per-page
    dropdown (BOOKBUB_PER_PAGE_OPTIONS, default 20) and is preserved across
    pagination and sort links via `?per_page=`.
    """
    s = _bookbub_sort(sort)
    d = _bookbub_dir(direction)
    pp = _bookbub_per_page(per_page)
    conn = deals_db.connect(config.DEALS_DB)
    try:
        deals_db.ensure_schema(conn)  # idempotent (adds verification cols if missing)
        rows = deals_db.sort_deals(
            deals_db.current_deals(conn, show_hidden=show_hidden), sort=s, direction=d
        )
    finally:
        conn.close()
    pagination = paginate(
        rows,
        page=page,
        per_page=pp,
        base_url="/bookbub-deals",
        extra_query={"sort": s, "dir": d, "show_hidden": show_hidden, "per_page": pp},
    )
    return templates.TemplateResponse(
        request,
        "bookbub_deals.html",
        _ctx(
            {
                "rows": pagination["rows"],
                "pagination": pagination,
                "sort": s,
                "dir": d,
                "show_hidden": show_hidden,
                "per_page": pp,
                "per_page_options": config.BOOKBUB_PER_PAGE_OPTIONS,
                "active": "bookbub",
            }
        ),
    )


@router.get("/covers/{name}")
def deal_cover(name: str):
    """Serve a captured book cover from the local covers dir.

    ``name`` is the bare filename stored in the deal row's ``cover`` column
    (``<ASIN>.<ext>``). The basename check plus the resolved-path containment
    check keep the response inside the covers dir (no path traversal); anything
    else 404s.
    """
    safe = Path(name).name
    if safe != name:
        raise HTTPException(404, "not found")
    covers_dir = Path(config.DEALS_COVERS_DIR).resolve()
    path = (covers_dir / safe).resolve()
    if not path.is_file() or covers_dir not in path.parents:
        raise HTTPException(404, "not found")
    return FileResponse(path)


@router.get("/settings")
def settings_page(request: Request):
    """App settings (primary only): daily schedule times + BookBub cover size.

    A read-only mirror never sees this tab (the nav link is hidden, and this
    403s here). Values live in the `settings` table (app.settings) and override
    the env/config defaults at the point of use (the scheduler's daily times,
    the BookBub Deals tab's default cover size); mutations go through
    POST /api/settings (primary-only). Times are server-local HH:MM.
    """
    if config.is_secondary():
        raise HTTPException(
            403, "settings are edited on the primary; this mirror is read-only"
        )
    scrape_h = settings.get_int("scrape_hour", config.SCRAPE_HOUR)
    scrape_m = settings.get_int("scrape_minute", config.SCRAPE_MINUTE)
    bookbub_h = settings.get_int("bookbub_hour", config.BOOKBUB_HOUR_DEFAULT)
    bookbub_m = settings.get_int("bookbub_minute", config.BOOKBUB_MINUTE_DEFAULT)
    cover_size = settings.get("cover_size", config.BOOKBUB_COVER_SIZE_DEFAULT)
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(
            {
                "scrape_time": f"{scrape_h:02d}:{scrape_m:02d}",
                "bookbub_time": f"{bookbub_h:02d}:{bookbub_m:02d}",
                "cover_size": cover_size,
                "cover_size_options": config.BOOKBUB_COVER_SIZE_OPTIONS,
                "active": "settings",
            }
        ),
    )


@router.get("/books")
def books_page(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE),
):
    rows, summary = services.all_books_by_price()
    pagination = paginate(
        rows, page=page, per_page=_per_page(per_page), base_url="/books"
    )
    return templates.TemplateResponse(
        request,
        "books.html",
        _ctx({
            "rows": pagination["rows"],
            "summary": summary,
            "pagination": pagination,
            "active": "books",
        }),
    )


@router.get("/no-price")
def no_price_page(
    request: Request,
    kindle_page: int = Query(1, ge=1),
    p404_page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE),
):
    groups = services.no_price_books()
    pp = _per_page(per_page)
    kindle_pagination = paginate(
        groups.get("kindle_unavailable", []),
        page=kindle_page,
        per_page=pp,
        base_url="/no-price",
        extra_query={"p404_page": p404_page},
        page_param="kindle_page",
    )
    p404_pagination = paginate(
        groups.get("page_404", []),
        page=p404_page,
        per_page=pp,
        base_url="/no-price",
        extra_query={"kindle_page": kindle_page},
        page_param="p404_page",
    )
    return templates.TemplateResponse(
        request,
        "no_price.html",
        _ctx({
            "kindle_unavailable": kindle_pagination["rows"],
            "kindle_pagination": kindle_pagination,
            "page_404": p404_pagination["rows"],
            "p404_pagination": p404_pagination,
            "active": "no_price",
        }),
    )


@router.get("/price-drops")
def price_drops_page(
    request: Request,
    min_dollar: float = 0.0,
    min_pct: float = 0.0,
    basis: str = "prev",
    page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE),
):
    b = _basis(basis)
    rows = services.price_drop_history(min_dollar, min_pct, b)  # type: ignore[arg-type]
    pagination = paginate(
        rows,
        page=page,
        per_page=_per_page(per_page),
        base_url="/price-drops",
        extra_query={"min_dollar": min_dollar, "min_pct": min_pct, "basis": b},
    )
    return templates.TemplateResponse(
        request,
        "price_drops.html",
        _ctx({
            "rows": pagination["rows"],
            "pagination": pagination,
            "min_dollar": min_dollar,
            "min_pct": min_pct,
            "basis": b,
            "active": "price_drops",
        }),
    )


@router.get("/purchased")
def purchased_page(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE),
):
    rows = services.purchased_books()
    pagination = paginate(
        rows, page=page, per_page=_per_page(per_page), base_url="/purchased"
    )
    return templates.TemplateResponse(
        request,
        "purchased.html",
        _ctx({
            "rows": pagination["rows"],
            "pagination": pagination,
            "active": "purchased",
        }),
    )


@router.get("/wishlists")
def wishlists_page(request: Request):
    from ..config import (
        INGEST_SHRINK_FLOOR,
        SCRAPE_HOUR,
        SCRAPE_MINUTE,
        SCRAPE_PER_WISHLIST_SECONDS,
        SYNC_HOUR,
        SYNC_MINUTE,
    )

    sync_status = sync.get_sync_status() if config.is_secondary() else None
    return templates.TemplateResponse(
        request,
        "wishlists.html",
        _ctx({
            "wishlists": services.list_wishlists(now=_mirror_now(sync_status)),
            "active": "wishlists",
            "scrape_time": f"{SCRAPE_HOUR:02d}:{SCRAPE_MINUTE:02d}",
            "per_list_seconds": SCRAPE_PER_WISHLIST_SECONDS,
            "shrink_floor": INGEST_SHRINK_FLOOR,
            "sync": sync_status,
            "sync_time": f"{SYNC_HOUR:02d}:{SYNC_MINUTE:02d}",
        }),
    )


def _mirror_now(sync_status: Optional[dict]) -> Optional[datetime]:
    """The primary's clock, advanced by however long ago we last synced.

    Every timestamp in a mirrored row was written by `services._now()` on the
    primary — naive server-LOCAL time. Comparing those against this box's clock
    is wrong by the timezone offset, and in the direction where we are behind
    the primary the computed age goes negative, so `stale` never fires and the
    only honest health column on this page silently switches itself off.

    Returns None (i.e. "use local now") on a primary or when we have never
    completed a sync, which is the correct behaviour in both cases.
    """
    if not sync_status:
        return None
    source_now = sync_status.get("source_now")
    synced_at = sync_status.get("synced_at_local")
    if not source_now or not synced_at:
        return None
    try:
        return datetime.fromisoformat(source_now) + (
            datetime.now() - datetime.fromisoformat(synced_at)
        )
    except (TypeError, ValueError):
        return None
