from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

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


def _bookbub_per_page(value: int) -> int:
    """Snap an incoming per_page to the nearest allowed BookBub option.

    The tab offers a fixed dropdown (BOOKBUB_PER_PAGE_OPTIONS, default
    BOOKBUB_PER_PAGE_DEFAULT) instead of the shared 10..500 clamp the other
    pages use.
    """
    return min(config.BOOKBUB_PER_PAGE_OPTIONS, key=lambda o: abs(o - value))


def _bookbub_min_stars(value: float) -> float:
    """Snap an incoming min_stars to the nearest allowed BookBub threshold.
    """
    return float(min(config.BOOKBUB_MIN_STARS_OPTIONS,
                     key=lambda o: abs(o - value)))


def _per_page(value: int) -> int:
    return max(PER_PAGE_MIN, min(PER_PAGE_MAX, value))


# ---------- clickable sort headings ----------

# Each page lists the columns it makes sortable as {column: default direction}.
# The first entry is that page's default sort; ``_sort_dir`` falls back to it
# (with its default direction) when the incoming sort/dir is invalid. Clicking
# the active column toggles dir; clicking another column switches to it with its
# default direction.
DEALS_SORT_DEFAULTS = {
    "drop_pct": "desc", "drop_dollar": "desc", "price": "asc",
    "base": "asc", "highest": "asc", "title": "asc", "author": "asc",
    "seen": "desc",
}
DROPS_SORT_DEFAULTS = {
    "seen": "desc", "drop_pct": "desc", "drop_dollar": "desc",
    "price": "asc", "base": "asc", "title": "asc", "author": "asc",
}
BOOKS_SORT_DEFAULTS = {
    "price": "asc", "title": "asc", "author": "asc", "list": "asc",
    "highest": "asc", "seen": "desc",
}
PURCHASED_SORT_DEFAULTS = {
    "seen": "desc", "title": "asc", "author": "asc", "price": "asc",
    "list": "asc",
}
NOPRICE_SORT_DEFAULTS = {
    "title": "asc", "author": "asc", "seen": "desc",
}
# BookBub Deals tab: all its columns sortable, not just price/date.
BOOKBUB_SORT_DEFAULTS = {
    "date": "desc", "title": "asc", "author": "asc", "price": "asc",
    "original": "asc", "stars": "desc",
}


def _sort_dir(sort: str, direction: str, defaults: dict) -> tuple[str, str]:
    """Normalise an incoming sort/dir against a page's allowed columns.

    An unknown sort falls back to the page default (the first key of
    ``defaults``); an unknown direction falls back to that column's default.
    """
    if sort not in defaults:
        sort = next(iter(defaults))
    if direction not in ("asc", "desc"):
        direction = defaults[sort]
    return sort, direction


def _sort_context(base_url: str, extra: dict, sort: str, direction: str, defaults: dict) -> dict:
    """Build the {column: href} map the templates use for clickable headers.

    Every header links to ``{base_url}?sort=<col>&dir=<next><extra>``, where
    ``<next>`` toggles the current direction when ``<col>`` is the active sort
    and otherwise is that column's default. ``extra`` carries the non-sort
    query params (filters, per_page) so a sort keeps them. No page param is
    included, so sorting always starts back at page 1.
    """
    links: dict[str, str] = {}
    for col, default_dir in defaults.items():
        next_dir = ("desc" if direction == "asc" else "asc") if sort == col else default_dir
        q = dict(extra)
        q["sort"] = col
        q["dir"] = next_dir
        links[col] = f"{base_url}?{urlencode(q)}"
    return {"sort": sort, "dir": direction, "sort_links": links}


@router.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/deals")


