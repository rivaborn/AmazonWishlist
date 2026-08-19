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
| `WISHLIST_PROGRESS` | `data/scrape_progress.json` | scrape progress is mirrored here on every step; on startup an interrupted run is resumed from it (see "Resume after restart" below) |
| `WISHLIST_RESUME_MAX_AGE` | `86400` | max age (seconds) of an interrupted run that will be auto-resumed on startup; older ones are discarded and left for the next daily cron |
| `WISHLIST_DELAY_MIN` / `WISHLIST_DELAY_MAX` | `4.0` / `9.0` | jittered delay between page-level requests within a single wishlist scrape, seconds |
| `WISHLIST_TIMEOUT` | `20` | per-request HTTP timeout, seconds |
| `WISHLIST_MAX_PAGES` | `100` | hard cap on pages fetched per wishlist. Reaching it while Amazon still offers a next page is treated as **partial pagination** and raises `FetchFailed` — the result is a prefix, and ingesting a prefix would replace the whole membership with it. |
| `WISHLIST_MAX_STALE_PAGES` | `3` | stop paginating after this many consecutive pages that add no new ASINs. Amazon keeps minting fresh `paginationToken`s past the end of a list, each re-serving rows already held, so "no next link" never arrives on its own — this is the real end-of-list signal. Only pages that still HAVE rows count as end-of-list; the same number of consecutive **zero-row** pages raises `FetchFailed` instead (an empty page is a soft-block or selector drift, never how a list ends). |
| `WISHLIST_SHRINK_FLOOR` | `0.8` | refuse to replace a wishlist's membership when a scrape returns less than this fraction of the stored count. A shrink is accepted only when the next completed scrape is short too and agrees with the first (both counts within this same ratio of each other); a scrape failure in between resets the confirmation. |
| `WISHLIST_STALE_AFTER_HOURS` | `26` | a wishlist whose last **successful** scrape is older than this is flagged stale on `/wishlists`. 26 rather than 24 because one-list-per-hour pacing already spreads a run across ~7 h. A wishlist that has never been scraped successfully ages from `added_at`. |
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

## Troubleshooting (production)

### `ModuleNotFoundError: No module named 'uvicorn'` after a system restart

**Cause:** The system Python was upgraded (e.g. 3.13 → 3.14) while the venv was already built. The venv's `python3` symlink now resolves to the new interpreter, which looks for packages under `python3.14/site-packages/` while everything is installed under `python3.13/site-packages/`.

**Fix:** Delete the stale venv and re-run the install script (the script only creates a new venv if one doesn't exist, so you must delete it first):

```bash
sudo rm -rf /opt/amazon-wishlist/.venv
sudo bash ~/AmazonWishlist/scripts/install_systemd.sh
```

---

### `ERROR: Playwright does not support chromium on ubuntu<version>-x64`

**Cause:** `install_systemd.sh` runs `playwright install --with-deps chromium`. If the Ubuntu version is newer than what the installed Playwright release officially lists (e.g. Ubuntu 26.04 with Playwright 1.59.x), the install fails with the error above. The Linux x64 binary is identical across Ubuntu 22/24/26 — the block is purely an OS-version check.

**Fix (step 1):** Install the browser binary with the platform override, bypassing the OS check:

```bash
sudo bash -c "
PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64 \
PLAYWRIGHT_BROWSERS_PATH=/opt/amazon-wishlist/.cache/playwright \
  /opt/amazon-wishlist/.venv/bin/python -m playwright install chromium-headless-shell \
&& chown -R wishlist:wishlist /opt/amazon-wishlist/.cache/playwright
"
```

**Fix (step 2):** The override must also be present when the service launches Playwright at scrape time. `amazon-wishlist.service` in this repo already includes:

```
Environment="PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64"
```

If the service file in `/etc/systemd/system/` predates this fix, copy it in:

```bash
sudo install -m 644 ~/AmazonWishlist/amazon-wishlist.service /etc/systemd/system/amazon-wishlist.service
sudo systemctl daemon-reload
sudo systemctl restart amazon-wishlist
```

Future `install_systemd.sh` runs copy the service file from the repo, so they will carry the override forward automatically.

---

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

  (The status JSON also carries `run_id`, `pending_ids`, and `last_started_at` — internal resume bookkeeping; safe to ignore.)

## Resume after restart

A full scrape takes hours (one wishlist per `WISHLIST_PER_LIST_SECONDS`, default 1h). The daily run therefore overlaps `apt-daily-upgrade`, and a security upgrade to a library the service links (e.g. `openssl`/`libssl`) makes `needrestart` **restart the service mid-scrape**, which would otherwise abandon the run until the next 03:00 cron and leave some wishlists on stale data.

To survive that, progress is persisted to `WISHLIST_PROGRESS` (`data/scrape_progress.json`) on every step — including the remaining-wishlist queue (`pending_ids`) and the wall-clock start of the last wishlist (so the per-list pacing gap is honoured across a restart). On startup the app checks that file: if a run was interrupted (queue still non-empty) and is younger than `WISHLIST_RESUME_MAX_AGE`, it resumes in the background, scraping **only** the wishlists that hadn't completed. A normal finish drains the queue, and a login-expiry abort clears it, so neither triggers a spurious resume.

This is best-effort, not a mitigation of the restart itself: if you'd rather the scrape not be interrupted at all, exclude this unit from `needrestart` (`/etc/needrestart/conf.d/`) or move the `apt-daily-upgrade.timer` outside your scrape window.

## Data model

SQLite, file at `data/wishlist.db`. Schema is created/migrated on startup.

- `wishlist` — registered URLs (`url`, `label`, `added_at`, `last_scraped_at`, `previous_item_count`, `pending_shrink_count`).
  `previous_item_count` is the membership count captured immediately *before* the latest ingest, so on a healthy run it matches the current count — that pair is a scrape-size sanity check, **not** a record of what changed on the wishlist. `pending_shrink_count` holds a short scrape that was refused (see `WISHLIST_SHRINK_FLOOR`) and is cleared by the next normal one.
- `book` — one row per ASIN ever seen (`title`, `author`, `product_url`, `first_seen`, `last_seen`).
- `wishlist_book` — many-to-many; rebuilt for a wishlist on each successful scrape, so removing an item from your Amazon wishlist drops it off `/deals` etc. but keeps its history.
- `price_snapshot` — append-only `(asin, observed_at, current_price_cents, list_price_cents, availability)`.

`availability` is `available` | `kindle_unavailable` | `page_404`.

## Notes / limitations

- Amazon actively rate-limits scrapers. The defaults (3 AM start, 1-hour pacing, 4–9 s per-page jitter, browser-like headers) are tuned to fly under the radar for accounts with a handful of wishlists totaling around 1,000 items. Larger accounts or noisier IPs may still see occasional bot-blocks; the app preserves the prior state when this happens and saves the offending HTML to `data/diagnostics/`.
- **A failed scrape leaves every count untouched, on purpose** ("never clobber good data"), so a wishlist that has been bot-blocked for days still shows a matching Previous/Current pair on `/wishlists` and looks perfectly healthy. `last_scraped_at` is the only column that moves — it is flagged **stale** past `WISHLIST_STALE_AFTER_HOURS`. Read the age, not the counts.
- Amazon's HTML structure changes occasionally. If scrapes start returning 0 items *without* a "bot-blocked" status, check `data/diagnostics/` for the saved HTML and update the selectors in `app/scraper.py`.
- This is a single-user app; there is no auth on the web UI. Don't expose it to the public internet without a reverse proxy + auth in front.
