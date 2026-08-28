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

# 00:00 rather than an early-morning hour on purpose: Amazon's blocks cluster by
# hour and the midnight slot is the quietest we have measured, and starting at
# 00:00 leaves the whole day of headroom before the mirror's daily sync (see
# SYNC_HOUR) for a paced run of one wishlist per hour.
SCRAPE_HOUR = int(os.environ.get("WISHLIST_SCRAPE_HOUR", "0"))
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

# ---------- Blocked-page retries ----------

# Amazon's 503 "Dogs of Amazon" page is a transient (its own copy says "go
# back and try again"), so the same page URL is retried with a polite backoff
# before the list is failed for the day. The anti-automation stub is a
# verdict, not a transient -- it gets at most ONE retry and only mid-list
# (page 1 stub = refused at the door, retrying it is how accounts get banned).
BLOCK_RETRY_503 = int(os.environ.get("WISHLIST_503_RETRIES", "2"))
BLOCK_RETRY_STUB_MIDLIST = int(os.environ.get("WISHLIST_STUB_RETRIES", "1"))
# Base backoff seconds; attempt N waits N*base + jitter. Worst case per list
# (~2 retries) adds ~5 min, well inside the hourly pacing slot.
BLOCK_RETRY_BACKOFF = float(os.environ.get("WISHLIST_BLOCK_BACKOFF", "90"))

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


# ---------- Role: primary scrapes, secondary mirrors (see README "Two-instance mirror") ----------

# Only ONE instance may scrape Amazon. Two instances hitting the same throwaway
# account from two IPs is exactly the traffic pattern the pacing and anti-bot
# guards exist to avoid, so the secondary pulls the primary's data over HTTP
# instead. `primary` is the default, so existing single-host installs are
# unaffected by this file growing a role.
ROLE = os.environ.get("WISHLIST_ROLE", "primary").strip().lower()


def is_secondary() -> bool:
    """True when this instance mirrors another rather than scraping.

    Read through a function against the module global (same shape as
    `use_playwright()` above) so tests can flip the role at runtime instead of
    having to re-import the whole app.
    """
    return ROLE == "secondary"


# Base URL of the primary, e.g. http://192.168.50.43:9060 -- must name the port
# the primary actually serves on (the systemd unit hardcodes --port 9060).
PRIMARY_URL = os.environ.get("WISHLIST_PRIMARY_URL", "").strip().rstrip("/")

# Daily, in server-local time, like the primary's scrape cron -- deliberately
# AFTER the primary has finished its run, so one pull picks up the whole night.
# The primary starts its run at SCRAPE_HOUR and paces one wishlist per
# SCRAPE_PER_WISHLIST_SECONDS, measured from the PREVIOUS list's start -- so the
# last of N lists begins at SCRAPE_HOUR + (N-1) hours. This must sit past that,
# or the lists scraped last stay a day behind on the mirror. With a 00:00 run
# start, 08:00 covers up to 8 wishlists.
SYNC_HOUR = int(os.environ.get("WISHLIST_SYNC_HOUR", "8"))
SYNC_MINUTE = int(os.environ.get("WISHLIST_SYNC_MINUTE", "0"))

# Not REQUEST_TIMEOUT: that one is 20s and tuned for Amazon. A snapshot page off
# a LAN peer is a bigger, slower response from a friendlier host.
SYNC_TIMEOUT = float(os.environ.get("WISHLIST_SYNC_TIMEOUT", "60"))

# Snapshot rows per request. They travel as positional arrays (~85 bytes/row vs
# ~170 as objects) and are the entire bulk of the transfer. The MAX is a
# server-side clamp: unclamped, `?limit=99999999` is a trivial way to exhaust
# memory on a box that also runs Chromium.
SYNC_PAGE_LIMIT = int(os.environ.get("WISHLIST_SYNC_PAGE_LIMIT", "2000"))
SYNC_PAGE_LIMIT_MAX = 10000

# Advisory telemetry only -- the sync cursor is MAX(price_snapshot.id) in the DB,
# never this file. See app/sync.py.
SYNC_STATE_PATH = Path(os.environ.get("WISHLIST_SYNC_STATE", DATA_DIR / "sync_state.json"))


# ---------- Grimmory book catalog (one-off build of data/grimmory.db) ----------

# Host serving the BookLore/Grimmory instance (the home-lab book library
# manager at 192.168.1.13:6060). Kept as-given (trailing slash included); the
# client strips/normalises when joining API paths.
GRIMMORY_URL = os.environ.get("GRIMMORY_URL", "http://192.168.1.13:6060/")

# Login user for /api/v1/auth/login. No defaults for the credentials: the
# password is a real secret and must be supplied at run time via the
# environment, never committed in this file.
GRIMMORY_USERNAME = os.environ.get("GRIMMORY_USERNAME", "").strip()
GRIMMORY_PASSWORD = os.environ.get("GRIMMORY_PASSWORD", "")

# Comma-separated names of the Grimmory libraries to export into the book DB.
GRIMMORY_LIBRARIES = os.environ.get("GRIMMORY_LIBRARIES", "Amazon fksogbetun,Amazon rivaborn")

# Standalone one-off book catalog DB. Deliberately separate from wishlist.db
# (different concern, different schema) and gitignored via data/.
GRIMMORY_DB = Path(os.environ.get("GRIMMORY_DB", DATA_DIR / "grimmory.db"))
