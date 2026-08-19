import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("WISHLIST_DB", DATA_DIR / "wishlist.db"))
LOG_PATH = Path(os.environ.get("WISHLIST_LOG", DATA_DIR / "scrape.log"))

# Scrape progress is mirrored to this file on every step so an interrupted run
# (e.g. the service restarted mid-scrape by an OS security upgrade via
# needrestart) can be detected and resumed on the next startup.
PROGRESS_PATH = Path(os.environ.get("WISHLIST_PROGRESS", DATA_DIR / "scrape_progress.json"))

# Don't auto-resume an interrupted run older than this (seconds). Guards against
# resuming a stale run after a long outage; the daily cron covers that instead.
RESUME_MAX_AGE_SEC = int(os.environ.get("WISHLIST_RESUME_MAX_AGE", str(24 * 3600)))

PORT = int(os.environ.get("WISHLIST_PORT", "9060"))

SCRAPE_HOUR = int(os.environ.get("WISHLIST_SCRAPE_HOUR", "3"))
SCRAPE_MINUTE = int(os.environ.get("WISHLIST_SCRAPE_MINUTE", "0"))

# Minimum seconds between the start of one wishlist and the start of the next
# during a single scrape run. 3600 = at most one wishlist per hour, which keeps
# us under Amazon's bot threshold across a multi-list account.
SCRAPE_PER_WISHLIST_SECONDS = int(os.environ.get("WISHLIST_PER_LIST_SECONDS", "3600"))

USER_AGENT = os.environ.get(
    "WISHLIST_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

REQUEST_DELAY_MIN = float(os.environ.get("WISHLIST_DELAY_MIN", "4.0"))
REQUEST_DELAY_MAX = float(os.environ.get("WISHLIST_DELAY_MAX", "9.0"))
REQUEST_TIMEOUT = float(os.environ.get("WISHLIST_TIMEOUT", "20"))

# ---------- Pagination bounds (see scraper._paginate_bounds) ----------

# Hard cap on pages fetched for a single wishlist. Reaching it means we stopped
# because we ran out of budget, not because Amazon ran out of list -- which is
# partial pagination, so the scrapers raise FetchFailed rather than ingest a
# possibly-truncated set.
MAX_PAGES_PER_WISHLIST = int(os.environ.get("WISHLIST_MAX_PAGES", "100"))

# Amazon keeps minting fresh `paginationToken`s past the end of a wishlist, each
# re-serving rows we already hold, so "no next link" never arrives and the
# dedupe-by-ASIN quietly absorbs the repeats. Stop after this many consecutive
# pages that add nothing new: that is the real end-of-list signal. Without it a
# 520-item list burns all 100 pages every night -- ~75 of them pure duplicates,
# which is request volume spent buying anti-bot blocks.
MAX_STALE_PAGES = int(os.environ.get("WISHLIST_MAX_STALE_PAGES", "3"))

# ---------- Ingest guards ----------

# Refuse to replace a wishlist's membership when a scrape comes back with less
# than this fraction of the previous count -- one short scrape wipes the missing
# items until the next good run. A shrink confirmed by a SECOND consecutive
# short scrape is accepted (a list really can be pruned), so nothing strands.
INGEST_SHRINK_FLOOR = float(os.environ.get("WISHLIST_SHRINK_FLOOR", "0.8"))

# A wishlist whose last successful scrape is older than this reads as stale in
# the UI. Scrape failures deliberately leave `previous_item_count` and the item
# count untouched, so a blocked list keeps showing a healthy matching pair --
# `last_scraped_at` is the only honest column, and this makes it say so.
STALE_AFTER_HOURS = float(os.environ.get("WISHLIST_STALE_AFTER_HOURS", "26"))

# ---------- Playwright (logged-in scrape via secondary Amazon account) ----------

STORAGE_STATE = Path(os.environ.get("WISHLIST_STORAGE_STATE", DATA_DIR / "storage_state.json"))
CHROMIUM_USER_DATA_DIR = Path(
    os.environ.get("WISHLIST_CHROMIUM_USER_DATA", DATA_DIR / ".chrome-login")
)
PLAYWRIGHT_HEADLESS = os.environ.get("WISHLIST_PLAYWRIGHT_HEADLESS", "1") not in ("0", "false", "False", "")


def use_playwright() -> bool:
    """Auto-detect: use Playwright if a non-trivial storage_state file exists.

    Re-evaluated on every call so removing or adding the file flips paths
    without requiring a service restart (next scrape picks the new path).
    """
    try:
        return STORAGE_STATE.is_file() and STORAGE_STATE.stat().st_size > 200
    except OSError:
        return False


# ---------- In-app login (noVNC + headful Chromium under Xvfb) ----------

VNC_PORT = int(os.environ.get("WISHLIST_VNC_PORT", "6080"))
LOGIN_IDLE_TIMEOUT_SEC = int(os.environ.get("WISHLIST_LOGIN_IDLE_TIMEOUT", "600"))
XVFB_DISPLAY = os.environ.get("WISHLIST_XVFB_DISPLAY", ":99")
XVFB_RESOLUTION = os.environ.get("WISHLIST_XVFB_RESOLUTION", "1280x800x24")
NOVNC_WEB_DIR = Path(os.environ.get("WISHLIST_NOVNC_DIR", "/usr/share/novnc"))
