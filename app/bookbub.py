"""BookBub daily ebook deals client + parser.

Reads the day's ebook deals from bookbub.com using the signed outbound link
from the daily BookBub email (the one that auto-logs you into BookBub).

BookBub sits behind a Cloudflare "Just a moment..." managed challenge, so a
plain ``httpx`` client is stopped at the challenge page before it ever sees the
deals. This module therefore tries a lightweight httpx pass first (it will work
if the site ever relaxes bot-gating, and it is cheap) and, when that yields an
auth-wall or zero deals, falls back to Chromium driven by Playwright, which
executes the challenge JS, keeps the login cookies the outbound link set, and
renders the deal cards.

Login is primarily a BookBub account login (``BOOKBUB_USERNAME`` /
``BOOKBUB_PASSWORD``, set in the environment — never committed). The signed
outbound link from the daily email is still accepted as a fallback so
ad-hoc/probe runs work without credentials.

Flow (mirrors the httpx<->playwright fallback used elsewhere in this app):

  1. Log in — either via the account login form (credentials) or by following
     the outbound link, which auto-logs the visitor in and lands on the
     daily-deals page (the link itself carries the target ``?date=``).
  2. Navigate (same session) to ``/ebook-deals/daily-deals?date=YYYYMMDD``.
  3. Parse each deal card: title, author(s), deal price, book URL, and the
     Amazon Kindle link (resolved from the card's Amazon retailer button).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import random
import sys
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from .config import (
    BOOKBUB_DAILY_DEALS_BASE,
    BOOKBUB_DATE_FORMAT,
    BOOKBUB_LOGIN_LINK,
    BOOKBUB_PASSWORD,
    BOOKBUB_USERNAME,
    USER_AGENT,
)

# Browser-like headers. BookBub's bot heuristics look at the full set, not
# just the UA (same approach as app.scraper).
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

_BASE = "https://www.bookbub.com"

# ---- Deal-card selectors (confirmed against the live rendered page) ---------
# Each deal is a `.books-feed-item-container`. Inside it:
#   .book-info-title            -> <a href="/books/...">Title</a>   (title + url)
#   .book-info-authors .person-name -> author(s) (one per author)
#   .deal-price .discount-price -> the deal price, e.g. "$2.99"
#   .deal-price .original-price -> retail/strikethrough price
#   .discount-price-free        -> present instead for $0 deals ("Free!")
CARD = ".books-feed-item-container"
TITLE = ".book-info-title"
# Author names live in the /authors/ links inside .book-info-authors. BookBub packs
# multiple authors into a single .person-name div ("<a>Iris</a> and <a>Roy</a>"),
# so selecting the author links — not the container text — keeps names clean.
AUTHORS = ".book-info-authors a[href*='/authors/']"
DEAL_PRICE = ".deal-price .discount-price"
FREE_PRICE = ".discount-price-free"
ORIGINAL_PRICE = ".deal-price .original-price"
# BookBub's Amazon retailer id. A deal card's Amazon button is an anchor whose
# href is `.../promotion_site_active_check/{id}?promotion_type=deals&retailer_id=1`
# and which 302-redirects (a plain GET) to the Amazon Kindle product page.
AMAZON_RETAILER_HREF_MARK = "retailer_id=1"

# ---- Account-login selectors (validated once against the live login page) --
# BookBub's account login lives at /users/sign_in with a standard email +
# password form (the old /login now 404s -> 'Page Not Found - BookBub'). If
# BookBub's markup drifts, adjust the selectors HERE (one place) and the
# daily updater + probe inherit the fix automatically — same convention as the
# deal-card selectors above.
LOGIN_URL = f"{_BASE}/users/sign_in"
LOGIN_EMAIL_SEL = "input[type='email']"
LOGIN_PASSWORD_SEL = "input[type='password']"
LOGIN_SUBMIT_SEL = "input[type='submit'], button[type='submit']"


class BookbubError(RuntimeError):
    """Raised on a failed login, a fetch error, or when no deals are found."""


@dataclass
class Deal:
    title: str
    author: str
    price: str
    url: str
    # The Amazon link for the deal. parse_deals() captures the BookBub
    # intermediate (retailer_id=1) href; resolve_amazon_urls() rewrites it to
    # the final amazon.com Kindle page. None when the card has no Amazon
    # retailer (a "no amazon link" deal).
    amazon_url: str | None = None
    original_price: str = ""


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _amazon_anchor(card):
    """Return the Amazon retailer anchor in a deal card, or None.

    Prefers the anchor carrying ``retailer_id=1`` (BookBub's Amazon slot);
    falls back to a button whose label is "Amazon".
    """
    anchor = card.css_first(f"a[href*='{AMAZON_RETAILER_HREF_MARK}']")
    if anchor is not None:
        return anchor
    for a in card.css("a"):
        if (a.text(strip=True) or "").strip().lower() == "amazon":
            return a
    return None


def parse_deals(html: str) -> list[Deal]:
    """Parse deal cards out of a rendered daily-deals page.

    Returns a de-duplicated, order-preserving list of :class:`Deal`. Tolerates
    cards with missing price/author (those fields come back empty).
    """
    tree = HTMLParser(html)
    deals: list[Deal] = []
    seen: set[str] = set()
    for card in tree.css(CARD):
        title_el = card.css_first(TITLE)
        if title_el is None:
            continue
        title = title_el.text(strip=True) or ""
        if not title:
            continue

        url = title_el.attributes.get("href", "") if title_el.attributes else ""
        url = urljoin(_BASE, url) if url else ""

        authors = [a.text(strip=True) for a in card.css(AUTHORS)]
        authors = [a for a in authors if a]
        author = ", ".join(authors)

        price_el = card.css_first(DEAL_PRICE)
        if price_el is None:
            price_el = card.css_first(FREE_PRICE)
        price = price_el.text(strip=True) if price_el is not None else ""

        orig_el = card.css_first(ORIGINAL_PRICE)
        original_price = orig_el.text(strip=True) if orig_el is not None else ""

        amz = _amazon_anchor(card)
        amz_href = amz.attributes.get("href", "") if (amz is not None and amz.attributes) else ""
        amazon_url = urljoin(_BASE, amz_href) if amz_href else None

        key = url or f"{title}|{author}|{price}"
        if key in seen:
            continue
        seen.add(key)
        deals.append(
            Deal(
                title=title,
                author=author,
                price=price,
                url=url,
                amazon_url=amazon_url,
                original_price=original_price,
            )
        )
    return deals


# --------------------------------------------------------------------------- #
# httpx pass (fast, but stopped by Cloudflare today)
# --------------------------------------------------------------------------- #
def _is_cloudflare_challenge(resp: httpx.Response) -> bool:
    """Heuristic: does this response look like a Cloudflare interstitial?"""
    if resp.status_code in (403, 503) and "just a moment" in resp.text[:4000].lower():
        return True
    head = resp.text[:8000]
    return (
        "just a moment" in head.lower()
        and ("cf_chl" in head or "challenge-platform" in head or "challenges.cloudflare.com" in head)
    )


def _deals_url(date: str) -> str:
    return f"{BOOKBUB_DAILY_DEALS_BASE}?date={date}"


def outbound_session(link: str) -> httpx.Client:
    """Follow the outbound link (redirect chain + cookie jar) and return an open client.

    Following the redirect chain lands on bookbub.com and sets the BookBub login
    cookies on the client's jar. The caller owns the returned client and must
    close it. (For a real browser this same step also clears the Cloudflare
    managed challenge; httpx captures the cookies but cannot execute the
    challenge JS, which is why the Playwright fallback exists.)
    """
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=40)
    client.get(link)
    return client


def fetch_daily_deals_httpx(link: str, date: str) -> list[Deal]:
    """Attempt the whole flow with httpx (cookie jar + redirect following).

    Returns the parsed deals, or an empty list if the session was never
    authenticated (e.g. Cloudflare interstitial) — the caller then falls back
    to Playwright. A network failure (unreachable host, timeout, TLS) is
    surfaced as :class:`BookbubError` so the caller can try the browser path
    instead of leaking a raw httpx traceback.
    """
    try:
        client = outbound_session(link)
        try:
            resp = client.get(_deals_url(date))
        finally:
            client.close()
    except httpx.HTTPError as e:
        raise BookbubError(f"BookBub unreachable via httpx: {e}") from e
    if _is_cloudflare_challenge(resp):
        return []
    if resp.status_code != 200:
        raise BookbubError(f"HTTP {resp.status_code} on {_deals_url(date)}")
    return parse_deals(resp.text)


# --------------------------------------------------------------------------- #
# Playwright pass (clears the Cloudflare challenge)
# --------------------------------------------------------------------------- #
def _launch_args() -> list[str]:
    # --disable-blink-features=AutomationControlled hides the default
    # navigator.webdriver flag; needed to get through the Cloudflare challenge.
    return ["--disable-blink-features=AutomationControlled", "--no-default-browser-check"]


def _wait_past_challenge(page, timeout_s: float = 90.0) -> bool:
    """Poll until Cloudflare's managed challenge is gone (see ``_is_challenged``)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _is_challenged(page):
            return True
        time.sleep(1.5)
    return False


def _on_daily_deals_for_date(page, date: str) -> bool:
    """True if the current page is already the daily-deals view for ``date``."""
    try:
        parsed = urlparse(page.url)
        if "daily-deals" not in parsed.path:
            return False
        return parse_qs(parsed.query).get("date", [None])[0] == date
    except Exception:
        return False


def _load_deals(page) -> None:
    """Wait for the Cloudflare challenge to clear and the deal cards to render.

    A headed pass under a trusted IP can sit in the managed challenge (or its
    Turnstile iframe) for tens of seconds, so the selector wait is generous
    (90s) rather than the earlier 20s the challenge routinely exceeded.
    """
    _wait_past_challenge(page)
    try:
        page.wait_for_selector(CARD, timeout=90_000)
    except Exception:
        pass  # blocked or empty; parse will yield [] and the caller retries
    time.sleep(2)


def _login(page, username: str, password: str) -> None:
    """Log into BookBub with account credentials on an open page.

    Best-effort: it navigates to the login page, clears the Cloudflare
    challenge, fills the email + password fields and submits, then clears the
    challenge again on the resulting page (a page that already reads
    non-challenged returns immediately). If the credentials are wrong BookBub
    just re-serves the login form — the subsequent daily-deals navigation will
    then find no deals and the caller surfaces a clean error.
    """
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)
    _wait_past_challenge(page)
    page.wait_for_selector(LOGIN_EMAIL_SEL, timeout=120_000)
    page.fill(LOGIN_EMAIL_SEL, username)
    page.fill(LOGIN_PASSWORD_SEL, password)
    page.click(LOGIN_SUBMIT_SEL)
    _wait_past_challenge(page)


