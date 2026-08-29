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


# ---------- BookBub daily ebook deals (one-off export to booklist.md) ----------

# The daily BookBub email carries an outbound.bookbub.com link that auto-logs
# the recipient into bookbub.com (signed, single-purpose). It is a session
# token that rotates, so it is supplied at run time (--link / env) and must
# never be committed here.
BOOKBUB_LOGIN_LINK = os.environ.get("BOOKBUB_LOGIN_LINK", "")

# BookBub account credentials (primary login for the daily updater). Like the
# Grimmory credentials above, these are real secrets read from the environment
# at run time (on the host via /etc/default/amazon-wishlist) and must never be
# committed here. Empty value means "not configured".
BOOKBUB_USERNAME = os.environ.get("BOOKBUB_USERNAME", "").strip()
BOOKBUB_PASSWORD = os.environ.get("BOOKBUB_PASSWORD", "")

# Where the daily deals live. The selected day is a ?date=YYYYMMDD query arg.
BOOKBUB_DAILY_DEALS_BASE = os.environ.get(
    "BOOKBUB_DAILY_DEALS_BASE",
    "https://www.bookbub.com/ebook-deals/daily-deals",
)

# strftime format for the ?date= value (BookBub uses bare YYYYMMDD).
BOOKBUB_DATE_FORMAT = os.environ.get("BOOKBUB_DATE_FORMAT", "%Y%m%d")

# Where the markdown report is written. Repo root by default (the deals file is
# a daily generated report, not app data). The task's "AmazonWhishlist" target
# path is a typo for this repo folder, so it lands at BASE_DIR/booklist.md.
BOOKBUB_OUTPUT = Path(os.environ.get("BOOKBUB_OUTPUT", BASE_DIR / "booklist.md"))

# Deals database: one row per BookBub deal (the day's page), plus the resolved
# Amazon Kindle link (when the deal has one), an owned-in-grimmory flag, and
# audit notes. Deliberately separate from wishlist.db and grimmory.db and
# gitignored via data/. Re-runs upsert per date; history is kept for audit.
DEALS_DB = Path(os.environ.get("DEALS_DB", DATA_DIR / "deals.db"))

# Book cover images captured during live verification (scripts/verify_deals.py,
# which the daily updater invokes): one file per book named <ASIN>.<ext>,
# stored locally under data/ (gitignored) and served by the web app at
# /covers/<name>.
DEALS_COVERS_DIR = Path(os.environ.get("DEALS_COVERS_DIR", DATA_DIR / "covers"))

# Live-deal verification (scripts/verify_deals.py): the deal_status values
# written into the `deal` table when we check a book's current Amazon price
# against the stored deal_price. See app.deals_db.classify_deal.
DEAL_STATUS_CURRENT = "current"
DEAL_STATUS_EXPIRED = "expired"
DEAL_STATUS_UNKNOWN = "unknown"

# Backfill of historical daily deals (scripts/backfill_bookbub_deals.py): walk
# [BOOKBUB_BACKFILL_END .. BOOKBUB_BACKFILL_START] day by day and store each
# day's deals in DEALS_DB. Dates are already recorded (DEALS_DB rows or the
# progress file) are skipped, so re-runs resume where a killed run stopped.
BOOKBUB_BACKFILL_START = os.environ.get("BOOKBUB_BACKFILL_START", "20260826")
BOOKBUB_BACKFILL_END = os.environ.get("BOOKBUB_BACKFILL_END", "20260613")
# Dates per login session before a fresh login (bounds session length and the
# number of pages one Cloudflare session has to absorb).
BOOKBUB_BACKFILL_CHUNK = int(os.environ.get("BOOKBUB_BACKFILL_CHUNK", "5"))
# Seconds between date navigations inside one session (jittered ±25%).
BOOKBUB_BACKFILL_DELAY = float(os.environ.get("BOOKBUB_BACKFILL_DELAY", "3"))
# Seconds to sleep between chunks (between login sessions); doubled when
# Cloudflare re-challenges are detected, to let the block cool down.
BOOKBUB_BACKFILL_BACKOFF = float(os.environ.get("BOOKBUB_BACKFILL_BACKOFF", "30"))
# Per-date status mirror for resume (atomic writes); gitignored via data/.
BOOKBUB_BACKFILL_PROGRESS = Path(
    os.environ.get("BOOKBUB_BACKFILL_PROGRESS", DATA_DIR / "backfill_progress.json")
)


# ---------- Optional local LLM (normalisation of the parsed deal list) ----------

# The homelab LLMConfig gateway, OpenAI-compatible. Off by default: the
# deterministic selectolax parse is the deliverable, and an LLM pass must never
# block the write when a model is absent or the gateway is unreachable.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://192.168.1.40:11430/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "").strip()
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))

