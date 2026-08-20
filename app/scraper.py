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
    BLOCK_RETRY_503,
    BLOCK_RETRY_BACKOFF,
    BLOCK_RETRY_STUB_MIDLIST,
    DATA_DIR,
    MAX_PAGES_PER_WISHLIST,
    MAX_STALE_PAGES,
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


def _is_amazon_error_page(body: str) -> bool:
    """Detect Amazon's 503 "Dogs of Amazon" error page.

    Served as a soft rate-limit distinct from the anti-automation stub: small
    body, title "Sorry! Something went wrong!", `ref=cs_503_*` links and the
    `500_503.png` hero image. It is NOT just noise -- an error page carries no
    pagination token, so mid-list it makes pagination end looking exactly like
    a natural end-of-list (that is what truncated wishlist 6 from 496 to 10 on
    2026-08-19). Same size gate as the stub so a legitimate page that merely
    mentions these strings cannot match.
    """
    if len(body) > 30_000:
        return False
    body_lc = body.lower()
    needles = (
        "ref=cs_503",
        "500_503.png",
        "sorry! something went wrong",
    )
    return any(n in body_lc for n in needles)


def _classify_block_page(body: str) -> Optional[str]:
    """Return the block-page kind ("antibot" | "error503") or None.

    One chokepoint for both scrapers and the product-page refiner, so a newly
    learned block shape is recognized everywhere at once.
    """
    if _is_antibot_stub(body):
        return "antibot"
    if _is_amazon_error_page(body):
        return "error503"
    return None


_BLOCK_KIND_LABEL = {"antibot": "anti-bot stub", "error503": "Amazon 503 error page"}


def _block_retry_delay(kind: str, page_count: int, attempt: int) -> Optional[float]:
    """Seconds to wait before re-fetching a blocked page, or None to give up.

    Policy (shared by both scrapers -- keep it here, not in the loops):
    - "error503" is a transient; retry up to BLOCK_RETRY_503 times.
    - "antibot" mid-list is often transient too (seen at pages 16/46/84 with
      the next day's scrapes succeeding), so it gets BLOCK_RETRY_STUB_MIDLIST
      cautious retries -- but NEVER on page 1, where the stub means the whole
      visit was refused and hammering it invites a real ban.
    `attempt` is 0-based: the first retry decision passes attempt=0.
    """
    if kind == "error503":
        allowed = BLOCK_RETRY_503
    elif kind == "antibot" and page_count > 1:
        allowed = BLOCK_RETRY_STUB_MIDLIST
    else:
        allowed = 0
    if attempt >= allowed:
        return None
    return BLOCK_RETRY_BACKOFF * (attempt + 1) + random.uniform(0, 15)



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


def _check_pagination_complete(
    url: str, page_count: int, next_url: Optional[str], item_count: int
) -> None:
    """Raise if we stopped on the page budget instead of on end-of-list.

    Both scrapers call this once the loop exits. A still-live `next_url` at the
    cap means there was more list than budget, so what we hold is a prefix --
    and ingesting a prefix replaces the wishlist's whole membership with it.
    Partial pagination is a failure, not a truncation (see CLAUDE.md).
    """
    if next_url and page_count >= MAX_PAGES_PER_WISHLIST:
        raise FetchFailed(
            f"page cap ({MAX_PAGES_PER_WISHLIST}) reached on {url} with more pages "
            f"still offered (had {item_count} items) -- result would be partial"
        )