def fetch_daily_deals_playwright(
    date: str,
    link: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    headless: bool = True,
) -> list[Deal]:
    """Log in and read the daily-deals page in Chromium.

    Login is via account credentials (``username``/``password``) when both are
    given, otherwise via the outbound ``link`` (which auto-logs the visitor in
    and lands directly on the daily-deals page for *the link's own date*). In
    the link case, if that landing page is already the requested date we parse
    it as-is — a redundant reload of the same URL re-triggers the Cloudflare
    managed challenge and is flaky. Credential login lands on the site, so the
    requested date is always navigated to explicitly. Raises
    :class:`BookbubError` when neither credentials nor a link are available.
    """
    creds = bool(username and password)
    if not creds and not link:
        raise BookbubError(
            "no BookBub login configured: set BOOKBUB_USERNAME/BOOKBUB_PASSWORD "
            "(or pass --link for the daily email's outbound link)"
        )
    from playwright.sync_api import sync_playwright  # lazy: optional dependency

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=_launch_args())
        try:
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            page = ctx.new_page()
            # 1. Login: account credentials, or the outbound link (auto-login +
            #    clears the Cloudflare challenge, landing on the daily-deals
            #    page for the link's date).
            if creds:
                _login(page, username, password)
            else:
                page.goto(link, wait_until="domcontentloaded", timeout=60_000)
                _load_deals(page)
            if creds or not _on_daily_deals_for_date(page, date):
                # 2. Same session, navigate to the requested day.
                page.goto(_deals_url(date), wait_until="domcontentloaded", timeout=60_000)
                _load_deals(page)
                html = page.content()
            else:
                html = page.content()
        finally:
            browser.close()
    return parse_deals(html)


