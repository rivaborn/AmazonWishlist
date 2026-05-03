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
