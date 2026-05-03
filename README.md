# Amazon Wishlist Deal Tracker

Self-hosted FastAPI app that watches Amazon ebook wishlists and shows deals, the full catalog, missing-price items, and price-drop history on a small web UI at port 9060.

## How it works

Two scraper modes, selected automatically based on whether a saved login session exists:

- **Anonymous (httpx)** — works on any **public** wishlist URL, no login. Default; gets IP-throttled by Amazon on accounts with many lists.
- **Authenticated (Playwright + headless Chromium)** — uses a saved Amazon session from a separate, throwaway account (logged-in scraping bypasses the throttling that hits anonymous requests). Login happens **inside the wiki UI** via the Login tab — server runs a headful Chromium under Xvfb and streams it to your browser via noVNC; you click through Amazon's real login page in an iframe.

Other behaviour:

- Scrapes each registered wishlist URL once a day at 03:00 server-local time, **at most one wishlist per hour** to stay under Amazon's bot-detection threshold (also on demand via the "Run scrape now" button — same pacing applies).
- Stores every observation as a snapshot in SQLite, so price-drop math works against either the previous observed price or Amazon's list/strikethrough price.
- Collapses duplicate ASINs across wishlists.
- Only shows books that are *currently* on a wishlist.
- Detects Amazon's anti-automation stub page; if a wishlist is bot-blocked, the previous successful state is preserved (no clobbering with 0 items). Same protection for HTTP errors and partial pagination failures.
- Detects logged-out state when running authenticated; surfaces "login expired — open Login tab and re-authenticate" via the progress UI without clobbering data.

## Prerequisites

For anonymous (httpx) scraping, each wishlist must be set to **Public** on Amazon:

1. Open the wishlist on amazon.com.
2. Click the three-dot menu → **Manage list**.
3. Set "Privacy" to **Public** and copy the share URL.
4. The URL should look like `https://www.amazon.com/hz/wishlist/ls/XXXXXXXX`.

For authenticated (Playwright) scraping, the wishlists can be private as long as the secondary account you use to log in has access to them.

## Local development (Windows or Linux)

```bash
python -m venv .venv
.venv/Scripts/activate     # Windows
# source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
uvicorn app.main:app --reload --port 9060
```

Open <http://localhost:9060/wishlists>, paste a public wishlist URL, click **Add**, then **Run scrape now**.

For a quick smoke test (no network — uses fake scraped items): `python scripts/_smoke.py`.

## Production deploy on Ubuntu

```bash
sudo bash scripts/install_systemd.sh
```

The script is **idempotent** — re-run it after every code change and it will rsync new files into `/opt/amazon-wishlist`, refresh the venv, and `systemctl restart` the unit. The SQLite DB and `diagnostics/` folder under `/opt/amazon-wishlist/data/` are preserved.

What it does on first run:

- Creates a `wishlist` system user.
- `apt-get install`s the Login-tab infra (`xvfb`, `x11vnc`, `websockify`, `novnc`).
- Copies the repo to `/opt/amazon-wishlist`.
- Builds a venv (with an `ensurepip` fallback for Ubuntu builds where `python3 -m venv` skips pip).
- Installs Python deps and runs `playwright install --with-deps chromium` to pull the browser binary + its system runtime libraries.
- Installs and starts the `amazon-wishlist.service` systemd unit.

### Standard deploy loop (after a code change)

```bash
cd ~/AmazonWishlist
git pull
sudo bash scripts/install_systemd.sh
```

### Status / logs

```bash
systemctl status amazon-wishlist
journalctl -u amazon-wishlist -f
sudo tail -f /opt/amazon-wishlist/data/scrape.log
```

If a scrape returned 0 items for a list and you want to see *why*, look in `/opt/amazon-wishlist/data/diagnostics/` — the scraper saves the raw HTML of any page that yielded zero rows or hit the anti-bot stub.

## Configuration (env vars)

Set in `amazon-wishlist.service` under `Environment=` if you need to override.

| var | default | meaning |
| --- | --- | --- |
| `WISHLIST_PORT` | `9060` | HTTP port |
| `WISHLIST_DB` | `data/wishlist.db` | SQLite path |
| `WISHLIST_LOG` | `data/scrape.log` | rotating scrape log |
| `WISHLIST_SCRAPE_HOUR` | `3` | daily cron hour (server local) |
| `WISHLIST_SCRAPE_MINUTE` | `0` | daily cron minute |
| `WISHLIST_PER_LIST_SECONDS` | `3600` | minimum seconds between starting one wishlist and the next during a single run. Set to `0` to disable pacing for one-off testing. |
| `WISHLIST_DELAY_MIN` / `WISHLIST_DELAY_MAX` | `4.0` / `9.0` | jittered delay between page-level requests within a single wishlist scrape, seconds |
| `WISHLIST_TIMEOUT` | `20` | per-request HTTP timeout, seconds |
| `WISHLIST_USER_AGENT` | recent Chrome | UA string sent to Amazon |
| `WISHLIST_STORAGE_STATE` | `data/storage_state.json` | Playwright session file. Presence flips the scraper to authenticated mode automatically. |
| `WISHLIST_PLAYWRIGHT_HEADLESS` | `1` | Headless mode for the *scrape* (login is always headful). Set to `0` to debug. |
| `WISHLIST_VNC_PORT` | `6080` | Port the noVNC client binds to during a Login session. Closed when no session is active. |
| `WISHLIST_LOGIN_IDLE_TIMEOUT` | `600` | Seconds before an idle Login session is auto-cancelled. |
| `WISHLIST_XVFB_DISPLAY` | `:99` | Display number for the virtual X server during login. |
| `WISHLIST_XVFB_RESOLUTION` | `1280x800x24` | Geometry for the virtual display. |
| `WISHLIST_NOVNC_DIR` | `/usr/share/novnc` | Where the apt `novnc` package lays out its HTML/JS. |

