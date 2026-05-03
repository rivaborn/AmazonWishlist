from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import services

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _basis(value: str) -> str:
    return "list" if value == "list" else "prev"


@router.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/deals")


@router.get("/deals")
def deals_page(
    request: Request,
    min_dollar: float = 0.0,
    min_pct: float = 0.0,
    basis: str = "prev",
):
    b = _basis(basis)
    rows = services.deals(min_dollar, min_pct, b)  # type: ignore[arg-type]
    return templates.TemplateResponse(
        request,
        "deals.html",
        {
            "rows": rows,
            "min_dollar": min_dollar,
            "min_pct": min_pct,
            "basis": b,
            "active": "deals",
        },
    )


@router.get("/books")
def books_page(request: Request):
    rows, summary = services.all_books_by_price()
    return templates.TemplateResponse(
        request,
        "books.html",
        {
            "rows": rows,
            "summary": summary,
            "active": "books",
        },
    )


@router.get("/no-price")
def no_price_page(request: Request):
    groups = services.no_price_books()
    return templates.TemplateResponse(
        request,
        "no_price.html",
        {
            "kindle_unavailable": groups.get("kindle_unavailable", []),
            "page_404": groups.get("page_404", []),
            "active": "no_price",
        },
    )


@router.get("/price-drops")
def price_drops_page(
    request: Request,
    min_dollar: float = 0.0,
    min_pct: float = 0.0,
    basis: str = "prev",
):
    b = _basis(basis)
    rows = services.price_drop_history(min_dollar, min_pct, b)  # type: ignore[arg-type]
    return templates.TemplateResponse(
        request,
        "price_drops.html",
        {
            "rows": rows,
            "min_dollar": min_dollar,
            "min_pct": min_pct,
            "basis": b,
            "active": "price_drops",
        },
    )


@router.get("/wishlists")
def wishlists_page(request: Request):
    return templates.TemplateResponse(
        request,
        "wishlists.html",
        {
            "wishlists": services.list_wishlists(),
            "active": "wishlists",
        },
    )