def _is_challenged(page) -> bool:
    """True if the page is (still) under Cloudflare's managed challenge.

    Covers both the classic "Just a moment…" interstitial (title check) and
    the modern Turnstile widget, which renders a ``challenges.cloudflare.com``
    iframe inside an otherwise non-challenge-titled page. Being conservative
    (returning True when unsure) makes a caller wait a little longer rather
    than proceeding while still blocked.
    """
    try:
        if "just a moment" in (page.title() or "").lower():
            return True
        for f in page.frames:
            url = (f.url or "").lower()
            if "challenges.cloudflare.com" in url or "challenge-platform" in url:
                return True
    except Exception:
        return True
    return False


def fetch_daily_deals_many(
    dates: list[str],
    link: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    headless: bool = True,
    inter_date_delay: float = 0.0,
    challenged: set[str] | None = None,
) -> dict[str, list[Deal]]:
    """Fetch several daily-deals pages in ONE logged-in browser session.

    Logs in once — via account credentials (``username``/``password``) when
    both are given, otherwise via the outbound ``link`` (clearing the
    Cloudflare managed challenge) — then navigates to ``?date=YYYYMMDD`` for
    each date in ``dates`` (in order) on the same session, reusing the login
    instead of paying the login+challenge cost per date.

    Every date is best-effort: a date whose page re-challenges or carries no
    deal cards yields an *empty list* for that date (recorded, never an
    abort). Two hard aborts:

    * ``BookbubError`` if neither credentials nor a ``link`` are available, and
    * ``BookbubError`` if the login page never clears the challenge (a dead /
      stale session — no point continuing with one).

    ``challenged`` (optional set) is filled with the dates whose page was
    still on the interstitial after the wait, so callers can distinguish
    "re-challenged" (retriable) from "genuinely no deals".
    ``inter_date_delay`` (seconds) paces the navigations with ±25% jitter.

    Returns ``{date: [Deal, ...]}`` with every input date as a key.
    """
    username = username or BOOKBUB_USERNAME
    password = password or BOOKBUB_PASSWORD
    creds = bool(username and password)
    link = link if link else (None if creds else BOOKBUB_LOGIN_LINK)
    if not creds and not link:
        raise BookbubError(
            "no BookBub login configured: set BOOKBUB_USERNAME/BOOKBUB_PASSWORD "
            "(or pass --link for the daily email's outbound link)"
        )
    if not dates:
        return {}
    if challenged is None:
        challenged = set()

    from playwright.sync_api import sync_playwright  # lazy: optional dependency

    result: dict[str, list[Deal]] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=_launch_args())
        try:
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            page = ctx.new_page()
            # 1. Login: account credentials, or the outbound link (auto-login
            #    + clears the Cloudflare challenge, landing on the daily-deals
            #    page for the link's own date).
            if creds:
                _login(page, username, password)
            else:
                page.goto(link, wait_until="domcontentloaded", timeout=60_000)
                _load_deals(page)
            if _is_challenged(page):
                raise BookbubError(
                    "Cloudflare challenge never cleared after the BookBub login — "
                    "the credentials/link may be wrong or stale, or Cloudflare is "
                    "hard-blocking the browser"
                )

            for i, date in enumerate(dates):
                html: str | None
                # The landing page already IS the first date's daily-deals view
                # when the link's own ?date= matches it: parse it as-is — a
                # redundant reload of the same URL re-triggers the challenge.
                if i == 0 and _on_daily_deals_for_date(page, date):
                    html = page.content()
                else:
                    try:
                        page.goto(_deals_url(date), wait_until="domcontentloaded", timeout=60_000)
                        _load_deals(page)
                        html = page.content()
                    except Exception:
                        # Navigation hiccup: record the date as empty and keep
                        # the session going (best-effort per date).
                        html = None
                if html is None:
                    result[date] = []
                else:
                    if _is_challenged(page):
                        challenged.add(date)
                    result[date] = parse_deals(html)
                if i < len(dates) - 1 and inter_date_delay > 0:
                    time.sleep(inter_date_delay * random.uniform(0.75, 1.25))
        finally:
            browser.close()

    for d in result.values():
        resolve_amazon_urls(d)
    return result