## Pages

- **/deals** — books on a wishlist whose latest snapshot is below baseline by ≥ filters. Filter by minimum dollar drop, minimum percent drop, and basis (vs. previous observed price or vs. list/strikethrough price).
- **/books** — every available book across all wishlists, sorted by current price ascending. Header shows total count, lowest, and highest.
- **/no-price** — split into "Kindle edition unavailable" and "Removed from Amazon" (HTTP 404).
- **/price-drops** — every historical snapshot that dropped vs. its baseline, filtered.
- **/wishlists** — add/remove wishlist URLs, run scrape on demand. Each row shows when it was last scraped and the item count from that scrape. The Run-scrape button shows a live progress bar and a "Waiting until HH:MM:SS" indicator between paced scrapes.
- **/login** — log in to the secondary Amazon account that the authenticated scraper uses. See "Authenticated scraping" below.

## Authenticated scraping (Playwright + Login tab)

When anonymous scraping is being IP-throttled by Amazon, switch to logged-in scraping by saving a session from a separate, throwaway Amazon account.

### Risk to your primary account

Low if you isolate the secondary properly. Don't reuse the same email, phone, payment method, or shipping address across the two accounts. Sign the secondary up from a different IP (phone hotspot is fine) so initial fingerprints don't overlap. Never log into your primary on this server. Worst realistic outcome: the secondary gets banned over time → make another. Primary stays intact.

### How to log in

1. Open `/login` in the wiki UI. Banner shows "No saved session" (or current age if you've logged in before).
2. Click **Start login session**. Server spawns:
   - `Xvfb` (virtual X display)
   - Headful Chromium driven by Playwright on that display
   - `x11vnc` bridging the display to a localhost VNC port
   - `websockify` wrapping the VNC port as a WebSocket and serving noVNC's web client at `:6080`
3. Within ~5–10 s the iframe shows Amazon's homepage. Sign into the **secondary** account, complete any 2FA / new-device verification, land on the homepage.
4. Click **Save session**. Server calls Playwright's `context.storage_state(path=…)` and writes `data/storage_state.json` (atomic, `0600 wishlist:wishlist`). All subprocesses are torn down.
5. Next scrape (manual button or 03:00 cron) auto-detects the file, logs `Scraper path: playwright`, and uses the logged-in session.

If you walk away mid-login, the session auto-cancels after `WISHLIST_LOGIN_IDLE_TIMEOUT` (default 10 min). The page sends heartbeats while you're using it, so it won't timeout while active.

### When to re-login

Amazon sessions last weeks to months. The scraper detects logged-out state on each run and surfaces "login expired — open Login tab and re-authenticate" via the progress UI without clobbering your data. When you see that, just re-do the login flow above; it overwrites `data/storage_state.json` with a fresh session.

### Going back to anonymous

```bash
sudo mv /opt/amazon-wishlist/data/storage_state.json{,.disabled}
sudo systemctl restart amazon-wishlist
```

Next scrape will log `Scraper path: httpx` and behave as before. Move the file back to switch on again.

## Scrape progress / status API

Two JSON endpoints back the wishlists page UI and can be polled by anything else:

- `POST /api/scrape/run` — starts a full scrape. If one is already running, returns `{"started": false, "progress": {...}}` instead of stacking a duplicate.
- `GET /api/scrape/status` — current progress. Shape:

  ```json
  {
    "running": true,
    "started_at": "2026-05-03T03:00:00.000000",
    "finished_at": null,
    "total": 7,
    "done": 2,
    "current_label": "Book List 3",
    "current_url": "https://www.amazon.com/hz/wishlist/ls/...",
    "items_total": 294,
    "error": null,
    "waiting": false,
    "next_starts_at": null
  }
  ```

  When `waiting` is `true`, the run is mid-pacing-gap and `next_starts_at` is the ISO timestamp the next wishlist will start.

## Data model

SQLite, file at `data/wishlist.db`. Schema is created/migrated on startup.

- `wishlist` — registered URLs (`url`, `label`, `added_at`, `last_scraped_at`).
- `book` — one row per ASIN ever seen (`title`, `author`, `product_url`, `first_seen`, `last_seen`).
- `wishlist_book` — many-to-many; rebuilt for a wishlist on each successful scrape, so removing an item from your Amazon wishlist drops it off `/deals` etc. but keeps its history.
- `price_snapshot` — append-only `(asin, observed_at, current_price_cents, list_price_cents, availability)`.

`availability` is `available` | `kindle_unavailable` | `page_404`.

## Notes / limitations

- Amazon actively rate-limits scrapers. The defaults (3 AM start, 1-hour pacing, 4–9 s per-page jitter, browser-like headers) are tuned to fly under the radar for accounts with a handful of wishlists totaling around 1,000 items. Larger accounts or noisier IPs may still see occasional bot-blocks; the app preserves the prior state when this happens and saves the offending HTML to `data/diagnostics/`.
- Amazon's HTML structure changes occasionally. If scrapes start returning 0 items *without* a "bot-blocked" status, check `data/diagnostics/` for the saved HTML and update the selectors in `app/scraper.py`.
- This is a single-user app; there is no auth on the web UI. Don't expose it to the public internet without a reverse proxy + auth in front.