@router.get("/deals")
def deals_page(
    request: Request,
    min_dollar: float = 0.0,
    min_pct: float = 0.0,
    basis: str = "prev",
    sort: str = "drop_pct",
    dir: str = "desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE),
):
    b = _basis(basis)
    rows = services.deals(min_dollar, min_pct, b)  # type: ignore[arg-type]
    s, d = _sort_dir(sort, dir, DEALS_SORT_DEFAULTS)
    rows = services.sort_book_rows(rows, s, d, b)
    extra = {"min_dollar": min_dollar, "min_pct": min_pct, "basis": b,
             "per_page": _per_page(per_page)}
    pagination = paginate(
        rows,
        page=page,
        per_page=_per_page(per_page),
        base_url="/deals",
        extra_query=extra,
    )
    sc = _sort_context("/deals", extra, s, d, DEALS_SORT_DEFAULTS)
    return templates.TemplateResponse(
        request,
        "deals.html",
        _ctx({
            "rows": pagination["rows"],
            "pagination": pagination,
            "min_dollar": min_dollar,
            "min_pct": min_pct,
            "basis": b,
            "sort": sc["sort"],
            "dir": sc["dir"],
            "sort_links": sc["sort_links"],
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
    min_stars: float = Query(config.BOOKBUB_MIN_STARS_DEFAULT),
):
    """Live BookBub deals (data/deals.db, deal_status='current').

    Read-only on both instances: the web app never mutates deals.db, and on a
    mirror the DB (plus its cover images) is mirrored from the primary by the
    daily sync (GET /api/sync/deals), so the tab duplicates the primary's
    page. The tab shows only verified live deals (expired/unknown/unchecked
    rows are filtered in the query, see deals_db.current_deals). Every column
    is a clickable sort heading via `?sort=title|author|price|original|date|stars`
    + `?dir=asc|desc` (all whitelisted, default date-desc = most recent first);
    clicking the active heading toggles its direction, and the other headings
    switch to it with its own default. Sorting the full list
    before pagination keeps every page consistently ordered and the
    extra_query carries the active sort into every page link.
    `?show_hidden=1` also reveals rows the user has hidden (hidden rows are
    excluded by default). A hide is stored per BOOK (deals_db.hidden_book), so
    it also covers the new row a later BookBub re-feature of the same book
    creates -- the per-row flag it replaced was erased by the nightly dedup. Each row shows the captured book cover (served from
    the local covers dir at /covers/<name>) by the title and the captured
    Amazon description as a hover tooltip on BOTH the cover and the title.
    Page size comes from the per-page dropdown (BOOKBUB_PER_PAGE_OPTIONS,
    default 20) and is preserved across pagination and sort links via
    `?per_page=`. `?min_stars=N` filters to deals rated >= N stars (only deals
    whose rating was captured in the daily check; a value >0 drops unrated
    rows). Cover size comes from the stored `cover_size` setting
    (Settings tab / the per-page cover-size dropdown, default
    BOOKBUB_COVER_SIZE_DEFAULT) and is applied as a `size-*` class.
    """
    s, d = _sort_dir(sort, direction, BOOKBUB_SORT_DEFAULTS)
    pp = _bookbub_per_page(per_page)
    ms = _bookbub_min_stars(min_stars)
    cover_size = settings.get("cover_size", config.BOOKBUB_COVER_SIZE_DEFAULT)
    tooltip_size = settings.get("tooltip_size", config.BOOKBUB_TOOLTIP_SIZE_DEFAULT)
    conn = deals_db.connect(config.DEALS_DB)
    try:
        deals_db.ensure_schema(conn)  # idempotent (adds verification cols if missing)
        rows = deals_db.sort_deals(
            deals_db.current_deals(conn, show_hidden=show_hidden, min_stars=ms),
            sort=s, direction=d,
        )
    finally:
        conn.close()
    extra = {"show_hidden": show_hidden, "per_page": pp, "min_stars": ms}
    pagination = paginate(
        rows,
        page=page,
        per_page=pp,
        base_url="/bookbub-deals",
        extra_query={**extra, "sort": s, "dir": d},
    )
    sc = _sort_context("/bookbub-deals", extra, s, d, BOOKBUB_SORT_DEFAULTS)
    return templates.TemplateResponse(
        request,
        "bookbub_deals.html",
        _ctx(
            {
                "rows": pagination["rows"],
                "pagination": pagination,
                "sort": sc["sort"],
                "dir": sc["dir"],
                "sort_links": sc["sort_links"],
                "show_hidden": show_hidden,
                "per_page": pp,
                "per_page_options": config.BOOKBUB_PER_PAGE_OPTIONS,
                "min_stars": ms,
                "min_stars_options": config.BOOKBUB_MIN_STARS_OPTIONS,
                "cover_size": cover_size,
                "cover_size_options": config.BOOKBUB_COVER_SIZE_OPTIONS,
                "cover_size_default": config.BOOKBUB_COVER_SIZE_DEFAULT,
                "tooltip_size": tooltip_size,
                "tooltip_size_options": config.BOOKBUB_TOOLTIP_SIZE_OPTIONS,
                "tooltip_size_default": config.BOOKBUB_TOOLTIP_SIZE_DEFAULT,
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
    tooltip_size = settings.get("tooltip_size", config.BOOKBUB_TOOLTIP_SIZE_DEFAULT)
    from .. import owned_update
    owned_status = owned_update.owned_update_status()
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(
            {
                "scrape_time": f"{scrape_h:02d}:{scrape_m:02d}",
                "bookbub_time": f"{bookbub_h:02d}:{bookbub_m:02d}",
                "cover_size": cover_size,
                "cover_size_options": config.BOOKBUB_COVER_SIZE_OPTIONS,
                "tooltip_size": tooltip_size,
                "tooltip_size_options": config.BOOKBUB_TOOLTIP_SIZE_OPTIONS,
                "owned_status": owned_status,
                "active": "settings",
            }
        ),
    )


@router.get("/books")
def books_page(
    request: Request,
    sort: str = "price",
    dir: str = "asc",
    page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE),
):
    rows, summary = services.all_books_by_price()
    s, d = _sort_dir(sort, dir, BOOKS_SORT_DEFAULTS)
    rows = services.sort_book_rows(rows, s, d)
    extra = {"per_page": _per_page(per_page)}
    pagination = paginate(
        rows, page=page, per_page=_per_page(per_page), base_url="/books",
        extra_query=extra,
    )
    sc = _sort_context("/books", extra, s, d, BOOKS_SORT_DEFAULTS)
    return templates.TemplateResponse(
        request,
        "books.html",
        _ctx({
            "rows": pagination["rows"],
            "summary": summary,
            "pagination": pagination,
            "sort": sc["sort"],
            "dir": sc["dir"],
            "sort_links": sc["sort_links"],
            "active": "books",
        }),
    )


@router.get("/no-price")
def no_price_page(
    request: Request,
    sort: str = "title",
    dir: str = "asc",
    kindle_page: int = Query(1, ge=1),
    p404_page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE),
):
    groups = services.no_price_books()
    s, d = _sort_dir(sort, dir, NOPRICE_SORT_DEFAULTS)
    pp = _per_page(per_page)
    sc = _sort_context("/no-price", {"per_page": pp}, s, d, NOPRICE_SORT_DEFAULTS)
    kindle = services.sort_book_rows(groups.get("kindle_unavailable", []), s, d)
    p404 = services.sort_book_rows(groups.get("page_404", []), s, d)
    kindle_pagination = paginate(
        kindle,
        page=kindle_page,
        per_page=pp,
        base_url="/no-price",
        extra_query={"p404_page": p404_page, "per_page": pp, "sort": s, "dir": d},
        page_param="kindle_page",
    )
    p404_pagination = paginate(
        p404,
        page=p404_page,
        per_page=pp,
        base_url="/no-price",
        extra_query={"kindle_page": kindle_page, "per_page": pp, "sort": s, "dir": d},
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
            "sort": sc["sort"],
            "dir": sc["dir"],
            "sort_links": sc["sort_links"],
            "active": "no_price",
        }),
    )