def resolve_amazon_urls(deals: list[Deal], *, timeout: float = 30.0) -> list[Deal]:
    """Best-effort: turn each deal's BookBub intermediate Amazon link into the
    final amazon.com Kindle page URL via a follow_redirects GET.

    Never raises — a failed or non-Amazon resolution keeps the intermediate
    URL (or None), so the fetch always succeeds and the link is retained for
    audit. Mutates and returns ``deals``.
    """
    if not deals:
        return deals
    client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=timeout)
    try:
        for d in deals:
            if not d.amazon_url:
                continue
            try:
                resp = client.get(d.amazon_url)
            except httpx.HTTPError:
                continue
            host = (resp.url.host or "").lower()
            if "amazon" in host:
                d.amazon_url = str(resp.url)
    finally:
        client.close()
    return deals


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def fetch_daily_deals(
    date: str,
    link: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    headless: bool | None = None,
) -> list[Deal]:
    """Fetch the day's deals, falling back from httpx to Playwright.

    Login is via account credentials when both ``username`` and ``password``
    are given (or set via ``BOOKBUB_USERNAME``/``BOOKBUB_PASSWORD``); the
    outbound ``link`` (defaulting to ``BOOKBUB_LOGIN_LINK``) is used only when
    no credentials are configured. ``headless`` pins the browser mode; when
    ``None`` headless is tried first and headful as a retry (headful clears
    Cloudflare more reliably). Raises :class:`BookbubError` when neither
    credentials nor a link are available.
    """
    username = username or BOOKBUB_USERNAME
    password = password or BOOKBUB_PASSWORD
    creds = bool(username and password)
    link = link or BOOKBUB_LOGIN_LINK
    if not creds and not link:
        raise BookbubError(
            "no BookBub login configured: set BOOKBUB_USERNAME/BOOKBUB_PASSWORD "
            "(or pass --link for the daily email's outbound link)"
        )

    deals: list[Deal] = []

    # Fast path — only useful if Cloudflare isn't interstitial-ing us, and
    # only for the link (httpx can't perform the account login form).
    if not creds:
        try:
            deals = fetch_daily_deals_httpx(link, date)
        except BookbubError:
            deals = []

    # Browser fallback — the path that actually clears Cloudflare.
    if not deals:
        modes = [headless] if headless is not None else [True, False]
        last_err: Exception | None = None
        for mode in modes:
            try:
                deals = fetch_daily_deals_playwright(
                    date, link=link, username=username, password=password, headless=mode
                )
            except Exception as e:  # noqa: BLE001 - surface a clean error below
                last_err = e
                continue
            if deals:
                break
        if not deals:
            raise BookbubError(
                f"no BookBub deals found for {date} (httpx and the browser "
                f"{'both headless and headful' if len(modes) > 1 else f'browser (headless={modes[0]})'} "
                f"all returned nothing"
                + (f"; last error: {last_err}" if last_err else "")
                + " — the credentials/outbound link may be stale/expired, or Cloudflare may "
                + "have challenged the browser"
            )

    return resolve_amazon_urls(deals)


