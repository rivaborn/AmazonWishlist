"""Public Amazon wishlist scraper.

Wishlist pages render server-side with `<li data-itemId="...">` rows under
`#g-items`. They paginate via a `lek` token exposed in the page source as
`wlNextLink` / `showMoreUrl`. We follow it until exhausted.

For items the wishlist view shows without a price, we hit the product page
once to disambiguate `kindle_unavailable` (page exists, no buy button) from
`page_404` (item delisted).
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from .config import (
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    REQUEST_TIMEOUT,
    USER_AGENT,
)
from .models import ScrapedItem

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PRICE_RE = re.compile(r"\$([\d,]+\.\d{2})")
NEXT_TOKEN_RE = re.compile(r'"showMoreUrl"\s*:\s*"([^"]+)"')


def _polite_sleep() -> None:
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def _to_cents(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = PRICE_RE.search(text)
    if not m:
        return None
    return int(round(float(m.group(1).replace(",", "")) * 100))


def _amazon_root(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _get(client: httpx.Client, url: str) -> httpx.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            resp = client.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 503 and attempt == 0:
                _polite_sleep()
                continue
            return resp
        except httpx.HTTPError as e:
            last_exc = e
            _polite_sleep()
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Failed to GET {url}")


def _parse_item_row(row, root: str) -> Optional[ScrapedItem]:
    asin = (
        row.attributes.get("data-itemId")
        or row.attributes.get("data-reposition-action-params", "")
    )
    if asin and asin.startswith("{"):
        m = re.search(r'"itemExternalId"\s*:\s*"ASIN:([^"]+)"', asin)
        asin = m.group(1) if m else None
    if not asin:
        return None

    title_node = row.css_first('a[id^="itemName_"]')
    if not title_node:
        return None
    title = (title_node.attributes.get("title") or title_node.text(strip=True)).strip()
    href = title_node.attributes.get("href", "")
    product_url = urljoin(root, href)

    author = None
    byline = row.css_first('span[id^="item-byline-"]')
    if byline:
        author = byline.text(strip=True).removeprefix("by ").strip() or None

    current_node = row.css_first(".a-price .a-offscreen")
    list_node = row.css_first(".a-text-price .a-offscreen")
    current_cents = _to_cents(current_node.text() if current_node else None)
    list_cents = _to_cents(list_node.text() if list_node else None)

    availability: str = "available"
    if current_cents is None:
        unavailable_node = row.css_first('span[id^="itemAvailability_"]')
        if unavailable_node and "unavailable" in unavailable_node.text().lower():
            availability = "kindle_unavailable"
        else:
            availability = "kindle_unavailable"

    return ScrapedItem(
        asin=asin,
        title=title,
        author=author,
        product_url=product_url,
        current_price_cents=current_cents,
        list_price_cents=list_cents,
        availability=availability,  # type: ignore[arg-type]
    )


def _next_page_url(html: str, root: str) -> Optional[str]:
    m = NEXT_TOKEN_RE.search(html)
    if not m:
        return None
    raw = m.group(1).encode("utf-8").decode("unicode_escape")
    if not raw:
        return None
    return urljoin(root, raw)


def _refine_no_price_item(client: httpx.Client, item: ScrapedItem) -> ScrapedItem:
    """For items lacking a price, GET the product page to refine availability."""
    try:
        resp = _get(client, item.product_url)
    except httpx.HTTPError:
        return item

    if resp.status_code == 404:
        item.availability = "page_404"
        return item
    if resp.status_code >= 400:
        return item

    tree = HTMLParser(resp.text)

    price_node = (
        tree.css_first("#kindle-price")
        or tree.css_first("#price")
        or tree.css_first(".a-price .a-offscreen")
    )
    cents = _to_cents(price_node.text() if price_node else None)
    if cents is not None:
        item.current_price_cents = cents
        item.availability = "available"
        return item

    body_text = tree.body.text(strip=False).lower() if tree.body else ""
    if "currently unavailable" in body_text or "out of print" in body_text:
        item.availability = "kindle_unavailable"
    return item


def fetch_wishlist(url: str) -> list[ScrapedItem]:
    """Fetch every item across paginated views of a public wishlist URL."""
    root = _amazon_root(url)
    items: dict[str, ScrapedItem] = {}

    with httpx.Client(follow_redirects=True) as client:
        next_url: Optional[str] = url
        page_count = 0
        seen_urls: set[str] = set()

        while next_url and page_count < 50:  # hard safety cap
            if next_url in seen_urls:
                break
            seen_urls.add(next_url)
            page_count += 1

            log.info("Fetching wishlist page %d: %s", page_count, next_url)
            resp = _get(client, next_url)
            if resp.status_code >= 400:
                log.warning("Wishlist page returned %s", resp.status_code)
                break

            tree = HTMLParser(resp.text)
            rows = tree.css('li[data-itemId], li[data-reposition-action-params]')
            new_count = 0
            for row in rows:
                item = _parse_item_row(row, root)
                if item and item.asin not in items:
                    items[item.asin] = item
                    new_count += 1

            log.info("Parsed %d new items (total %d)", new_count, len(items))

            next_url = _next_page_url(resp.text, root)
            if next_url:
                _polite_sleep()

        # Refine items that came back without a price.
        no_price_items = [it for it in items.values() if it.current_price_cents is None]
        for it in no_price_items:
            _polite_sleep()
            _refine_no_price_item(client, it)

    return list(items.values())


def fetch_many(urls: Iterable[str]) -> dict[str, list[ScrapedItem]]:
    out: dict[str, list[ScrapedItem]] = {}
    for u in urls:
        try:
            out[u] = fetch_wishlist(u)
        except Exception as e:
            log.exception("Scrape failed for %s: %s", u, e)
            out[u] = []
    return out
