# Amazon Wishlist Deal Tracker

Self-hosted FastAPI app that watches your **public** Amazon ebook wishlists and shows deals, missing-price items, and price-drop history on a small web UI at port 9060.

## How it works

- Scrapes each registered wishlist URL once a day at 08:00 server-local time (also on demand via the "Run scrape now" button).
- Stores every observation as a snapshot in SQLite, so price-drop math works against either the previous observed price or Amazon's list/strikethrough price.
- Collapses duplicate ASINs across wishlists.
- Only shows books that are *currently* on a wishlist.

## Prerequisites

Each wishlist must be set to **Public** on Amazon:

1. Open the wishlist on amazon.com.
2. Click the three-dot menu → **Manage list**.
3. Set "Privacy" to **Public** and copy the share URL.
4. The URL should look like `https://www.amazon.com/hz/wishlist/ls/XXXXXXXX`.

The app does **not** log into Amazon. If you keep your wishlist private it cannot be tracked.

## Local development (Windows or Linux)

```bash
python -m venv .venv
.venv/Scripts/activate     # Windows
# source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
uvicorn app.main:app --reload --port 9060
```

Open <http://localhost:9060/wishlists>, paste a public wishlist URL, click **Add**, then **Run scrape now**.

## Production deploy on Ubuntu

```bash
sudo bash scripts/install_systemd.sh
```

The script:

- Creates a `wishlist` system user.
- Copies the repo to `/opt/amazon-wishlist`.
- Builds a venv, installs deps.
- Installs and starts the `amazon-wishlist.service` systemd unit.

Check status / logs:

```bash
systemctl status amazon-wishlist
journalctl -u amazon-wishlist -f
tail -f /opt/amazon-wishlist/data/scrape.log
```

## Configuration (env vars)

| var | default | meaning |
| --- | --- | --- |
| `WISHLIST_PORT` | `9060` | HTTP port |
| `WISHLIST_DB` | `data/wishlist.db` | SQLite path |
| `WISHLIST_LOG` | `data/scrape.log` | rotating scrape log |
| `WISHLIST_SCRAPE_HOUR` | `8` | daily cron hour (server local) |
| `WISHLIST_SCRAPE_MINUTE` | `0` | daily cron minute |
| `WISHLIST_DELAY_MIN` / `WISHLIST_DELAY_MAX` | `1.5` / `3.0` | jittered delay between requests, seconds |
| `WISHLIST_USER_AGENT` | Chrome 124 | UA string sent to Amazon |

## Pages

- **/deals** — books on a wishlist whose latest snapshot is below baseline by ≥ filters.
- **/no-price** — split into "Kindle edition unavailable" and "Removed from Amazon".
- **/price-drops** — every historical snapshot that dropped vs. its baseline, filtered.
- **/wishlists** — add/remove wishlist URLs, run scrape on demand.

Filters on each page: minimum dollar drop, minimum percent drop, basis (vs. previous price or vs. list price).

## Notes / limitations

- Amazon's HTML changes occasionally; if scrapes start returning 0 items, check the selectors in `app/scraper.py`.
- This is a single-user app; there's no auth on the web UI. Don't expose it to the public internet without a reverse proxy + auth.