class _PaginationTracker:
    """Decides when pagination has really ended. Shared by BOTH scrapers --
    keep end-of-list policy here, not in the loops (CLAUDE.md's no-forking
    rule).

    Two counters, deliberately separate:

    - ``stale_pages``: consecutive pages FULL of rows we already hold. That is
      what Amazon's real end of list looks like -- it keeps minting fresh
      paginationTokens past the last item, each re-serving collected rows --
      so hitting MAX_STALE_PAGES is a clean stop.
    - ``empty_pages``: consecutive pages with no item rows at all. End of list
      never looks like that (end-of-list pages HAVE rows), so it is selector
      drift or an unrecognized soft-block; hitting the same budget raises
      FetchFailed rather than blessing a partial result (zero rows on a later
      page is a failure, not a truncation). A single trailing empty page
      usually carries no next token and ends the loop naturally first.

    An empty page neither confirms nor denies end-of-list, so it does not
    reset ``stale_pages``; any page that adds a new item resets both.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.stale_pages = 0
        self.empty_pages = 0

    def note_page(
        self, *, page_count: int, new_count: int, row_count: int, item_count: int
    ) -> bool:
        """Record one parsed page. True = stop cleanly (end of list reached)."""
        if row_count == 0:
            self.empty_pages += 1
            if self.empty_pages >= MAX_STALE_PAGES:
                raise FetchFailed(
                    f"{self.empty_pages} consecutive pages with zero item rows on "
                    f"{self.url} (had {item_count} items) -- soft-block or "
                    f"selector drift, not end-of-list; result would be partial"
                )
            return False
        self.empty_pages = 0
        if new_count == 0:
            self.stale_pages += 1
            if self.stale_pages >= MAX_STALE_PAGES:
                log.info(
                    "Stopping at page %d: %d consecutive pages added nothing new "
                    "(end of list, %d items)",
                    page_count, self.stale_pages, item_count,
                )
                return True
        else:
            self.stale_pages = 0
        return False


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
    if _classify_block_page(resp.text):
        # Don't poison item state from a blocked or errored product fetch.
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
        tracker = _PaginationTracker(url)

        while next_url and page_count < MAX_PAGES_PER_WISHLIST:
            if next_url in seen_urls:
                # Every observed paginationToken is unique, so this "never"
                # fires; if it ever does we already hold every row that URL
                # serves, so stopping is safe -- but leave a trace for the day
                # Amazon's token semantics change.
                log.warning(
                    "Pagination URL repeated at page %d of %s -- treating as end of list",
                    page_count, url,
                )
                break
            seen_urls.add(next_url)
            page_count += 1

            log.info("Fetching wishlist page %d: %s", page_count, next_url)
            # Any non-success response at any point during pagination raises
            # rather than silently truncating. Partial ingest would replace
            # the wishlist's full membership with whatever we managed to
            # parse before the error, wiping items the wishlist still has.
            attempt = 0
            while True:
                try:
                    resp = _get(client, next_url)
                except httpx.HTTPError as e:
                    log.warning("Wishlist page %d network error: %s", page_count, e)
                    raise FetchFailed(
                        f"network error on page {page_count} of {url} (had {len(items)} items so far): {e}"
                    ) from e
                body = resp.text
                # Classify the body BEFORE the generic status gate: the 503 dog
                # page arrives with a 503 status here, and blocked pages have
                # their own (retryable) handling via _block_retry_delay.
                kind = _classify_block_page(body)
                if kind is None:
                    if resp.status_code >= 400:
                        path = _save_diagnostic(f"{list_label}_p{page_count}_http{resp.status_code}", next_url, body)
                        raise FetchFailed(
                            f"HTTP {resp.status_code} on page {page_count} of {url} "
                            f"(had {len(items)} items so far); saved {path}"
                        )
                    break
                what = _BLOCK_KIND_LABEL[kind]
                path = _save_diagnostic(f"{list_label}_p{page_count}_{kind}", next_url, body)
                delay = _block_retry_delay(kind, page_count, attempt)
                if delay is None:
                    log.warning("%s on page %d (saved %s)", what, page_count, path)
                    if page_count == 1:
                        raise BotDetected(f"{what} on first page of {url}")
                    raise FetchFailed(
                        f"{what} on page {page_count} of {url} (had {len(items)} items so far)"
                    )
                attempt += 1
                log.warning("%s on page %d (saved %s); retry %d in %.0fs",
                            what, page_count, path, attempt, delay)
                time.sleep(delay)

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

            # Only a page with no ROWS AT ALL is a selector problem worth a
            # diagnostic; a page of rows we already hold is just Amazon
            # paginating past the end of the list.
            if not rows:
                path = _save_diagnostic(f"{list_label}_p{page_count}_zero", next_url, body)
                log.warning("Zero rows on page %d; saved HTML to %s", page_count, path)
                if page_count == 1:
                    # First page yielded nothing and isn't an anti-bot stub --
                    # selector drift or a genuinely empty list. Don't ingest
                    # (same contract as the Playwright path).
                    raise FetchFailed(
                        f"first page of {url} yielded zero rows; saved {path}"
                    )
            if tracker.note_page(
                page_count=page_count, new_count=new_count,
                row_count=len(rows), item_count=len(items),
            ):
                # `next_url = None` tells _check_pagination_complete below this
                # exit was a natural end, not the page budget.
                next_url = None
                break

            next_url = _next_page_url(body, root, next_url)
            if next_url:
                _polite_sleep()

        _check_pagination_complete(url, page_count, next_url, len(items))

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
