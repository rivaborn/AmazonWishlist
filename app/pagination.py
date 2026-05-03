"""Pagination helpers shared by the page routes."""

from __future__ import annotations

from math import ceil
from typing import Sequence
from urllib.parse import urlencode

DEFAULT_PER_PAGE = 100
PER_PAGE_MIN = 10
PER_PAGE_MAX = 500


def clamp_per_page(value: int) -> int:
    return max(PER_PAGE_MIN, min(PER_PAGE_MAX, value))


def page_number_window(page: int, total_pages: int) -> list:
    """Return the list to render between Prev and Next.

    Short lists: [1, 2, ..., total_pages]. Long lists: a window with '…' gaps.
    Sentinel for a gap is the literal string '…' so templates can detect it.
    """
    if total_pages <= 1:
        return [1] if total_pages == 1 else []
    if total_pages <= 9:
        return list(range(1, total_pages + 1))

    pages: list = [1]
    window_start = max(2, page - 2)
    window_end = min(total_pages - 1, page + 2)
    if window_start > 2:
        pages.append("…")
    for n in range(window_start, window_end + 1):
        pages.append(n)
    if window_end < total_pages - 1:
        pages.append("…")
    pages.append(total_pages)
    return pages


def paginate(
    items: Sequence,
    page: int,
    per_page: int,
    base_url: str,
    extra_query: dict | None = None,
    page_param: str = "page",
) -> dict:
    """Slice `items` for the current page and return template context.

    `extra_query` is merged into every pagination link (preserves filters etc).
    `page_param` lets one page render two paginators with separate query keys.
    """
    per_page = clamp_per_page(per_page)
    total = len(items)
    total_pages = max(1, ceil(total / per_page)) if total else 1
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    rows = list(items[offset : offset + per_page])

    extras = dict(extra_query or {})
    # Strip the pagination param itself in case caller passed it through.
    extras.pop(page_param, None)
    qs = ("&" + urlencode(extras, doseq=True)) if extras else ""

    return {
        "rows": rows,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "page_numbers": page_number_window(page, total_pages),
        "base_url": base_url,
        "page_param": page_param,
        "extra_qs": qs,
    }