@router.get("/price-drops")
def price_drops_page(
    request: Request,
    min_dollar: float = 0.0,
    min_pct: float = 0.0,
    basis: str = "prev",
    sort: str = "seen",
    dir: str = "desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE),
):
    b = _basis(basis)
    rows = services.price_drop_history(min_dollar, min_pct, b)  # type: ignore[arg-type]
    s, d = _sort_dir(sort, dir, DROPS_SORT_DEFAULTS)
    rows = services.sort_book_rows(rows, s, d, b)
    extra = {"min_dollar": min_dollar, "min_pct": min_pct, "basis": b,
             "per_page": _per_page(per_page)}
    pagination = paginate(
        rows,
        page=page,
        per_page=_per_page(per_page),
        base_url="/price-drops",
        extra_query=extra,
    )
    sc = _sort_context("/price-drops", extra, s, d, DROPS_SORT_DEFAULTS)
    return templates.TemplateResponse(
        request,
        "price_drops.html",
        _ctx({
            "rows": pagination["rows"],
            "pagination": pagination,
            "min_dollar": min_dollar,
            "min_pct": min_pct,
            "basis": b,
            "sort": sc["sort"],
            "dir": sc["dir"],
            "sort_links": sc["sort_links"],
            "active": "price_drops",
        }),
    )


@router.get("/purchased")
def purchased_page(
    request: Request,
    sort: str = "seen",
    dir: str = "desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE),
):
    rows = services.purchased_books()
    s, d = _sort_dir(sort, dir, PURCHASED_SORT_DEFAULTS)
    rows = services.sort_book_rows(rows, s, d)
    extra = {"per_page": _per_page(per_page)}
    pagination = paginate(
        rows, page=page, per_page=_per_page(per_page), base_url="/purchased",
        extra_query=extra,
    )
    sc = _sort_context("/purchased", extra, s, d, PURCHASED_SORT_DEFAULTS)
    return templates.TemplateResponse(
        request,
        "purchased.html",
        _ctx({
            "rows": pagination["rows"],
            "pagination": pagination,
            "sort": sc["sort"],
            "dir": sc["dir"],
            "sort_links": sc["sort_links"],
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
