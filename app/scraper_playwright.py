"""Playwright-based wishlist scraper.

Used when a saved Amazon login session (data/storage_state.json) is present.
Logged-in shoppers get vastly higher rate limits than anonymous httpx, so we
avoid the 503/anti-bot wall the public-URL scraper hits.

Same input + output shape as `app.scraper.fetch_wishlist`, so the dispatcher
in services.py can swap between them without further changes.

Page DOM is the same logged-in vs. logged-out (sometimes with extra fields
when logged in), so we reuse the existing `_parse_item_row` and `_to_cents`
from `app.scraper`.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Optional, TYPE_CHECKING

from selectolax.parser import HTMLParser

from .config import REQUEST_DELAY_MAX, REQUEST_DELAY_MIN
from .models import ScrapedItem
from .scraper import (
    BotDetected,
    FetchFailed,
    LoginExpired,
    _amazon_root,
    _is_antibot_stub,
    _parse_item_row,
    _save_diagnostic,
    _next_page_url,
)

if TYPE_CHECKING:  # avoid hard import at module load
    from playwright.sync_api import BrowserContext, Page

log = logging.getLogger(__name__)

# Markers Amazon shows on a page when the session is anonymous.
_LOGGED_OUT_MARKERS = (
    "Hello, sign in",
    "Hello, Sign in",
    "Sign in",
)
_GREETING_SELECTOR = "#nav-link-accountList-nav-line-1, #nav-link-accountList"


def _polite_sleep() -> None:
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def _looks_logged_out(page: "Page") -> bool:
    """Return True if the rendered page looks like an anonymous session.

    We check the account-list nav element. Logged-in users see "Hello, <name>"
    or "Returns & Orders" hover text; logged-out shows "Hello, sign in".
    """
    try:
        node = page.locator(_GREETING_SELECTOR).first
        if node.count() == 0:
            # No nav element rendered at all — likely a captcha or stripped
            # mobile layout. Treat as suspect, not as logged-out.
            return False
        text = (node.inner_text(timeout=2000) or "").strip()
    except Exception:
        return False
    return any(m.lower() in text.lower() for m in _LOGGED_OUT_MARKERS)


def fetch_wishlist_playwright(
    url: str,
    *,
    list_label: str,
    context: "BrowserContext",
) -> list[ScrapedItem]:
    """Fetch every item across paginated views of a wishlist via a logged-in
    Playwright BrowserContext.

    Raises:
      LoginExpired — saved session is no longer logged in.
      BotDetected  — Amazon served the anti-automation stub on first page.
      FetchFailed  — HTTP error / partial pagination failure.
    """
    root = _amazon_root(url)
    items: dict[str, ScrapedItem] = {}
    page = context.new_page()
    try:
        next_url: Optional[str] = url
        page_count = 0
        seen_urls: set[str] = set()

        while next_url and page_count < 100:
            if next_url in seen_urls:
                break
            seen_urls.add(next_url)
            page_count += 1

            log.info("Fetching wishlist page %d (Playwright): %s", page_count, next_url)
            try:
                page.goto(next_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                raise FetchFailed(
                    f"Playwright goto failed on page {page_count} of {url} "
                    f"(had {len(items)} items so far): {e}"
                ) from e

            # Bot stub detection — same content marker as the httpx path.
            body = page.content()
            if _is_antibot_stub(body):
                path = _save_diagnostic(f"{list_label}_p{page_count}_antibot_pw", next_url, body)
                log.warning("Anti-bot stub on page %d (saved %s)", page_count, path)
                if page_count == 1:
                    raise BotDetected(f"anti-bot stub on first page of {url}")
                raise FetchFailed(
                    f"anti-bot stub on page {page_count} of {url} "
                    f"(had {len(items)} items so far)"
                )

            # Logged-out detection — only on first page; if we made it this
            # far on subsequent pages, the session is clearly fine.
            if page_count == 1 and _looks_logged_out(page):
                path = _save_diagnostic(f"{list_label}_p{page_count}_loggedout", next_url, body)
                log.warning("Saved session no longer logged in (saved %s)", path)
                raise LoginExpired(
                    f"saved storage_state no longer logged in (first page of {url})"
                )

            # Wait for the items list to actually render. Some wishlist
            # variants lazy-load via XHR after DOMContentLoaded.
            try:
                page.wait_for_selector("#g-items li, #wl-item-view li[data-id]", timeout=10_000)
            except Exception:
                # Continue anyway — _parse_item_row will yield 0 if nothing
                # matched and we'll save a diagnostic below.
                pass

            tree = HTMLParser(page.content())
            rows = tree.css('li[data-itemId], li[data-reposition-action-params]')
            new_count = 0
            for row in rows:
                item = _parse_item_row(row, root)
                if item and item.asin not in items:
                    items[item.asin] = item
                    new_count += 1

            log.info(
                "Page %d (PW): parsed %d new items (cumulative %d, raw rows %d)",
                page_count, new_count, len(items), len(rows),
            )

            if new_count == 0 and len(rows) == 0:
                path = _save_diagnostic(
                    f"{list_label}_p{page_count}_zero_pw", next_url, page.content()
                )
                if page_count == 1:
                    # First page yielded nothing and isn't an anti-bot/logged-out
                    # — selector drift or genuinely empty list. Don't ingest.
                    raise FetchFailed(
                        f"first page of {url} yielded zero rows; saved {path}"
                    )

            next_url = _next_page_url(page.content(), root, next_url)
            if next_url:
                _polite_sleep()
    finally:
        page.close()

    return list(items.values())
