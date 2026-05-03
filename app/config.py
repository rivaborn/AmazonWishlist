import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("WISHLIST_DB", DATA_DIR / "wishlist.db"))
LOG_PATH = Path(os.environ.get("WISHLIST_LOG", DATA_DIR / "scrape.log"))

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
