"""Read a book's *current* price from its Amazon product page.

Given an ``amazon_url`` (or ASIN) and a :class:`~app.fingerprint.Fingerprint`,
open ``https://www.amazon.com/dp/<ASIN>`` in a Playwright Chromium whose
context is built from the fingerprint (User-Agent / locale / viewport) and
return the current price.

Price extraction is a two-stage best effort:

1. **Selector fast path** — try a set of known Amazon price nodes
   (``span.a-price span.a-offscreen`` and friends) and take the first that
   yields text.
2. **Optional local-LLM fallback** — when ``LLM_MODEL`` is set and the
   selectors miss, send a bounded trim of the page's ``innerText`` to the
   OpenAI-compatible gateway (``LLM_BASE_URL``) and ask it for just the price.
   The LLM is off by default and a failure here *never* blocks: it logs a
   warning and the result's price is left ``None``.

On a hard-to-read or blocked page we save the raw HTML to
``data/diagnostics/`` (the repo convention) and return a result whose
``price_text``/``price_cents`` are ``None`` — we never raise for a page that
loaded. ASSUMPTION (recorded): the page is fetched as ``/dp/<ASIN>``
regardless of the VPN exit country (rotation is anti-bot, not storefront
switching), and price selectors are best-effort with the LLM as the optional
fallback.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser

from .config import (
    AMAZON_LLM_TEXT_CAP,
    AMAZON_NAV_TIMEOUT_MS,
    DATA_DIR,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TIMEOUT,
    USER_AGENT,
)
from .deals_db import asin_from_amazon_url, parse_price_cents
from .fingerprint import Fingerprint
from .scraper import _BLOCK_KIND_LABEL, _classify_block_page

__all__ = [
    "PriceResult",
    "read",
    "read_page",
    "LAUNCH_ARGS",
    "build_dp_url",
    "extract_page_meta_html",
    "download_cover",
]

log = logging.getLogger("amazon_price")

# Chromium launch args that hide the default automation marker (mirrors
# app.bookbub._launch_args).
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-default-browser-check"]

# Primary price containers, tried in order (buybox / apex first — the current
# price — before other-edition or list prices). Each is read via
# _price_text_of, which prefers the ``a-offscreen`` text and falls back to the
# whole/fraction digits when that is blank (modern Amazon's buybox does this).
_PRICE_CONTAINERS = (
    ".apex-pricetopay-value",
    "#corePrice_feature_div .a-price",
    "#corePriceDisplay_desktop_feature_div .a-price",
    ".priceToPay .a-price",
    "#buybox .a-price",
    "span.a-price.a-text-price",
)

DIAG_DIR = DATA_DIR / "diagnostics"

# How much page text to hand to the LLM fallback (chars) and how long a
# navigation may take before we give up on that book (both overridable via
# config / env).
_LLM_TEXT_CAP = AMAZON_LLM_TEXT_CAP
_NAV_TIMEOUT_MS = AMAZON_NAV_TIMEOUT_MS

_LLM_SYSTEM_PROMPT = (
    "You are reading an Amazon product page. State the CURRENT price of the "
    "item. Reply with only one of: a price such as \"$4.99\", the word "
    "\"FREE\", or the word \"NONE\" (if there is no price or it is "
    "unavailable)."
)


@dataclass
class PriceResult:
    """The outcome of reading one product page.

    ``price_cents`` is the parsed integer-cents value (``None`` when the price
    could not be read); ``source`` says which stage produced it
    (``'selector'`` | ``'llm'`` | ``None``); ``note`` is a short human-readable
    reason for a failed/ambiguous read.
    """

    price_text: str | None = None
    price_cents: int | None = None
    source: str | None = None
    note: str = ""


def build_dp_url(amazon_url: str | None, asin: str | None = None) -> str | None:
    """Return the bare ``https://www.amazon.com/dp/<ASIN>`` for a deal's link.

    Uses ``asin`` when given, else extracts it from ``amazon_url``. ``None``
    when no ASIN is present (a no-Amazon deal or an unresolved BookBub
    intermediate link).
    """
    code = asin or (asin_from_amazon_url(amazon_url) if amazon_url else None)
    return f"https://www.amazon.com/dp/{code}" if code else None


# --------------------------------------------------------------------------- #
# Book cover + description capture (verification-time metadata)
# --------------------------------------------------------------------------- #
# Browser-like headers for the cover-image fetch. BookBub/Amazon bot
# heuristics look at the full set, not just the UA (same convention as
# app.scraper / app.bookbub).
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Product-page anchors for the book's cover image and the description block.
# Co-located so an Amazon markup change is a one-place fix (same convention as
# the price containers above).
COVER_SELECTORS = ("#landingImage", "#imgBlkFront")
DESCRIPTION_SELECTORS = ("#bookDescription_feature_div", "#productDescription")

# Image extensions accepted when deriving the cover filename; anything else
# falls back to .jpg (Amazon serves the cover as JPEG by default).
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _collapse_ws(text: str | None) -> str:
    """Collapse runs of whitespace to single spaces (tooltip-friendly)."""
    return re.sub(r"\s+", " ", text or "").strip()


def _cover_ext(url: str) -> str:
    """The image extension from the URL path, else ``.jpg``."""
    base = os.path.basename(urlsplit(url).path)
    ext = os.path.splitext(base)[1].lower()
    return ext if ext in _IMAGE_EXTS else ".jpg"


def extract_page_meta_html(html: str) -> dict:
    """Parse the book cover URL, description, star rating and rating count out
    of a rendered Amazon product page (pure — offline-testable against a canned
    HTML string).

    Best-effort: never raises; returns ``{"cover_url": str, "description": str,
    "stars": str, "ratings": str}`` with empty strings when any is absent. The
    cover prefers the ``data-old-hires`` attribute (full-resolution source) over
    ``src``; the rating comes from the ``#acrPopover`` "X out of 5 stars"
    title and the count from ``#acrCustomerReviewText`` ("1,234 ratings").
    """
    try:
        tree = HTMLParser(html or "")
    except Exception:
        return {"cover_url": "", "description": "", "stars": "", "ratings": ""}

    cover_url = ""
    for sel in COVER_SELECTORS:
        node = tree.css_first(sel)
        if node is None:
            continue
        attrs = node.attributes or {}
        cover_url = (attrs.get("data-old-hires") or attrs.get("src") or "").strip()
        if cover_url:
            break

    description = ""
    for sel in DESCRIPTION_SELECTORS:
        node = tree.css_first(sel)
        if node is not None:
            description = _collapse_ws(node.text(strip=True))
            if description:
                break

    stars = ""
    ratings = ""
    try:
        node = tree.css_first("#acrPopover") or tree.css_first("[title*='out of 5 stars']")
        if node is not None:
            title = (node.attributes or {}).get("title", "")
            m = re.search(r"([0-5](?:\.[0-9])?)\s*out of 5", title)
            if m:
                stars = m.group(1)
        node = tree.css_first("#acrCustomerReviewText")
        if node is not None:
            m = re.search(r"([0-9][0-9,]*)", node.text(strip=True) or "")
            if m:
                ratings = m.group(1)
    except Exception:
        stars, ratings = "", ""

    return {"cover_url": cover_url, "description": description,
            "stars": stars, "ratings": ratings}


def download_cover(
    url: str,
    dest: str | Path,
    asin: str | None = None,
    fetcher=None,
) -> Path | None:
    """Download a cover image into directory ``dest`` and return its Path.

    ``dest`` is the covers directory (created when missing). The filename is
    ``<asin>.<ext>`` when ``asin`` is given (one file per book, matching the
    deal row's ``asin``), else a sanitised URL basename; ``<ext>`` is derived
    from the URL path (``.jpg`` default). ``fetcher`` is an injectable
    ``url -> bytes`` callable (used by tests); when ``None`` an httpx client
    does the GET (lazy import, browser-like headers). Best-effort: never
    raises — logs and returns ``None`` on any failure so a cover miss can
    never take a verification run down.
    """
    try:
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        if asin:
            name = f"{asin}{_cover_ext(url)}"
        else:
            base = os.path.basename(urlsplit(url).path) or "cover"
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:80] or "cover"
        if fetcher is None:
            with httpx.Client(headers=HEADERS, timeout=30) as client:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.content
        else:
            data = fetcher(url)
        if not data:
            log.warning("cover download: empty response for %s", url)
            return None
        path = dest / name
        path.write_bytes(data)
        return path
    except Exception as exc:
        log.warning("cover download failed (%s): %s", url, exc)
        return None


def _save_diagnostic(label: str, url: str, body: str):
    """Dump ``body`` to ``data/diagnostics/<ts>_<label>.html`` (repo convention)."""
    try:
        import datetime
        from pathlib import Path

        DIAG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9]+", "_", label)[:60]
        path = DIAG_DIR / f"{stamp}_{safe}.html"
        path.write_text(f"<!-- url: {url} -->\n{body}", encoding="utf-8", errors="ignore")
        return path
    except Exception as exc:  # diagnostics must never take the run down
        log.warning("could not save diagnostic %s: %s", label, exc)
        return None


def _price_text_of(node) -> str | None:
    """A readable price from one price-container node, else ``None``.

    Prefers the ``.a-offscreen`` text (what screen readers announce). When that
    is blank — which modern Amazon does for the buybox price — reconstructs the
    number from ``.a-price-whole`` + ``.a-price-fraction``.
    """
    off = node.query_selector(".a-offscreen")
    if off is not None:
        text = (off.inner_text() or "").strip()
        if parse_price_cents(text) is not None:
            return text
    whole = node.query_selector(".a-price-whole")
    if whole is not None:
        w = re.sub(r"\s+", "", whole.inner_text() or "").rstrip(".")
        if w:
            frac_node = node.query_selector(".a-price-fraction")
            frac = re.sub(r"\s+", "", frac_node.inner_text() or "") if frac_node is not None else ""
            return f"${w}.{frac}" if frac else f"${w}"
    return None


def _price_from_selectors(page) -> str | None:
    """The current price text, or ``None``.

    Tries the buybox / apex price containers first (read ``a-offscreen``, else
    rebuild from the whole/fraction digits), then falls back to the first
    price-looking ``span.a-offscreen`` anywhere on the page.
    """
    for sel in _PRICE_CONTAINERS:
        try:
            nodes = page.query_selector_all(sel)
        except Exception:
            continue
        for node in nodes:
            try:
                text = _price_text_of(node)
            except Exception:
                continue
            if text:
                return text
    try:
        nodes = page.query_selector_all("span.a-offscreen")
    except Exception:
        return None
    for node in nodes:
        try:
            text = (node.inner_text() or "").strip()
        except Exception:
            continue
        if parse_price_cents(text) is not None:
            return text
    return None


def _llm_price(page, model: str) -> str | None:
    """Ask the local gateway for the page's price; ``None`` on any failure."""
    try:
        text = page.inner_text("body")[:_LLM_TEXT_CAP]
    except Exception:
        return None
    if not text.strip():
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
    }
    base = LLM_BASE_URL.rstrip("/")
    # Tolerate both ".../v1" and ".../v1/" (mirrors scripts/build_bookbub_deals.py).
    endpoint = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
    try:
        with httpx.Client(timeout=LLM_TIMEOUT) as client:
            resp = client.post(endpoint, json=payload)
            resp.raise_for_status()
            answer = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("LLM price fallback failed (LLM off/errored): %s", exc)
        return None
    answer = (answer or "").strip()
    if not answer or answer.upper() == "NONE":
        return None
    # Keep the LLM's answer as the price_text; parse_price_cents understands
    # "$4.99", "FREE"/"free ..." (-> 0) and bare "0".
    if parse_price_cents(answer) is None:
        return None
    return answer


