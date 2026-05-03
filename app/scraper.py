"""Public Amazon wishlist scraper.

Wishlist pages render server-side with `<li data-itemId="...">` rows under
`#g-items`. They paginate via a `lek` token exposed in the page source as
`wlNextLink` / `showMoreUrl`. We follow it until exhausted.

For items the wishlist view shows without a price, we hit the product page
once to disambiguate `kindle_unavailable` (page exists, no buy button) from
`page_404` (item delisted).

When Amazon decides we look like a bot it serves a ~5KB stub page with no
items and a contact-api-services-support note. We detect that case so the
caller can distinguish "blocked" from "list is empty".
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from .config import (
    DATA_DIR,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    REQUEST_TIMEOUT,
    USER_AGENT,
)
from .models import ScrapedItem

log = logging.getLogger(__name__)

DIAG_DIR = DATA_DIR / "diagnostics"

# Browser-like headers. Amazon's bot heuristics look at the full set, not
# just the UA.
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

PRICE_RE = re.compile(r"\$([\d,]+\.\d{2})")

# Try several next-page tokens. Amazon has used different markers across
# rendering variants:
#   - `"showMoreUrl":"/hz/wishlist/...?lek=..."` (JSON island)
#   - `wlNextLink` / `wishlist-load-more-button` data attributes
#   - a hidden form-input named `showMoreToken`
#   - direct `?lek=...` in a "Show more" anchor
NEXT_TOKEN_PATTERNS = [
    re.compile(r'"showMoreUrl"\s*:\s*"([^"]+)"'),
    re.compile(r'data-href="([^"]+lek=[^"]+)"'),
    re.compile(r'<a[^>]+id="endOfListMarker"[^>]*></a>'),  # used as a sentinel
    re.compile(r'href="(/hz/wishlist/ls/[^"]+lek=[^"]+)"'),
]
LEK_TOKEN_RE = re.compile(r'"lastEvaluatedKey"\s*:\s*"([^"]+)"')


class BotDetected(Exception):
    """Raised when Amazon's anti-bot stub is served instead of a wishlist."""


class FetchFailed(Exception):
    """Raised when the first wishlist page can't be fetched at all (HTTP error,
    network error, etc.). Callers should NOT ingest an empty list in this case
    — the previous wishlist_book membership and last_scraped_at stay intact."""


class LoginExpired(Exception):
    """Raised by the Playwright scraper when it detects the saved storage state
    no longer represents a logged-in Amazon session. Caller should stop the
    run, surface a 're-authenticate' message, and leave wishlist_book intact."""


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


def _is_antibot_stub(body: str) -> bool:
    """Detect Amazon's anti-automation stub.

    Length is the cheapest signal — real wishlist pages are >100KB. Combine
    with a content marker so we don't trip on a legitimately tiny empty list.
    """
    if len(body) > 30_000:
        return False
    needles = (
        "automated access to amazon data",
        "to discuss automated access",
        "/errors/validateCaptcha",
        "captcha",
    )
    body_lc = body.lower()
    return any(n in body_lc for n in needles)


def _save_diagnostic(label: str, url: str, body: str) -> Path:
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9]+", "_", label)[:60]
    path = DIAG_DIR / f"{stamp}_{safe}.html"
    path.write_text(f"<!-- url: {url} -->\n{body}", encoding="utf-8", errors="ignore")
    return path


def _get(client: httpx.Client, url: str) -> httpx.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = client.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code in (503, 429) and attempt < 2:
                # exponential backoff for transient throttling
                time.sleep(2 ** attempt + random.uniform(0, 1.5))
                continue
            return resp
        except httpx.HTTPError as e:
            last_exc = e
            time.sleep(2 ** attempt)
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


def _next_page_url(html: str, root: str, current_url: str) -> Optional[str]:
    """Find the next-page URL using whichever token Amazon used today."""
    for pat in NEXT_TOKEN_PATTERNS:
        m = pat.search(html)
        if not m or not m.groups():
            continue
        raw = m.group(1).encode("utf-8").decode("unicode_escape")
        if "lek=" in raw or raw.startswith("/hz/wishlist/"):
            return urljoin(root, raw)
    # Fallback: build the next URL from a lastEvaluatedKey token if present.
    m = LEK_TOKEN_RE.search(html)
    if m:
        from urllib.parse import urlencode, urlparse as _u, parse_qsl
        token = m.group(1).encode("utf-8").decode("unicode_escape")
        parsed = _u(current_url)
        qs = dict(parse_qsl(parsed.query))
        qs["lek"] = token
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{urlencode(qs)}"
    return None


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
    if _is_antibot_stub(resp.text):
        # Don't poison item state from a blocked product fetch.
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


def fetch_wishlist(url: str, *, list_label: str = "wishlist") -> list[ScrapedItem]:
    """Fetch every item across paginated views of a public wishlist URL.

    Raises `BotDetected` if Amazon serves the anti-automation stub on the
    first page (so the caller can mark the wishlist failed instead of empty).
    Saves the raw HTML of any page that yielded zero rows to
    `data/diagnostics/<timestamp>_<label>.html` for later inspection.
    """
    root = _amazon_root(url)
    items: dict[str, ScrapedItem] = {}

    with httpx.Client(follow_redirects=True, headers=HEADERS, http2=False) as client:
        next_url: Optional[str] = url
        page_count = 0
        seen_urls: set[str] = set()

        while next_url and page_count < 100:  # hard safety cap
            if next_url in seen_urls:
                break
            seen_urls.add(next_url)
            page_count += 1

            log.info("Fetching wishlist page %d: %s", page_count, next_url)
            # Any non-success response at any point during pagination raises
            # rather than silently truncating. Partial ingest would replace
            # the wishlist's full membership with whatever we managed to
            # parse before the error, wiping items the wishlist still has.
            try:
                resp = _get(client, next_url)
            except httpx.HTTPError as e:
                log.warning("Wishlist page %d network error: %s", page_count, e)
                raise FetchFailed(
                    f"network error on page {page_count} of {url} (had {len(items)} items so far): {e}"
                ) from e
            body = resp.text
            if resp.status_code >= 400:
                path = _save_diagnostic(f"{list_label}_p{page_count}_http{resp.status_code}", next_url, body)
                raise FetchFailed(
                    f"HTTP {resp.status_code} on page {page_count} of {url} "
                    f"(had {len(items)} items so far); saved {path}"
                )
            if _is_antibot_stub(body):
                path = _save_diagnostic(f"{list_label}_p{page_count}_antibot", next_url, body)
                log.warning("Anti-bot stub on page %d (saved %s)", page_count, path)
                if page_count == 1:
                    raise BotDetected(f"anti-bot stub on first page of {url}")
                raise FetchFailed(
                    f"anti-bot stub on page {page_count} of {url} (had {len(items)} items so far)"
                )

            tree = HTMLParser(body)
            rows = tree.css('li[data-itemId], li[data-reposition-action-params]')
            new_count = 0
            for row in rows:
                item = _parse_item_row(row, root)
                if item and item.asin not in items:
                    items[item.asin] = item
                    new_count += 1

            log.info("Page %d: parsed %d new items (cumulative %d, raw rows %d)",
                     page_count, new_count, len(items), len(rows))

            if new_count == 0:
                path = _save_diagnostic(f"{list_label}_p{page_count}_zero", next_url, body)
                log.warning("Zero items parsed on page %d; saved HTML to %s", page_count, path)

            next_url = _next_page_url(body, root, next_url)
            if next_url:
                _polite_sleep()

        # Refine items that came back without a price (only if we got SOMETHING
        # — if every page hit anti-bot, don't hammer product pages too).
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