# --------------------------------------------------------------------------- #
# __main__ probe
# --------------------------------------------------------------------------- #
def _normalise_date(raw: str | None) -> str:
    """Return a bare YYYYMMDD. Default to today; accept YYYYMMDD or YYYY-MM-DD."""
    if not raw:
        return _dt.date.today().strftime(BOOKBUB_DATE_FORMAT)
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 8:
        raise SystemExit(f"--date must be YYYYMMDD (or YYYY-MM-DD), got: {raw!r}")
    return digits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe BookBub daily deals (login + fetch + parse).")
    parser.add_argument("--link", default=None, help="Outbound auto-login link from the daily email.")
    parser.add_argument("--username", default=None,
                        help="BookBub account email (default: BOOKBUB_USERNAME).")
    parser.add_argument("--password", default=None,
                        help="BookBub account password (default: BOOKBUB_PASSWORD).")
    parser.add_argument("--date", default=None, help="Day to fetch, YYYYMMDD (default: today).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headless", dest="headless", action="store_true", default=None,
                      help="Force headless Chromium (default when nothing is given).")
    mode.add_argument("--headful", dest="headless", action="store_false",
                      help="Force headful Chromium (better if Cloudflare blocks headless).")
    args = parser.parse_args(argv)

    link = args.link or BOOKBUB_LOGIN_LINK
    date = _normalise_date(args.date)
    try:
        deals = fetch_daily_deals(
            date,
            link=link,
            username=args.username or BOOKBUB_USERNAME or None,
            password=args.password or BOOKBUB_PASSWORD or None,
            headless=args.headless,
        )
    except BookbubError as e:
        print(f"BOOKBUB PROBE FAILED: {e}", file=sys.stderr)
        return 1

    if not deals:
        print("BOOKBUB PROBE FAILED: no deals parsed", file=sys.stderr)
        return 1

    for i, d in enumerate(deals, 1):
        print(f"{i}. {d.title} — {d.author} — {d.price}")
        if d.url:
            print(f"   bookbub: {d.url}")
        if d.amazon_url:
            print(f"   amazon:  {d.amazon_url}")
        else:
            print("   amazon:  (no amazon link)")
    print(f"\n{len(deals)} deals for {date}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