def read_page(page, url: str, *, model: str | None = None, asin: str | None = None) -> PriceResult:
    """Read the price from an already-open Playwright ``page`` (no browser mgmt).

    ``model`` pins the LLM model (``None`` = use ``LLM_MODEL``). Never raises
    for a page that loads; a failed/ambiguous read returns a result with
    ``price_text=None`` and a ``note``.
    """
    label = f"amazon_{asin}" if asin else "amazon_page"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
    except Exception as exc:
        # Navigation itself failed (network/DNS/timeout): report, don't raise.
        return PriceResult(note=f"navigation failed: {exc}")

    html = page.content()
    kind = _classify_block_page(html)
    if kind is not None:
        _save_diagnostic(label, url, html)
        return PriceResult(note=f"blocked: {_BLOCK_KIND_LABEL[kind]}")

    # Small pause so any client-side price rendering settles.
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass

    price_text = _price_from_selectors(page)
    if price_text is not None:
        return PriceResult(
            price_text=price_text,
            price_cents=parse_price_cents(price_text),
            source="selector",
            note="",
        )

    chosen_model = model if model is not None else LLM_MODEL
    if chosen_model:
        llm_text = _llm_price(page, chosen_model)
        llm_cents = parse_price_cents(llm_text) if llm_text else None
        if llm_text is not None and llm_cents is not None:
            return PriceResult(
                price_text=llm_text,
                price_cents=llm_cents,
                source="llm",
                note="",
            )
        _save_diagnostic(label, url, page.content())
        return PriceResult(note="no price: selectors empty, LLM fallback unhelpful")

    _save_diagnostic(label, url, page.content())
    return PriceResult(note="no price found (selectors empty; LLM off)")