# ---------- Amazon price reader (app/amazon_price.py) ----------
# How many chars of a product page's innerText to hand to the LLM price
# fallback, and the navigation timeout (ms) before giving up on one book.
AMAZON_LLM_TEXT_CAP = int(os.environ.get("AMAZON_LLM_TEXT_CAP", "6000"))
AMAZON_NAV_TIMEOUT_MS = int(os.environ.get("AMAZON_NAV_TIMEOUT_MS", "45000"))

# ---------- NordVPN CLI wrapper (app/nordvpn.py) ----------
# Path to the `nordvpn` CLI (override with NORDVPN_CLI if it is not on PATH).
# Credentials are read ONLY from the NORDVPN_USERNAME / NORDVPN_PASSWORD env
# (or --nord-user / --nord-pass) — never from a committed file or a default here.
# NORDVPN_COUNTRIES is the pool of exit countries `rotate()` cycles through so
# each rotation lands on a different server/exit IP; NORDVPN_CITIES (optional)
# adds a per-rotation city for finer variation.
NORDVPN_CLI = os.environ.get("NORDVPN_CLI", "nordvpn")
NORDVPN_COUNTRIES = [
    c.strip()
    for c in os.environ.get(
        "NORDVPN_COUNTRIES",
        "United States,Germany,Japan,United Kingdom,Canada,Australia",
    ).split(",")
    if c.strip()
]
NORDVPN_CITIES = [
    c.strip() for c in os.environ.get("NORDVPN_CITIES", "").split(",") if c.strip()
]
# Starting exit country for scripts/verify_deals.py (first of the pool by default).
NORDVPN_START_COUNTRY = os.environ.get(
    "NORDVPN_START_COUNTRY", NORDVPN_COUNTRIES[0] if NORDVPN_COUNTRIES else "United States"
)
# Books per exit IP / fingerprint pair before a NordVPN rotation (the
# scripts/verify_deals.py --rotate-every default).
NORDVPN_ROTATE_EVERY = int(os.environ.get("NORDVPN_ROTATE_EVERY", "10"))

# ---------- NordVPN netns tunnel (app/nordvpn.py "tunnel mode") ----------
# The Ubuntu deployment puts the live-deal verifier INSIDE a network namespace
# whose only route is a NordLynx (WireGuard) tunnel — scripts/vpn_netns_up.sh /
# vpn_netns_down.sh + amazon-wishlist-vpn.service (see README). These knobs
# must match the tunnel unit's /etc/default/amazon-wishlist values so
# `verify_deals.py --netns <NS>` addresses the same namespace the unit builds.
# No credentials here: the session is pre-negotiated by the tunnel unit as
# WISHLIST_VPN_USER (the nordvpn CLI's operator user).
WISHLIST_VPN_NS = os.environ.get("WISHLIST_VPN_NS", "wlvpn")
WISHLIST_VPN_IFACE = os.environ.get("WISHLIST_VPN_IFACE", "wlwg")
WISHLIST_VPN_UNIT = os.environ.get("WISHLIST_VPN_UNIT", "amazon-wishlist-vpn.service")
# Where the egress checks point (must be reachable through the tunnel's DNS).
WISHLIST_VPN_ENDPOINT = os.environ.get(
    "WISHLIST_VPN_ENDPOINT", "https://api.ipify.org"
)

# ---------- Live-deal verification pacing (scripts/verify_deals.py) ----------
# Random jitter (seconds) between per-book Amazon reads (anti-bot pacing, the
# same idea as the wishlist scraper's REQUEST_DELAY_MIN/MAX), the per-book retry
# budget for transient failures (block page / navigation failure / no price),
# and the backoff sleep between retries.
VERIFY_DELAY_MIN = float(os.environ.get("VERIFY_DELAY_MIN", "2"))
VERIFY_DELAY_MAX = float(os.environ.get("VERIFY_DELAY_MAX", "6"))
VERIFY_MAX_RETRIES = int(os.environ.get("VERIFY_MAX_RETRIES", "2"))
VERIFY_RETRY_BACKOFF = float(os.environ.get("VERIFY_RETRY_BACKOFF", "20"))
# Tunnel-mode liveness gate (scripts/verify_deals.py _tunnel_live): how many
# egress checks (with VERIFY_TUNNEL_RETRY_DELAY seconds between) the fail-closed
# gate runs before aborting on a lost tunnel. A transient Nord rekey/reconnect
# blip must not abort a many-hour run, but no Amazon access happens while egress
# is down (the gate blocks before every read). Defaults ~60s total.
VERIFY_TUNNEL_RETRIES = int(os.environ.get("VERIFY_TUNNEL_RETRIES", "4"))
VERIFY_TUNNEL_RETRY_DELAY = float(os.environ.get("VERIFY_TUNNEL_RETRY_DELAY", "15"))
# Advisory progress mirror (atomic tmp+replace telemetry). The source of truth
# for "what is pending" is deal_status in DEALS_DB, never this file.
VERIFY_PROGRESS = Path(os.environ.get("VERIFY_PROGRESS", DATA_DIR / "verify_progress.json"))
