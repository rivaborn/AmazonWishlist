from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import services
from ..pagination import DEFAULT_PER_PAGE, PER_PAGE_MAX, PER_PAGE_MIN, paginate

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _basis(value: str) -> str:
    return "list" if value == "list" else "prev"


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
        {
            "rows": pagination["rows"],
            "pagination": pagination,
            "min_dollar": min_dollar,
            "min_pct": min_pct,
            "basis": b,
            "active": "deals",
        },
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
        {
            "rows": pagination["rows"],
            "summary": summary,
            "pagination": pagination,
            "active": "books",
        },
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
        {
            "kindle_unavailable": kindle_pagination["rows"],
            "kindle_pagination": kindle_pagination,
            "page_404": p404_pagination["rows"],
            "p404_pagination": p404_pagination,
            "active": "no_price",
        },
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
        {
            "rows": pagination["rows"],
            "pagination": pagination,
            "min_dollar": min_dollar,
            "min_pct": min_pct,
            "basis": b,
            "active": "price_drops",
        },
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
        {
            "rows": pagination["rows"],
            "pagination": pagination,
            "active": "purchased",
        },
    )


@router.get("/wishlists")
def wishlists_page(request: Request):
    from ..config import SCRAPE_HOUR, SCRAPE_MINUTE, SCRAPE_PER_WISHLIST_SECONDS
    return templates.TemplateResponse(
        request,
        "wishlists.html",
        {
            "wishlists": services.list_wishlists(),
            "active": "wishlists",
            "scrape_time": f"{SCRAPE_HOUR:02d}:{SCRAPE_MINUTE:02d}",
            "per_list_seconds": SCRAPE_PER_WISHLIST_SECONDS,
        },
    )