def read(
    amazon_url: str | None,
    *,
    fingerprint: Fingerprint,
    headless: bool = True,
    model: str | None = None,
    asin: str | None = None,
    browser=None,
) -> PriceResult:
    """Open the product page and read its current price.

    Launches Playwright + a context built from ``fingerprint`` unless a
    pre-built ``browser`` is supplied (so the orchestrator can share one
    browser across books and only rotate the context). Never raises for a
    page that loads; returns a :class:`PriceResult`.
    """
    url = build_dp_url(amazon_url, asin)
    if url is None:
        return PriceResult(note="no ASIN to fetch")

    owns_browser = browser is None
    pw = None
    if owns_browser:
        from playwright.sync_api import sync_playwright  # lazy: optional dep

        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=headless, args=LAUNCH_ARGS)

    ctx = None
    try:
        ctx = browser.new_context(
            user_agent=fingerprint.user_agent,
            locale=fingerprint.locale,
            viewport={"width": fingerprint.width, "height": fingerprint.height},
        )
        page = ctx.new_page()
        return read_page(page, url, model=model, asin=asin or asin_from_amazon_url(amazon_url))
    except Exception as exc:
        return PriceResult(note=f"browser error: {exc}")
    finally:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass
        if owns_browser:
            try:
                browser.close()
            except Exception:
                pass
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass


def _main() -> int:
    """``python -m app.amazon_price <amazon-url|ASIN>`` — one-page live probe."""
    import argparse
    import logging as _logging

    from .fingerprint import next_fingerprint

    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Read the current Amazon price for one ASIN/URL.")
    ap.add_argument("target", help="An amazon.com /dp/ URL or a bare ASIN")
    ap.add_argument("--model", default=None, help="LLM model (default: LLM_MODEL env)")
    args = ap.parse_args()

    asin = asin_from_amazon_url(args.target)
    bare_asin = args.target if (asin is None and re.fullmatch(r"[A-Z0-9]{10}", args.target)) else None
    result = read(args.target, fingerprint=next_fingerprint(), model=args.model, asin=asin or bare_asin)
    print(result)
    return 0 if result.price_cents is not None else 1


if __name__ == "__main__":
    raise SystemExit(_main())
