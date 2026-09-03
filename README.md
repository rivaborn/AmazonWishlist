# Amazon Wishlist Deal Tracker

Self-hosted FastAPI app that watches Amazon ebook wishlists and shows deals, the full catalog, missing-price items, and price-drop history on a small web UI at port 9060.

## How it works

Two scraper modes, selected automatically based on whether a saved login session exists:

- **Anonymous (httpx)** — works on any **public** wishlist URL, no login. Default; gets IP-throttled by Amazon on accounts with many lists.
- **Authenticated (Playwright + headless Chromium)** — uses a saved Amazon session from a separate, throwaway account (logged-in scraping bypasses the throttling that hits anonymous requests). Login happens **inside the wiki UI** via the Login tab — server runs a headful Chromium under Xvfb and streams it to your browser via noVNC; you click through Amazon's real login page in an iframe.

Other behaviour:

- Scrapes each registered wishlist URL once a day at 00:00 server-local time, **at most one wishlist per hour** to stay under Amazon's bot-detection threshold (also on demand via the "Run scrape now" button — same pacing applies).
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

**Secrets & config for local runs** go in a repo-root `.env` file (gitignored, never
committed): copy [`.env.example`](.env.example) to `.env` and fill in the values. It is
loaded automatically at startup by `app/config.py` (real environment variables win over
it, so anything you `export` still takes precedence). On the **Ubuntu host** credentials
are not read from `.env` but from `/etc/default/amazon-wishlist` via the service unit's
`EnvironmentFile` — see "Configuration (env vars)".

## Production deploy on Ubuntu

```bash
sudo bash scripts/install_systemd.sh
```

The script is **idempotent** — re-run it after every code change and it will rsync new files into `/opt/amazon-wishlist`, refresh the venv, and `systemctl restart` the unit. The SQLite DB and `diagnostics/` folder under `/opt/amazon-wishlist/data/` are preserved.

What it does on first run:

- Creates a `wishlist` system user.
- `apt-get install`s the Login-tab infra (`xvfb`, `x11vnc`, `websockify`, `novnc`) plus the tunnel's hard deps (`curl`, `wireguard-tools`), and — best-effort, a stale .deb URL must never abort a deploy — the `nordvpn` CLI (it ships as a .deb from Nord, not a distro package).
- Copies the repo to `/opt/amazon-wishlist`.
- Builds a venv (with an `ensurepip` fallback for Ubuntu builds where `python3 -m venv` skips pip).
- Installs Python deps and runs `playwright install --with-deps chromium` to pull the browser binary + its system runtime libraries.
- Installs and starts the `amazon-wishlist.service` systemd unit, and installs `amazon-wishlist-bookbub.service` (the daily BookBub one-shot — **scheduled by the app itself**, not a systemd timer) and `amazon-wishlist-vpn.service` (the `wlvpn` NordVPN tunnel — installed but **not enabled at boot**: it is brought up on-demand by the BookBub run and torn down after it, so the VPN is connected only while a run is in use; until the operator has run `nordvpn login --token <TOKEN>` it cannot come up, and that must never block a deploy).
- Provisions a scoped sudoers rule at `/etc/sudoers.d/amazon-wishlist` (mode `0440`, validated with `visudo -cf`) granting the `wishlist` user NOPASSWD access to exactly `systemctl start amazon-wishlist-bookbub.service` and `systemctl stop amazon-wishlist-vpn.service` — the bridge the app's scheduler and the run's teardown use (not blanket sudo; same pattern as `scripts/vpn_verify.sh`).

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

On the Ubuntu host the app is configured through env vars — either `Environment=` in
`amazon-wishlist.service` or (secrets and host-specific knobs) the values it loads
from `EnvironmentFile=/etc/default/amazon-wishlist`. For **local development** you can
instead put any of the keys below into a repo-root `.env` file: `app/config.py` loads it
at startup. `.env` is **gitignored and never committed** — copy
[`.env.example`](.env.example) to `.env` and fill in your own values. An already-set
real environment variable always wins over `.env`, so this is purely a local convenience
and can never override what the host's systemd env provides. Secrets shared across
environments (`BOOKBUB_USERNAME`/`PASSWORD`, `GRIMMORY_USERNAME`/`PASSWORD`,
`NORDVPN_TOKEN`, `BOOKBUB_LOGIN_LINK`) belong in `.env` on a dev box and
in `/etc/default/amazon-wishlist` in production — never in a committed file.

| var | default | meaning |
| --- | --- | --- |
| `WISHLIST_PORT` | `9060` | HTTP port |
| `WISHLIST_DB` | `data/wishlist.db` | SQLite path |
| `WISHLIST_LOG` | `data/scrape.log` | rotating scrape log |
| `WISHLIST_SCRAPE_HOUR` | `0` | daily cron hour (server local). Midnight: blocks cluster by hour and this slot is the quietest, and it leaves room for a paced run to finish before the mirror's daily sync. |
| `WISHLIST_SCRAPE_MINUTE` | `0` | daily cron minute |
| `WISHLIST_PER_LIST_SECONDS` | `3600` | minimum seconds between starting one wishlist and the next during a single run. Set to `0` to disable pacing for one-off testing. |
| `WISHLIST_PROGRESS` | `data/scrape_progress.json` | scrape progress is mirrored here on every step; on startup an interrupted run is resumed from it (see "Resume after restart" below) |
| `WISHLIST_RESUME_MAX_AGE` | `86400` | max age (seconds) of an interrupted run that will be auto-resumed on startup; older ones are discarded and left for the next daily cron |
| `WISHLIST_DELAY_MIN` / `WISHLIST_DELAY_MAX` | `4.0` / `9.0` | jittered delay between page-level requests within a single wishlist scrape, seconds |
| `WISHLIST_TIMEOUT` | `20` | per-request HTTP timeout, seconds |
| `WISHLIST_MAX_PAGES` | `100` | hard cap on pages fetched per wishlist. Reaching it while Amazon still offers a next page is treated as **partial pagination** and raises `FetchFailed` — the result is a prefix, and ingesting a prefix would replace the whole membership with it. |
| `WISHLIST_MAX_STALE_PAGES` | `3` | stop paginating after this many consecutive pages that add no new ASINs. Amazon keeps minting fresh `paginationToken`s past the end of a list, each re-serving rows already held, so "no next link" never arrives on its own — this is the real end-of-list signal. Only pages that still HAVE rows count as end-of-list; the same number of consecutive **zero-row** pages raises `FetchFailed` instead (an empty page is a soft-block or selector drift, never how a list ends). |
| `WISHLIST_SHRINK_FLOOR` | `0.8` | refuse to replace a wishlist's membership when a scrape returns less than this fraction of the stored count. A shrink is accepted only when the next completed scrape is short too and agrees with the first (both counts within this same ratio of each other); a scrape failure in between resets the confirmation. |
| `WISHLIST_503_RETRIES` | `2` | retries (with backoff) of a page that served Amazon's 503 "Dogs of Amazon" error page — a transient by its own copy, and mid-list it would otherwise cost the whole scrape. |
| `WISHLIST_STUB_RETRIES` | `1` | retries of the anti-automation stub, **mid-list only** — a page-1 stub is a refused visit and is never retried. |
| `WISHLIST_BLOCK_BACKOFF` | `90` | base backoff seconds before a blocked-page retry; attempt N waits N×base plus jitter. |
| `WISHLIST_STALE_AFTER_HOURS` | `26` | a wishlist whose last **successful** scrape is older than this is flagged stale on `/wishlists`. 26 rather than 24 because one-list-per-hour pacing already spreads a run across ~7 h. A wishlist that has never been scraped successfully ages from `added_at`. |
| `WISHLIST_USER_AGENT` | recent Chrome | UA string sent to Amazon |
| `WISHLIST_STORAGE_STATE` | `data/storage_state.json` | Playwright session file. Presence flips the scraper to authenticated mode automatically. |
| `WISHLIST_PLAYWRIGHT_HEADLESS` | `1` | Headless mode for the *scrape* (login is always headful). Set to `0` to debug. |
| `WISHLIST_VNC_PORT` | `6080` | Port the noVNC client binds to during a Login session. Closed when no session is active. |
| `WISHLIST_LOGIN_IDLE_TIMEOUT` | `600` | Seconds before an idle Login session is auto-cancelled. |
| `WISHLIST_XVFB_DISPLAY` | `:99` | Display number for the virtual X server during login. |
| `WISHLIST_XVFB_RESOLUTION` | `1280x800x24` | Geometry for the virtual display. |
| `WISHLIST_NOVNC_DIR` | `/usr/share/novnc` | Where the apt `novnc` package lays out its HTML/JS. |
| `WISHLIST_ROLE` | `primary` | `primary` scrapes Amazon; `secondary` never does and mirrors a primary instead. See "Two-instance mirror". |
| `WISHLIST_PRIMARY_URL` | *(unset)* | Secondary only: base URL of the primary, e.g. `http://192.168.50.43:9060`. Must name the port the primary actually serves on. |
| `WISHLIST_SYNC_HOUR` | `8` | Secondary only: daily sync hour, server local time. Set this **after** the primary's run finishes — see "Picking the sync time". |
| `WISHLIST_SYNC_MINUTE` | `0` | Secondary only: daily sync minute. |
| `WISHLIST_SYNC_TIMEOUT` | `60` | Read timeout (seconds) for a sync request. Separate from `WISHLIST_TIMEOUT`, which is tuned for Amazon. |
| `WISHLIST_SYNC_PAGE_LIMIT` | `2000` | Snapshot rows per sync request. Server-side hard clamp is 10000. |
| `WISHLIST_SYNC_STATE` | `data/sync_state.json` | Secondary only: last-sync telemetry. Advisory — the real sync cursor is `MAX(price_snapshot.id)` in the database. |

## Pages

- **/deals** — books on a wishlist whose latest snapshot is below baseline by ≥ filters. Filter by minimum dollar drop, minimum percent drop, and basis (vs. previous observed price or vs. list/strikethrough price).
- **/bookbub-deals** — the BookBub Deals tab: BookBub deals currently verified live on Amazon (see "Verifying deals are still live" below). Each row shows Title (a link that opens the Amazon product page in a new tab), Author, Deal price, Regular price, and Date of Deal. Its source is `data/deals.db` (`DEALS_DB`): only rows with `deal_status = 'current'` and an `amazon_url` are listed — expired, unverified (NULL status), and unreadable (`unknown`) deals are omitted. The **Deal price** and **Date of Deal** columns are clickable sort headers: clicking one cycles ascending/descending, and clicking a non-active column switches to it with its default (price ascending = cheapest first, date descending = most recent first); the default order is Date of Deal descending (most recent first). Each row also carries the book's **cover image** — `<img class="deal-cover">`, served at `/covers/<name>` from the local `data/covers/` directory — and the book's **description** shows as a hover tooltip on both the cover and the title link; both are captured during verification (below) and blank until a deal's book has been verified. Each row also shows the book's **Amazon star rating** and **rating count** (e.g. `4.5★ (1,234)`), captured in the daily check (below) and blank until a deal has been checked. A **Min rating** dropdown (`?min_stars=0|3|3.5|4|4.5`) filters to deals rated at least that high (a non-zero value drops unrated rows) and is preserved across sort/pagination. The tooltip is a custom CSS element (`.tip[data-tip]`, not the native `title`, whose size the browser controls) so its **text size** is configurable via the **Tooltip-size dropdown** (small/normal/large, default normal, primary-only — also reflected on the Settings tab). A **cover-size dropdown** (1x/2x/4x, default 1x, primary-only) rescales the covers; its choice is a stored runtime setting (see "Runtime settings" below). A **per-page dropdown** (20/40/60/80/100, default 20) picks how many rows per page; `per_page` is preserved across sorting, pagination, and the dropdown itself. The URL query params are `sort=price` or `sort=date` and `dir=asc` or `dir=desc`, preserved across pagination. Each row has a **Hide** checkbox (leading column) that hides that book from the tab; it persists to the DB via `POST /api/deals/{id}/hidden` (`deal.hidden`), and the **Show Hidden** checkbox at the top reveals hidden deals (`?show_hidden=1`). Read-only on both instances (the hide toggle is primary-only, like the purchased flag).
- **/books** — every available book across all wishlists, sorted by current price ascending. Header shows total count, lowest, and highest.
- **/no-price** — split into "Kindle edition unavailable" and "Removed from Amazon" (HTTP 404).
- **/price-drops** — every historical snapshot that dropped vs. its baseline, filtered.
- **/wishlists** — add/remove wishlist URLs, run scrape on demand. On a secondary this becomes a read-only table plus a mirror-status panel. Each row shows when it was last scraped and the item count from that scrape. The Run-scrape button shows a live progress bar and a "Waiting until HH:MM:SS" indicator between paced scrapes.
- **/purchased** — books marked purchased. They are excluded from every other view and shown here regardless of whether they are still on a wishlist.
- **/login** — log in to the secondary Amazon account that the authenticated scraper uses. See "Authenticated scraping" below.
- **/settings** — primary-only: set the daily **scrape time** (default 0:00), the daily **BookBub time** (default 18:00), and the BookBub **cover size** (1x/2x/4x, default 1x). Changes persist to the app's `settings` table and reschedule the cron jobs immediately — no restart, and a restart never triggers a bookbub/grimmory update (only the configured daily time does). The mirror never sees this page (nor the nav link).

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
5. Next scrape (manual button or midnight cron) auto-detects the file, logs `Scraper path: playwright`, and uses the logged-in session.

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

### wlvpn tunnel fails to start (`fopen: Permission denied`)

`amazon-wishlist-vpn.service` exits 1 with nothing in its log but `wg`'s bare `fopen: Permission denied`. Ubuntu 24.04+ ships a Canonical AppArmor profile for `/usr/bin/wg` (`/etc/apparmor.d/wg`) whose only file rule is `file rw @{etc_rw}/wireguard/{,**}` — a session key file anywhere else is denied, and **nothing in the error mentions AppArmor**. Confirm with:

```bash
dmesg | grep -i 'apparmor.*DENIED'      # profile="wg" name="/tmp/tmp.XXXX" comm="wg"
```

`vpn_netns_up.sh` keeps its key in `/etc/wireguard` for exactly this reason; if you see this, the box is running an older copy of the script.

Two traps that make this harder to diagnose than it should be:

- After a few failed starts the unit hits `StartLimitBurst=5` and every later attempt returns **"Start request repeated too quickly"** — which masks whether your fix worked. Always `sudo systemctl reset-failed amazon-wishlist-vpn.service` before retrying.
- Current NordVPN clients (5.x) have **no username/password login** — only browser SSO, `--callback`, and `--token`. If `nordvpn account` fails for `WISHLIST_VPN_USER`, the preflight aborts with "nordvpn not logged in"; the fix is an access token from the Nord Account dashboard, not the account password.

## Scrape progress / status API

Two JSON endpoints back the wishlists page UI and can be polled by anything else:

- `POST /api/scrape/run` — starts a full scrape. If one is already running, returns `{"started": false, "progress": {...}}` instead of stacking a duplicate.
- `GET /api/scrape/status` — current progress. Shape:

  ```json
  {
    "running": true,
    "started_at": "2026-05-03T00:00:00.000000",
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

On a secondary, `POST /api/scrape/run` returns **403** — mirrors never scrape.

### Sync API

Served by the **primary** and consumed by the secondary:

- `GET /api/sync/catalog` — the whole small half of the DB (`wishlist`, `book`, `wishlist_book`) read in one transaction, plus `max_snapshot_id` (the watermark that catalog is consistent with) and `source_now` (the primary's clock).
- `GET /api/sync/snapshots?since_id=&max_id=&limit=` — one ascending page of the append-only `price_snapshot` log as positional arrays. Returns `has_more` / `next_since_id`.
- `GET /api/sync/deals` — the whole BookBub `deal` table (all columns, incl. `cover`/`description`/`hidden`) plus the cover images as base64, fetched on the same once-a-day pull. The table is small, so the secondary **replaces its local rows wholesale** (no watermark, no cursor) and rewrites the covers into its own `DEALS_COVERS_DIR` — a secondary duplicates the primary's BookBub Deals page, covers and tooltips included.

About **this** instance's own mirroring, on either role:

- `GET /api/sync/status` — role, primary URL, last success/error, watermark, row counts.
- `POST /api/sync/run` — pull now. **403** on a primary, which has nothing to sync from.

⚠️ The two export endpoints hand out the entire database, and like the rest of this app they are unauthenticated. Firewall port 9060 to the peer IP or your VPN subnet.

## Resume after restart

A full scrape takes hours (one wishlist per `WISHLIST_PER_LIST_SECONDS`, default 1h). The daily run therefore overlaps `apt-daily-upgrade`, and a security upgrade to a library the service links (e.g. `openssl`/`libssl`) makes `needrestart` **restart the service mid-scrape**, which would otherwise abandon the run until the next midnight cron and leave some wishlists on stale data.

To survive that, progress is persisted to `WISHLIST_PROGRESS` (`data/scrape_progress.json`) on every step — including the remaining-wishlist queue (`pending_ids`) and the wall-clock start of the last wishlist (so the per-list pacing gap is honoured across a restart). On startup the app checks that file: if a run was interrupted (queue still non-empty) and is younger than `WISHLIST_RESUME_MAX_AGE`, it resumes in the background, scraping **only** the wishlists that hadn't completed. A normal finish drains the queue, and a login-expiry abort clears it, so neither triggers a spurious resume.

This is best-effort, not a mitigation of the restart itself: if you'd rather the scrape not be interrupted at all, exclude this unit from `needrestart` (`/etc/needrestart/conf.d/`) or move the `apt-daily-upgrade.timer` outside your scrape window.

## Data model

SQLite, file at `data/wishlist.db`. Schema is created/migrated on startup.

- `wishlist` — registered URLs (`url`, `label`, `added_at`, `last_scraped_at`, `previous_item_count`, `pending_shrink_count`).
  `previous_item_count` is the membership count captured immediately *before* the latest ingest, so on a healthy run it matches the current count — that pair is a scrape-size sanity check, **not** a record of what changed on the wishlist. `pending_shrink_count` holds a short scrape that was refused (see `WISHLIST_SHRINK_FLOOR`) and is cleared by the next normal one.
- `book` — one row per ASIN ever seen (`title`, `author`, `product_url`, `first_seen`, `last_seen`).
- `wishlist_book` — many-to-many; rebuilt for a wishlist on each successful scrape, so removing an item from your Amazon wishlist drops it off `/deals` etc. but keeps its history.
- `price_snapshot` — append-only `(asin, observed_at, current_price_cents, list_price_cents, availability)`.
- `settings` — key/value runtime settings (daily times, cover size; set in the **Settings tab** — see "Runtime settings" below). Primary-only: the mirror's `export_catalog` ignores the table, so settings never sync.

`availability` is `available` | `kindle_unavailable` | `page_404`.

`book` also carries `purchased` (0/1). `price_snapshot.id` is an `AUTOINCREMENT` primary key and the table is append-only — nothing in the app ever updates or deletes a snapshot. That, plus SQLite being single-writer, is what makes `MAX(id)` a safe sync cursor; see "Two-instance mirror".

On a secondary, `data/sync_state.json` records last-sync telemetry. It is **not** the cursor — a file cursor and the rows it describes are two separate writes and can diverge on a crash.

## Two-instance mirror (primary / secondary)

To run the app at two locations, exactly **one** instance may scrape. Two instances hitting the same throwaway Amazon account from two IPs is precisely the traffic pattern the pacing and anti-bot guards exist to avoid.

- **Primary** — behaves exactly as a single instance always has. It additionally serves `/api/sync/*`.
- **Secondary** — never scrapes and never logs in. It pulls the primary's data into its own SQLite on an interval, so every page works identically and keeps working while the primary is unreachable. It is strictly read-only: adding wishlists and ticking "purchased" happen on the primary and mirror down. Write endpoints return 403 and the controls behind them are not rendered.

`primary` is the default, so an existing single-host install needs no change.

### Setup

Per-host settings go in `/etc/default/amazon-wishlist`, **not** in the unit file — `install_systemd.sh` reinstalls the unit from the repo on every deploy and would revert an edit there.

On the primary, nothing is required. On the secondary:

```bash
sudo tee /etc/default/amazon-wishlist >/dev/null <<'EOF'
WISHLIST_ROLE=secondary
WISHLIST_PRIMARY_URL=http://192.168.50.43:9060
EOF
sudo chmod 640 /etc/default/amazon-wishlist
sudo chown root:wishlist /etc/default/amazon-wishlist
sudo systemctl restart amazon-wishlist
```

Then check the log line naming the resolved role — this is the one thing worth verifying on every deploy, because a secondary that came up as a primary will start scraping Amazon from a second IP:

```bash
journalctl -u amazon-wishlist -n 20 | grep '^Role:'
```

Note `ExecStart` hardcodes `--port 9060`, so `WISHLIST_PORT` is decorative and `WISHLIST_PRIMARY_URL` must name port 9060.

### What to expect

The secondary syncs **once a day** at `WISHLIST_SYNC_HOUR:WISHLIST_SYNC_MINUTE` (default 08:00 server local), plus once at startup — so a restart re-converges immediately instead of waiting up to a day. You can always force one with the **Sync now** button on `/wishlists` or `POST /api/sync/run`.

The first sync pulls the full snapshot history — for ~1,000 tracked items that is roughly 30 MB over ~180 requests, i.e. a minute or two on a LAN. Every sync after that carries only new rows. A sync also runs once at startup, so a restart re-converges immediately rather than waiting out the interval.

The `/wishlists` page on a secondary shows **two** freshness figures, which answer different questions: the per-row *stale* flag is how old the **primary's scrape** is (computed against the primary's own clock, carried down with the catalog, so the two hosts need not share a timezone), and the mirror panel is how old **our copy of the primary** is.

The same pull also mirrors the BookBub deals DB + covers (`GET /api/sync/deals`, above) — so the BookBub Deals tab works identically on a secondary and updates with everything else once a day. The deals pull is **non-fatal** to the wishlist sync: an older primary without the endpoint (404) is skipped with a log line and retried on the next pull, so mixed-version peers keep working.

### Picking the sync time

The sync hour must land **after** the primary has finished its nightly run, or the wishlists it scrapes last stay a day behind on the mirror.

The primary starts its whole run at `WISHLIST_SCRAPE_HOUR` and paces one wishlist per `WISHLIST_PER_LIST_SECONDS` (default 1h). Pacing is measured from the *previous* list's start, so the first list begins immediately at the cron fire and list *k* begins `k-1` hours in:

```
last list starts   =  WISHLIST_SCRAPE_HOUR + (N - 1) hours     # N = number of wishlists
```

With the run starting at **00:00** and the sync at the default **08:00**, that covers up to **8 wishlists** (the 8th starts at 07:00 and finishes well before 08:00). Leave an hour of slack for blocked-page retries — `WISHLIST_503_RETRIES` backoff can add ~5 min per list:

| wishlists | last list starts | 08:00 sync catches it? |
| --------- | ---------------- | ---------------------- |
| 7         | 06:00            | yes, ~1.5h to spare    |
| 8         | 07:00            | yes, tight             |
| 9         | 08:00            | no — races the sync    |

Getting it wrong is not a data problem — nothing is lost or corrupted, and the mirror is never internally inconsistent, because a sync is always a coherent prefix. The lists scraped after the sync fires simply arrive on the *next* day's sync. The mirror panel's "synced Nh ago" and each row's stale flag are what surface it.

### Recovery

The secondary refuses a catalog that would shrink its membership below `WISHLIST_SHRINK_FLOOR` unless a second, agreeing catalog confirms it — the same rule that protects an ingest. It also refuses a primary whose `max_snapshot_id` has gone *backwards*, which means that primary's database was rebuilt, restored from an older backup, or `WISHLIST_PRIMARY_URL` now points somewhere else. Both show up in `GET /api/sync/status` as `last_error`.

For the regression case the fix is a full resync — the secondary holds nothing the primary doesn't:

```bash
sudo systemctl stop amazon-wishlist
sudo -u wishlist rm /opt/amazon-wishlist/data/wishlist.db*
sudo systemctl start amazon-wishlist
```

The deals mirror needs **no** recovery action: it holds no cursor and no local state — every pull wholesale-replaces the local `deal` rows and rewrites the covers, so the next sync re-converges it automatically.

### Failover: promoting a secondary

When the primary is down long enough that you are unwilling to lose more daily scrapes, the secondary can take over. Every role check funnels through `config.ROLE`, which is read **once at import**, so the entire switch is the env file plus a restart:

```bash
sudo cp -a /etc/default/amazon-wishlist /etc/default/amazon-wishlist.bak-$(date +%Y%m%d)
sudoedit /etc/default/amazon-wishlist     # WISHLIST_ROLE=primary, and comment out WISHLIST_PRIMARY_URL
sudo systemctl restart amazon-wishlist
journalctl -u amazon-wishlist -n 30 | grep '^Role:'    # expect: Role: PRIMARY. Daily scrape at HH:MM ...
```

**First check whether you need to promote at all.** Only the *scheduling* is role-gated. `amazon-wishlist-bookbub.service` is a standalone oneshot that writes `data/deals.db` and never touches `price_snapshot`, so it runs on a secondary unchanged — a missed BookBub day can be caught up without any role change, and `--date` targets the day that was missed rather than today:

```bash
sudo -u wishlist bash -c 'set -a; . /etc/default/amazon-wishlist; set +a;   export HOME=/opt/amazon-wishlist PLAYWRIGHT_BROWSERS_PATH=/opt/amazon-wishlist/.cache/playwright          PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64 XDG_RUNTIME_DIR=/tmp/runtime-wishlist;   cd /opt/amazon-wishlist && xvfb-run -a .venv/bin/python scripts/bookbub_daily.py --date 20260831 --netns wlvpn --limit 1'
```

Exporting the unit's own `Environment=` lines matters: sourcing only `/etc/default/amazon-wishlist` leaves `PLAYWRIGHT_BROWSERS_PATH` unset, Playwright then looks in `~/.cache/ms-playwright`, and the fetch fails with "Executable doesn't exist". `--limit` bounds the verify pass so catching up two days does not pay for two full re-verifications. On a secondary the fetched rows survive only until the next sync, which wholesale-replaces the `deal` table.

**The revert rule — the thing to get right.** `price_snapshot` is written by exactly one function, `ingest_wishlist`, i.e. only by a wishlist scrape. Promotion itself, the BookBub job and `owned_update` write no snapshots. So:

- **Before the promoted box's first scrape** its `wishlist.db` is still a pristine mirror, `MAX(price_snapshot.id)` still equals the watermark it synced, and reverting to `secondary` is just the env file and a restart — **no wipe, no resync**.
- **After that first scrape** it allocates ids from `watermark+1` — the same ids the original primary will hand to *different* rows. The two databases have diverged permanently and whichever box becomes the secondary must be wiped (`rm data/wishlist.db* data/sync_state.json`) and fully resynced. `sync._warn_id_collision` only detects this after the fact, once rows have already been silently dropped.

Check it any time with `sqlite3 /opt/amazon-wishlist/data/wishlist.db "SELECT MAX(id) FROM price_snapshot"` against the `watermark` in `GET /api/sync/status`. The deadline is the next `daily_scrape` fire, so decide before then or move the scrape time in the Settings tab to buy time.

**What promotion turns on**, all at once: `daily_scrape` at the configured hour, `bookbub_daily` at 18:00, the monthly `owned_update`, the Login and Settings tabs, and the "Run scrape now" button. A `CronTrigger` never fires at startup, so a restart alone triggers nothing — but a job whose time has *not yet passed today* fires today.

**What does not mirror, and must be provisioned by hand on the new primary:**

- `data/storage_state.json` — the Amazon session is never synced and a secondary cannot create one (the login routes are 403). Without it the new primary scrapes **anonymously**, which only works for wishlists that are Public. Copy the file across or redo the Login-tab flow.
- The `settings` table — `export_catalog` skips it, so the scrape and BookBub times fall back to the `WISHLIST_SCRAPE_HOUR` / 18:00 **env defaults**, not whatever the old primary had stored. Re-set them in the Settings tab.
- Every secret in `/etc/default/amazon-wishlist` — `BOOKBUB_USERNAME`/`PASSWORD`, `GRIMMORY_USERNAME`/`PASSWORD`, and the one-time `nordvpn login --token`. A mirror never needed any of them, so a box that has only ever been a secondary has none.

**Handing the role back.** Point the old primary at the new one (`WISHLIST_ROLE=secondary`, `WISHLIST_PRIMARY_URL=http://<new-primary>:9060`), then, because the ids diverged, wipe and resync it:

```bash
sudo systemctl stop amazon-wishlist
sudo -u wishlist rm /opt/amazon-wishlist/data/wishlist.db* /opt/amazon-wishlist/data/sync_state.json
sudo systemctl start amazon-wishlist
```

Also confirm the new secondary can reach the new primary on port 9060 — the sync direction reverses, and that port is unauthenticated and must stay firewalled to the peer.

## Grimmory book catalog (data/grimmory.db)

A one-off export of the home-lab **Grimmory** (BookLore) ebook libraries into this repo's `data/` directory. It is a separate SQLite file from `wishlist.db` with its own schema — a static catalog snapshot (title, author, publisher, date published, ISBN). The web app reads it (see "Update Owned Books" below) but never writes it directly; it is rebuilt from Grimmory by `scripts/build_grimmory_db.py`, run manually OR automatically: on the 1st of each month, and on demand via the **"Update Owned Books"** button on the Settings tab.

`scripts/build_grimmory_db.py` logs into the Grimmory instance (JWT login via `POST /api/v1/auth/login`, see `app/grimmory.py`), resolves the target libraries by name, fetches every book per library (`GET /api/v1/libraries/{id}/book`), and rebuilds the `book` table in a single transaction (staging table renamed over the old one, so a failed run rolls back and the previous data is left intact):

```bash
GRIMMORY_USERNAME=... GRIMMORY_PASSWORD=... python scripts/build_grimmory_db.py
```

The `book` table is `id` (PK) plus `library_id`, `library_name`, `title`, `author`, `publisher`, `published_date` (ISO date), `isbn` (ISBN-13, falling back to ISBN-10). `author` is the author list comma-joined; `publisher`, `published_date`, and `isbn` are NULL when Grimmory has no value for that book.

Missing target library, bad login, or an HTTP error exits non-zero with a `GRIMMORY DB BUILD FAILED: ...` message and leaves the existing table untouched. A quick probe — lists every library with its book count and exits non-zero if a configured target library name is missing:

```bash
GRIMMORY_USERNAME=... GRIMMORY_PASSWORD=... python -m app.grimmory
```

`data/grimmory.db` is **gitignored** (like the rest of `data/`) — the build is reproducible from the Grimmory libraries themselves, so the file is a local artifact, not part of the repo.

### Configuration (env vars)

Read at run time by `scripts/build_grimmory_db.py` / `app/grimmory.py`. The password is env-only on purpose — it is never written to a committed file.

| var | default | meaning |
| --- | --- | --- |
| `GRIMMORY_URL` | `http://192.168.1.13:6060/` | Base URL of the Grimmory (BookLore) instance. |
| `GRIMMORY_USERNAME` | *(unset)* | Username for `/api/v1/auth/login`. |
| `GRIMMORY_PASSWORD` | *(unset)* | Password for `/api/v1/auth/login`. Supplied via the environment only, never committed. |
| `GRIMMORY_LIBRARIES` | `Amazon fksogbetun,Amazon rivaborn` | Comma-separated names of the libraries to export. |
| `GRIMMORY_DB` | `data/grimmory.db` | SQLite path of the catalog (gitignored via `data/`). |

## BookBub daily ebook deals (booklist.md + data/deals.db)

A one-off pull of the daily [BookBub](https://www.bookbub.com) ebook-deal list. Login is primarily the **BookBub account** — `BOOKBUB_USERNAME` / `BOOKBUB_PASSWORD` from the environment (real secrets, **never committed to the repo**; see Configuration) — which Chromium fills into the `bookbub.com/login` form. The signed **outbound link in the daily BookBub email** remains as an ad-hoc auto-login for one-off runs and the probe (`--link`), used only when no account credentials are configured: following it logs you into bookbub.com and lands on that day's daily-deals page. The deals themselves are served from `https://www.bookbub.com/ebook-deals/daily-deals?date=YYYYMMDD`, and any date can be opened in the same logged-in session.

BookBub sits behind a Cloudflare “Just a moment…” managed challenge, so a plain HTTP client is stopped at the interstitial. `app/bookbub.py` tries a lightweight httpx path first and, when that is interstitial-ed (the current case), falls back to Chromium driven by Playwright (headless, then headful as a retry) which executes the challenge and parses the deal cards.

### Running it

```bash
python scripts/build_bookbub_deals.py --link '<outbound link from the daily email>' [--date YYYYMMDD] [--out PATH]
```

- `--link` — today's outbound email link, or the `BOOKBUB_LOGIN_LINK` env var. It **rotates daily** — use the link from the email you just received; a stale one will not log in.
- `--date` — day to pull, `YYYYMMDD` (default: today).
- `--out` — report path (default: `booklist.md` at the repo root).
- `--llm-model` — optional: normalise the list through the local LLM gateway (see below).

The script fetches the deals, stores them in the deals database (see below), then writes the report atomically (tmp + replace), so a failed run never leaves a half-written file. Exit codes: `0` written, `1` fetch/parse error, an empty day, or a deals-DB write failure, `2` missing `--link`.

A quick probe that prints the day's deals without writing the file (also supports `--headless`/`--headful` if Cloudflare keeps challenging headless). It uses the account credentials from the environment by default; `--link '<outbound link>'` is the ad-hoc alternative when they are not set:

```bash
python -m app.bookbub [--username U --password P] [--link '<outbound link>'] [--date YYYYMMDD]
```

### Output

`booklist.md` at the repo root — a heading with the date and a table with one row per deal: **title** (linked to the resolved **Amazon Kindle page** — a plain, unlinked title when the deal has no Amazon edition), **author**, and **deal price** (`$X.XX`, or `Free!` for free deals). It is a generated daily report, rewritten on every run.

### Deals database (data/deals.db)

Every deal from a run is also stored in a standalone SQLite database, `data/deals.db` (gitignored via `data/`, separate from `wishlist.db` and `grimmory.db`), so the full history is retained for audit — including deals with no Amazon link and books not owned in the Grimmory libraries. The schema and helpers live in `app/deals_db.py`.

The `deal` table has one row per BookBub deal per day:

| column | meaning |
| --- | --- |
| `id` | primary key (autoincrement). |
| `date` | the deals day, `YYYYMMDD`. |
| `title` / `author` | the deal's title and author(s). |
| `deal_price` / `original_price` | the sale price and the strikethrough retail price. |
| `bookbub_url` | the BookBub book page — kept for audit only, not shown in `booklist.md`. |
| `amazon_url` | the resolved Amazon Kindle page (`amazon.com/dp/…`), followed out from the deal card's Amazon retailer button; **NULL when the book has no Amazon edition**. |
| `no_amazon_link` | `1` when `amazon_url` is NULL (no Amazon link saved), else `0`. |
| `owned_in_grimmory` | `1` owned / `0` not owned / **NULL when `grimmory.db` is unavailable**. An approximate normalised title+author match against `data/grimmory.db` — kept in the DB so a human can audit its accuracy. |
| `audited_at` | ISO timestamp of when the row was last written. |
| `deal_status` | live-deal check outcome: `current` / `expired` / `unknown` — **NULL until first verified** (written by `scripts/verify_deals.py`; the resume cursor). |
| `current_price` | the Amazon price last read during verification (NULL when the read was unreadable → `unknown`). |
| `verified_at` | ISO timestamp of the last live check. |
| `hidden` | `1`/`0` — row hidden from the BookBub Deals tab by the per-row checkbox (primary-only toggle; the mirror carries the flag). |
| `cover` | filename of the captured cover image in `data/covers/` (e.g. `B0….jpg`) — **NULL until the book's page has been verified** (captured by `scripts/verify_deals.py`). |
| `description` | the book's Amazon description captured at verification (hover tooltip on the tab) — **NULL until verified**. |
| `stars` | the book's Amazon star rating (0-5, e.g. `4.5`) captured at verification (the tab's Stars column) — **NULL until a page carried it**. |
| `ratings` | the book's Amazon rating count (e.g. `1234`) captured at verification — **NULL until a page carried it**. |

Rows are keyed by a UNIQUE index on `(date, bookbub_url)`. Re-running the same date **upserts** that day's rows (refreshed, never duplicated); rows for other dates are never deleted, so the history is kept for audit. The ownership lookup reads `grimmory.db` read-only and is approximate on purpose (normalised title **and** author must both match).

### Backfilling historical dates

`scripts/backfill_bookbub_deals.py` backfills the deals database over a range of past dates, day by day (default `20260613..20260826`, i.e. back from the newest day to the oldest). The single-day builder above only covers one day per run:

```bash
python scripts/backfill_bookbub_deals.py --link '<outbound link from the daily email>' [--start YYYYMMDD] [--end YYYYMMDD] [--dry-run]
```

- `--link` — the same rotating outbound link as the builder (required).
- `--start` / `--end` — newest / oldest day to process, inclusive (defaults from `BOOKBUB_BACKFILL_START` / `BOOKBUB_BACKFILL_END`).
- `--dry-run` — print the date list and current per-date status without fetching anything.

One login session covers a *chunk* of dates (default 5) and navigates `?date=YYYYMMDD` inside it — one Cloudflare login instead of one per day. The backfill writes **only** to `data/deals.db` (it never touches `booklist.md`), storing through the same `store_deals` path as the builder: ownership is audited against `grimmory.db` and `no_amazon_link` is set when a deal has no Amazon edition.

It is **idempotent and resumable**: after every date a per-date status (`ok` / `empty` / `challenge` / `error` + deal count + timestamp) is mirrored to `data/backfill_progress.json` (atomic tmp+replace), and dates already present in `deals.db` are treated as done at startup — a killed run resumes where it stopped and a re-run is a fast no-op over days already recorded.

**Cloudflare monitoring**: a day whose page is still on the interstitial after the wait is recorded `challenge` — retriable on the next run, deliberately *not* marked `empty`; a page with no deal cards is recorded `empty` (a day BookBub no longer serves; not retried); and under Cloudflare pressure (challenged pages or consecutive bad days) the between-sessions backoff doubles to let the block cool down. A login whose challenge never clears aborts the run cleanly — re-run later with a fresh `--link`.

### De-duplicating repeated books

A book can be featured on several days, so the same book can appear as multiple `deal` rows (one per date). `scripts/dedup_deals.py` collapses those repeats, keeping only the most recent deal for each book:

```bash
python scripts/dedup_deals.py [--db PATH] [--backup PATH] [--check]
```

- **Same-book identity** (`deals_db.book_identity`): the Amazon ASIN from `amazon_url` (`/dp/XXXXXXXXXX`, tracking suffix ignored) — a book re-featured on different dates shares its ASIN; deals with no Amazon link fall back to the normalised title+author pair.
- **Keep most recent**: per book, the row with the newest `date` (tie → highest row `id`) is kept; only duplicates are removed — the kept row is never modified, and a second run removes 0 (idempotent).
- **`--check`** previews: prints the stats (rows / distinct books / repeated books / rows that would be removed) and lists the removable rows without modifying anything.
- **Automatic backup**: before removing anything, the DB is backed up with the WAL-safe sqlite backup API to `data/deals_pre_dedup_<YYYYMMDD-HHMMSS>.db` (gitignored via `data/`, path overridable with `--backup`) — the pre-dedup state can always be restored from it.
- `--db` targets another deals DB (default `DEALS_DB` = `data/deals.db`).
- **The daily run does this automatically**: `scripts/bookbub_daily.py` dedups the stored rows after each fetch (inline `deals_db.deduplicate`, best-effort, logged — no backup step). The manual script above (with its automatic backup and `--check` preview) is for ad-hoc cleanups.

### Verifying deals are still live

A BookBub deal is only as good as the price Amazon actually charges today. `scripts/verify_deals.py` walks the stored deals, re-reads each book's live Amazon price over a rotating NordVPN tunnel, and records the outcome back in the deals DB:

```bash
python scripts/verify_deals.py [--limit N] [--rotate-every N] [--db PATH] [--check] [--nord-token T] [--fresh]
python scripts/verify_deals.py --netns wlvpn …   # Ubuntu: run INSIDE the wlvpn tunnel (see below)
```

- **Per-book price check**: each pending deal (`deal_status IS NULL` with an Amazon ASIN) is opened at `amazon.com/dp/<ASIN>` via `app/amazon_price.py` and compared to the stored `deal_price`: the current price at or below the deal price → **`current`**, above it → **`expired`**, and a price that cannot be read (blocked page, no price) → **`unknown`** — never guessed. When `LLM_MODEL` is set, an unreadable page falls back to the optional local LLM asking for just the current price (off by default; an LLM failure only logs a warning and leaves the price unreadable, so the row lands `unknown`).
- **Outcome columns**: `mark_verified` writes `deal_status`, the read `current_price` (NULL when `unknown`), and `verified_at` onto the `deal` row. **`deal_status` is the resume cursor** — a pending deal is exactly `deal_status IS NULL`, so an interrupted run resumes by simply re-running (verified rows are skipped, and a re-run of a finished run is a fast no-op); `data/verify_progress.json` (atomic tmp+replace) is an advisory telemetry mirror, never the source of truth. `--fresh` starts a new run scope; `--db` targets another DB (default `DEALS_DB`); `--check` is a dry run that prints the pending count + effective config, connects to nothing, and exits 0.
- **Cover/description/rating capture**: the same page read also captures the book's **cover image** and **description**, best-effort, plus the **Amazon star rating and rating count** (`#acrPopover` "X out of 5 stars" title + `#acrCustomerReviewText` count) — the cover is downloaded to `data/covers/` (`DEALS_COVERS_DIR`, filename `<ASIN>.<ext>`) and the metadata stored on the `deal` row (`cover`/`description` via `deals_db.update_cover_desc`, `stars`/`ratings` via `deals_db.update_rating`), feeding the tab's cover column, hover tooltip and Stars column. A capture failure never fails the run (logged, the price check stands) and rows that were never captured stay NULL (blank cover, no tooltip/rating).
- **NordVPN exit-IP rotation**: every `--rotate-every` books — the default is **10 books per IP** (`NORDVPN_ROTATE_EVERY`) — the run hops to a different NordVPN server via `app/nordvpn.py`: `rotate()` = disconnect → connect to a country/city *different* from the current one (pooled in `NORDVPN_COUNTRIES`) → read the new exit IP. In `--netns` tunnel mode (the Ubuntu deployment, below) the hop is instead a *best-effort tunnel rebuild* for a fresh exit IP — the namespace's IP is fixed for the tunnel's life, and a rebuild that is not permitted leaves the stable IP while the run continues.
- **Fingerprint rotation**: each book is fetched in its own browser context with a rotated fingerprint (`app/fingerprint.py`) — one of four plain desktop-Chrome 149 User-Agents with distinct OS tokens (`_LINUX` `X11; Linux x86_64`, `_WIN` `Windows NT 10.0; Win64; x64`, `_MAC` `Macintosh; Intel Mac OS X 10_15_7`, `_CROS` `X11; CrOS x86_64`), one of six `en-*` locales, and one of five viewports. After every exit-IP rotation the next fingerprint is *fresh* — different in **all three** fields from the previous one — so the IP and the browser identity change together.
- **Anti-bot pacing and retries**: per-book reads are spaced by a random jitter (`VERIFY_DELAY_MIN`–`VERIFY_DELAY_MAX` seconds, the same idea as the wishlist scraper's pacing); a blocked page, failed navigation, or no-price result is retried with backoff (`VERIFY_RETRY_BACKOFF` × up to `VERIFY_MAX_RETRIES`) before the book is recorded `unknown`. Blocked/ambiguous pages are dumped to `data/diagnostics/` for selector debugging.

The NordVPN login is read **only** from the `NORDVPN_TOKEN` environment variable (or `--nord-token`) — never from a committed file. It is a Nord Account **access token** (dashboard: Services → NordVPN → Access token), not the account password: current clients (verified on 5.3.0) removed username/password login entirely and accept only browser SSO, `--callback`, or `--token`. The starting exit country defaults to the first entry of `NORDVPN_COUNTRIES` (`NORDVPN_START_COUNTRY`); the CLI itself is `nordvpn` on `PATH` (overridable via `NORDVPN_CLI`), and `python -m app.nordvpn` is a standalone probe (`--login` / `--rotate` / `--connect COUNTRY [CITY]`). That host-CLI path is for dev boxes; the Ubuntu deployment uses the netns tunnel below instead.

### Daily BookBub updater (default 18:00 local, configurable in the Settings tab)

`scripts/bookbub_daily.py` runs the whole daily cycle in one pass:

```bash
python scripts/bookbub_daily.py --check                                                    # dry run: date, credential status, recheckable count
BOOKBUB_USERNAME='...' BOOKBUB_PASSWORD='...' python scripts/bookbub_daily.py [--date YYYYMMDD]  # manual run (dev box)
# Ubuntu: runs automatically via the systemd units below; manually:
#   systemctl start amazon-wishlist-bookbub.service
#   journalctl -u amazon-wishlist-bookbub -f
```

1. **Fetch + store** — pulls the day's BookBub daily deals (`bookbub.fetch_daily_deals`: logs into the BookBub account with `BOOKBUB_USERNAME` / `BOOKBUB_PASSWORD`; Playwright clears the Cloudflare challenge) and upserts them into `data/deals.db` via `deals_db.store_deals` — idempotent per date, computes `owned_in_grimmory` from `data/grimmory.db` (books you own never show on the tab) and sets `no_amazon_link`. It then **refreshes `owned_in_grimmory` on every stored row** against `data/grimmory.db` (`deals_db.refresh_owned`) — so a book added to your Grimmory libraries since the last run is re-flagged owned and drops off the tab on the next run without any re-fetch; a missing/empty `grimmory.db` leaves the flags NULL and the run still completes. It then **dedups** the stored rows (`deals_db.deduplicate`) so a book re-featured on a later date (same identity — ASIN, else normalised title+author) collapses to the single most recent deal per book; best-effort like the owned refresh — a failure is logged and the run continues.
2. **Re-verify** — then runs `scripts/verify_deals.py --recheck` (the tunnel/fingerprint/pacing loop above) over every deal that is neither `expired` nor `hidden` — i.e. `current`, `unknown`, and unchecked rows still visible on the tab — re-reading each against its live Amazon price. Deals that come back `current` flow into the BookBub Deals tab automatically.

   Two exclusions, both terminal in the sense that the row can never be displayed again, so an Amazon read for it buys nothing:

   - `expired` — terminal by requirement: *expired deals are never checked again*, even if the price later drops back.
   - `hidden` — dismissed by the user, and `current_deals()` filters hidden rows off the tab regardless of status. These dominate in practice: on 2026-09-01 they were **271 of 390** recheckable rows, so excluding them cut the nightly pass to **118 books** — roughly a third of the Amazon reads, and a correspondingly smaller anti-bot footprint. The trade-off is that un-hiding a deal shows whatever status it carried when it was hidden, because nothing refreshed it meanwhile.

**Scheduling (Ubuntu)**: the run is scheduled **by the app itself** — `app/scheduler.py` registers a `bookbub_daily` cron job on the primary at the time set in the **Settings tab** (default **18:00 local**; `BOOKBUB_HOUR_DEFAULT` / `BOOKBUB_MINUTE_DEFAULT` are just the fallback defaults — a stored setting overrides them), and its handler starts the one-shot `amazon-wishlist-bookbub.service` via the scoped sudoers rule `install_systemd.sh` provisions (the app runs as `wishlist`, and only systemd can place the process inside the netns). The old `amazon-wishlist-bookbub.timer` (`OnCalendar=*-*-* 18:00:00`) is retired — `install_systemd.sh` disables and removes it if a previous install left it, and no longer starts the service at deploy time (a run would block the deploy for the whole cycle). The **fetch runs on the host, outside the tunnel namespace** — `amazon-wishlist-bookbub.service` has no `NetworkNamespacePath`, because the fetch's *headful* Chromium fallback (used to clear Cloudflare when headless is challenged) cannot launch inside a netns (it crashes with a crashpad SIGTRAP), and the fetch needs neither the tunnel nor the netns. The whole cycle therefore runs under a **throwaway virtual display** (`xvfb-run -a` in the unit's `ExecStart`) so the headed browser has an X server on this headless box. After the fetch, `bookbub_daily` **starts `amazon-wishlist-verify.service` and waits for it** — that unit runs `verify_deals --netns wlvpn --recheck` **headless INSIDE the `wlvpn` namespace** (`Requires=`/`BindsTo=amazon-wishlist-vpn.service` bring the tunnel up), so the re-verify's Amazon reads egress only through NordVPN — the same fail-closed guarantee as before, split across the two units. Both units are **fail-fast** (no `Restart=`): a failing run dies and the app's once-a-day schedule is the retry. **Restarting the app — or the box — never triggers a bookbub/grimmory update**: a cron trigger's next fire time is always in the future after startup, so the update only ever happens at the configured daily time.

**Prerequisites**:
- `BOOKBUB_USERNAME` / `BOOKBUB_PASSWORD` in `/etc/default/amazon-wishlist` — the BookBub account, read from the environment and **never committed to the repo** (same rule as `GRIMMORY_USERNAME` / `GRIMMORY_PASSWORD`). **Missing** credentials exit the run **2** with a clear message (nothing fetched, DB untouched); **wrong/expired** credentials fail the fetch (logged, exit 1) but the re-verify step still runs. Rotate the account password if it is ever exposed.
- `data/grimmory.db` (`GRIMMORY_DB`) must be present for owned detection; without it, `owned_in_grimmory` stays NULL (unavailable) and those deals still show.
- `--check` is a dry run: it prints the date, the credential status (`credentials: set` / `no BOOKBUB_USERNAME / BOOKBUB_PASSWORD configured` — never the values), and the recheckable (neither expired nor hidden) deal count read straight from the DB — no network, browser, or tunnel.

### The wlvpn NordVPN tunnel (Ubuntu deployment)

On the Ubuntu box the verifier does **not** use the host-wide `nordvpn connect`: the NordVPN CLI has no per-process split tunnel (a `connect` reroutes the whole host). So `scripts/vpn_netns_up.sh` *borrows* the session into a Linux network namespace instead: a brief `nordvpn connect` on the host negotiates a WireGuard (NordLynx) session; the script reads the session's keys/endpoint/assigned address back out of the `nordlynx` interface (`wg show`) and `nordvpn disconnect`s (host routing returns to normal); it then rebuilds an equivalent WireGuard interface inside the `wlvpn` netns with `allowed-ips 0.0.0.0/0` plus a single default route. The namespace is leak-proof (there is no other route to fall back on), and **only processes placed inside it egress through Nord** — the host's SSH, LAN/NFS, and the wishlist scraper itself keep their normal connection.

- **`amazon-wishlist-vpn.service`** (Type=oneshot + `RemainAfterExit`) brings the namespace up **on-demand, not at boot** — it has no `[Install]` section, so it is started only when a consumer needs it (the daily BookBub run and the `amazon-wishlist-verify.service` unit pull it up via `Requires=`; the ad-hoc `scripts/vpn_verify.sh` wrapper instead requires the operator to have started it first with `systemctl start amazon-wishlist-vpn.service` — it refuses to run when the namespace is missing) and torn down when the run is done (the bookbub unit's `ExecStopPost`) — the VPN is **only connected while a run is in use**. It is `Requires`/`After` `nordvpnd.service` with a start-race-resilient restart **at the source** (`Restart=on-failure`, `RestartSec=20`, `StartLimitBurst=5` — a `Requires`/`BindsTo` consumer that loses this race fails its start job with result “dependency” and can never retry itself). `ExecStop` runs `scripts/vpn_netns_down.sh`. `install_systemd.sh` installs it and `systemctl disable`s/stops it if a previous install had enabled it — a down tunnel (`inactive (dead)`) is the healthy idle state. Both scripts support `--dry-run` / `WISHLIST_VPN_DRY_RUN=1` to print the exact command sequence without executing anything.
- **Prerequisites**: the `nordvpn` CLI installed and **logged in once** as the operator user (`nordvpn login --token <TOKEN>` — the token/credentials live only in the operator's login, never in this repo; the tunnel merely reads the session back out of the interface), that user in the `nordvpn` group, and `wireguard-tools` (all handled by `install_systemd.sh`, the CLI in a best-effort block).
- **Allowlist warning**: during that brief host connect the script allowlists the host's LAN (`WISHLIST_VPN_LAN_SUBNET`), the Tailscale range `100.64.0.0/10`, and port 22 so SSH/mgmt are never dropped — **set `WISHLIST_VPN_LAN_SUBNET` to match the host's LAN** or the SSH session drops for those seconds.
- **Running the verifier inside the tunnel**: `amazon-wishlist-verify.service` (`sudo systemctl start amazon-wishlist-verify`, follow with `journalctl -u amazon-wishlist-verify -f`) runs `verify_deals.py --netns wlvpn --recheck` inside the namespace; it doubles as the daily re-verify step of the BookBub updater. The verifier is `BindsTo`/`Requires` the tunnel, so it deliberately does NOT tear the tunnel down itself — stopping the tunnel from its own `ExecStopPost` would circular-wait against `TimeoutStopSec`. In the daily run the bookbub unit's `ExecStopPost` owns teardown; for a **standalone** run, follow up with `sudo systemctl stop amazon-wishlist-vpn.service` (an explicit, not-bound teardown). The ad-hoc `scripts/vpn_verify.sh` (needs the scoped sudoers rule documented in its header) is the manual companion. Both place the process in the namespace (`NetworkNamespacePath` / `ip netns exec`) with the namespace's Nord DNS bound over `/etc/resolv.conf`. In this mode no `NORDVPN_TOKEN` is needed anywhere — the tunnel unit reuses the session the operator established once with `nordvpn login --token`.
- **Steady state**: the tunnel's egress IP is **fixed for the tunnel's life** and changes only on a rebuild, so the per-N `--rotate-every` hop is a *best-effort* `systemctl restart` of the tunnel unit (fresh exit IP when permitted; a rebuild that is not permitted leaves the stable IP and the run continues with fingerprint-only rotation). A NordLynx peer-key rotation can silently stale the tunnel the same way — `systemctl restart amazon-wishlist-vpn` re-establishes it, and the verifier's `unknown`/failure rate is the canary.
- **Known issue — the verify pass can abort a few minutes in (unit runs only).** The run stops early with `tunnel error: namespace 'wlvpn' lost live egress after N verified book(s)`, observed at N = 7, 12, 15 and 17, between ~2 and ~3½ minutes in. Verified rows are kept and a re-run resumes, but a long pass never completed unattended, leaving freshly fetched deals at `deal_status IS NULL` — invisible on the tab, since `current_deals()` lists only `current` rows.

  **It is a false positive: the tunnel is healthy when the verifier gives up.** Probing the same namespace from outside at the exact second of an abort (2026-09-01 22:58:04) returned the Nord exit IP over both a DNS-resolving and a DNS-bypassed request, with WireGuard rekeying normally and bytes still flowing. Note the namespace is *structurally* leak-proof anyway — its only route is the tunnel — so the probe is a second layer over a guarantee that already holds, and each false positive costs a whole run while preventing nothing.

  **It only reproduces under the systemd unit.** The identical code, namespace, user and book list run standalone (`ip netns exec wlvpn runuser -u wishlist -- …`) reached 127 books where every unit run died by 17, and a later standalone pass completed **all 118 books with zero probe failures**.

  Ruled out, with evidence — don't re-test these:

  | Suspect | Evidence against |
  | ------------------------------- | ------------------------------------------------------------- |
  | The tunnel dying | Idle namespace stable 10 min; healthy at the abort instant |
  | `api.ipify.org` rate-limiting | 20/20 consecutive probes returned the IP |
  | A momentary blip | `_tunnel_live` already retries over a ~60s window |
  | `TasksMax` / memory | Cap 3758 vs ~84 tasks; `MemoryMax=infinity`, peak 671M |
  | Book count or elapsed time | Neither predicts it; only Chromium traffic being present does |

  Still unexplained: what in the unit context makes the probe's `curl` subprocess fail. Untested differences are `EnvironmentFile`, `XDG_RUNTIME_DIR`, and systemd's mount namespace (`BindReadOnlyPaths=…resolv.conf:/etc/resolv.conf`) versus `ip netns exec`'s own bind. **Start from the WARNING line**: `tunnel_egress_ip` now logs curl's rc (6 = DNS, 7 = connect, 28 = curl timeout, 124 = subprocess timeout), which previously sat at DEBUG and was discarded on every abort.

  Note one confound before concluding the problem is gone: excluding hidden deals cut the pass from 390 to 118 books at the same time, so a short run may simply outrun the failure rather than avoid it. The clean test is a **unit** run carrying both changes.

- **Fail-closed guarantee**: the verifier only ever fetches an Amazon page while the tunnel is *verifiably live*. Two layers: (1) structurally, the namespace's **only** route is the tunnel (`allowed-ips 0.0.0.0/0` + a single default route), so a dead/stale tunnel cannot leak Amazon traffic onto the host's plain IP; (2) operationally, `--netns` mode checks egress before the browser even launches, then **re-verifies it immediately before every single book** — if the tunnel loses live egress the run **aborts** (verified rows are kept; re-run resumes from `deal_status`) rather than continue. A dead tunnel therefore never results in Amazon access from the host IP.

### Configuration (env vars)

The BookBub credentials and session link are never committed (the credentials are real secrets; the link is a rotating signed token), and neither are the NordVPN credentials. The `BOOKBUB_*` / `LLM_*` / `NORDVPN_*` / `VERIFY_*` / `WISHLIST_VPN_*` settings in `app/config.py` (the `WISHLIST_VPN_USER` / `WISHLIST_VPN_LAN_SUBNET` / `WISHLIST_VPN_DNS` tunnel knobs are read by the tunnel scripts from `/etc/default/amazon-wishlist`):

| var | default | meaning |
| --- | --- | --- |
| `BOOKBUB_USERNAME` | *(unset)* | BookBub account email — primary login for the daily updater and probe. A real secret: supplied via env (`/etc/default/amazon-wishlist` on the host), **never committed**. |
| `BOOKBUB_PASSWORD` | *(unset)* | BookBub account password — same rules as `BOOKBUB_USERNAME`; rotate it if it is ever exposed. |
| `BOOKBUB_LOGIN_LINK` | *(unset)* | Optional ad-hoc login: the outbound auto-login link from the email. Used only when the account credentials are not configured, via `--link` or this var (one-shot builder / probe). |
| `BOOKBUB_DAILY_DEALS_BASE` | `https://www.bookbub.com/ebook-deals/daily-deals` | Daily-deals page; the day is the `?date=YYYYMMDD` query arg. |
| `BOOKBUB_DATE_FORMAT` | `%Y%m%d` | strftime format of the `?date=` value. |
| `BOOKBUB_OUTPUT` | `booklist.md` (repo root) | Where the report is written. |
| `DEALS_DB` | `data/deals.db` | The deals database (gitignored via `data/`): each day's deals, resolved Amazon links, the owned-in-grimmory audit, and the captured cover/description. Mirrored to a secondary whole-table via `GET /api/sync/deals`. |
| `DEALS_COVERS_DIR` | `data/covers` | Local directory for the deal cover images captured at verification (filenames `<ASIN>.<ext>`, served at `/covers/<name>`; gitignored via `data/`). Mirrored to a secondary alongside the deal rows. |
| `BOOKBUB_PER_PAGE_OPTIONS` | `[20, 40, 60, 80, 100]` | The per-page options offered by the BookBub Deals tab dropdown (a config constant in `app/config.py`, not env-driven — edit it there). An out-of-list `per_page` request snaps to the nearest option. |
| `BOOKBUB_PER_PAGE_DEFAULT` | `20` | Default per-page for the BookBub Deals tab (same constant in `app/config.py`). |
| `BOOKBUB_COVER_SIZE_OPTIONS` | `['1x', '2x', '4x']` | The cover-size options offered by the BookBub Deals tab dropdown (a config constant in `app/config.py`, not env-driven — edit it there). The *chosen* size is a stored runtime setting, not an env var. |
| `BOOKBUB_COVER_SIZE_DEFAULT` | `1x` | Default cover size for the BookBub Deals tab (same constant in `app/config.py`). |
| `BOOKBUB_TOOLTIP_SIZE_OPTIONS` | `['small', 'normal', 'large']` | The description-tooltip text-size options for the BookBub Deals tab dropdown (a config constant in `app/config.py`, not env-driven — edit it there). The *chosen* size is a stored runtime setting, not an env var. |
| `BOOKBUB_TOOLTIP_SIZE_DEFAULT` | `normal` | Default tooltip text size for the BookBub Deals tab (same constant in `app/config.py`). |
| `BOOKBUB_MIN_STARS_OPTIONS` | `[0, 3, 3.5, 4, 4.5]` | The min-star thresholds offered by the BookBub Deals "Min rating" filter (0 = show all; a config constant in `app/config.py`, not env-driven — edit it there). |
| `OWNED_UPDATE_DAY` / `OWNED_UPDATE_HOUR` / `OWNED_UPDATE_MINUTE` | `1` / `3` / `0` | When the monthly "Update Owned Books" scheduled run fires (server-local; default 1st of the month at 03:00). It is also triggerable on demand from the Settings tab. |
| `BOOKBUB_MIN_STARS_DEFAULT` | `0` | Default min-star filter for the BookBub Deals tab (same constant in `app/config.py`). |
| `BOOKBUB_HOUR_DEFAULT` / `BOOKBUB_MINUTE_DEFAULT` | `18` / `0` | Default daily time for the BookBub run. The time is **configurable at runtime in the Settings tab**; a stored setting overrides these defaults. |
| `BOOKBUB_BACKFILL_START` | `20260826` | Newest day the backfill processes, `YYYYMMDD`. |
| `BOOKBUB_BACKFILL_END` | `20260613` | Oldest day the backfill processes, `YYYYMMDD` (inclusive). |
| `BOOKBUB_BACKFILL_CHUNK` | `5` | Dates per login session (re-login between chunks). |
| `BOOKBUB_BACKFILL_DELAY` | `3` | Seconds between date navigations within a session (jittered ±25%). |
| `BOOKBUB_BACKFILL_BACKOFF` | `30` | Seconds to sleep between sessions; doubled when Cloudflare re-challenges are detected. |
| `BOOKBUB_BACKFILL_PROGRESS` | `data/backfill_progress.json` | Per-date status mirror for resuming (atomic writes; gitignored via `data/`). |
| `LLM_BASE_URL` | `http://192.168.1.40:11430/v1` | Local LLMConfig gateway (OpenAI-compatible) used for optional normalisation. |
| `LLM_MODEL` | *(unset = off)* | Model for `--llm-model`. The deterministic parse is the deliverable — an unavailable model/gateway only logs a warning and writes the parsed list as-is. |
| `LLM_TIMEOUT` | `120` | Timeout (seconds) for the LLM call. |
| `NORDVPN_TOKEN` | *(unset)* | Nord Account access token for the live-deal verifier's host-CLI mode. **Env-only** (or `--nord-token`) — never committed, never given a default. Current clients have no username/password login. |
| `NORDVPN_CLI` | `nordvpn` | Path to the NordVPN CLI (override if it is not on `PATH`). |
| `NORDVPN_START_COUNTRY` | first of `NORDVPN_COUNTRIES` | Starting exit country for `verify_deals.py` (`nordvpn connect`). |
| `NORDVPN_COUNTRIES` | `United States,Germany,Japan,United Kingdom,Canada,Australia` | Exit-country pool the verifier rotates through (`nordvpn rotate`), comma-separated. |
| `NORDVPN_ROTATE_EVERY` | `1000000` | Books per exit IP / fingerprint pair before an IP rotation (the `--rotate-every` default). **Dormant by default** (effectively never): in `--netns` tunnel mode the exit IP is fixed for the tunnel's life and the `wishlist` user has no sudoers rule to restart the tunnel unit, so rotation can't succeed anyway — it just logs "fingerprint-only rotation" and keeps the IP. To actually rotate, set a real N and grant `systemctl restart amazon-wishlist-vpn.service` in the scoped rule. |
| `VERIFY_DELAY_MIN` / `VERIFY_DELAY_MAX` | `2` / `6` | Jitter range (seconds) between per-book Amazon reads (anti-bot pacing). |
| `VERIFY_MAX_RETRIES` / `VERIFY_RETRY_BACKOFF` | `2` / `20` | Per-book retry budget for transient failures (block page / navigation failure / no price) and the backoff sleep between retries. |
| `VERIFY_PROGRESS` | `data/verify_progress.json` | Advisory progress mirror for the verifier (atomic writes; gitignored via `data/`). The resume cursor is `deal_status` in `DEALS_DB`, not this file. |
| `WISHLIST_VPN_USER` | *(unset)* | The operator user the tunnel scripts drive the `nordvpn` CLI as (in `/etc/default/amazon-wishlist`; that user must have run `nordvpn login --token <TOKEN>`). No default on purpose — the CLI cannot run as root. |
| `WISHLIST_VPN_LAN_SUBNET` | `192.168.1.0/24` | The host's LAN, kept off the tunnel during the brief host connect so SSH/NFS are not dropped. **Must match the host.** |
| `WISHLIST_VPN_NS` / `WISHLIST_VPN_IFACE` | `wlvpn` / `wlwg` | Network-namespace / WireGuard interface names. The tunnel unit, the verifier unit, and `--netns` must all agree. |
| `WISHLIST_VPN_DNS` | `103.86.96.100,1.1.1.1` | Per-namespace resolvers written to `/etc/netns/<NS>/resolv.conf` (Nord resolver + fallback), reached via the tunnel. |
| `WISHLIST_VPN_UNIT` | `amazon-wishlist-vpn.service` | The tunnel unit the verifier rebuilds on a per-N rotate (`systemctl restart`; needs root or a scoped sudoers rule). |
| `WISHLIST_VPN_ENDPOINT` | `https://api.ipify.org` | Endpoint the egress checks curl (must be reachable through the tunnel's DNS). |

The local LLM gateway is an **optional normalisation step, off by default**: `--llm-model`/`LLM_MODEL` reformat the parsed list via `POST {LLM_BASE_URL}/chat/completions`. It never blocks the write — on any failure the raw parsed list is written instead.

### Runtime settings (Settings tab)

The daily times and the cover size are **configurable at runtime** from the **Settings tab** (`GET /settings`, `POST /api/settings`, primary-only — a mirror never sees the page or the nav link): the daily **scrape time** (default **0:00**, from `WISHLIST_SCRAPE_HOUR`/`WISHLIST_SCRAPE_MINUTE`), the daily **BookBub time** (default **18:00**, from `BOOKBUB_HOUR_DEFAULT`/`BOOKBUB_MINUTE_DEFAULT`), the BookBub **cover size** (1x/2x/4x, default **1x**, from `BOOKBUB_COVER_SIZE_DEFAULT`) and the BookBub **tooltip text size** (small/normal/large, default **normal**, from `BOOKBUB_TOOLTIP_SIZE_DEFAULT`) — the BookBub Deals tab carries the matching dropdowns, so either place changes the shared setting.

Values are stored in the `settings` table of `wishlist.db` (primary-only — the mirror's `export_catalog` ignores it, so settings never sync). The env vars remain the **defaults**; a stored setting overrides them at the point of use. Changing a time calls `scheduler.reschedule_jobs()` so the new fire time applies immediately without a restart. The fetch, the `owned_in_grimmory` refresh, and the dedup all run at the single configured BookBub time — one daily cycle, one time.

Restarting the app (or the box) never triggers a bookbub/grimmory update: the BookBub job's next fire time is always in the future after startup, so the update happens only at the configured daily time.

## Notes / limitations

- Amazon actively rate-limits scrapers. The defaults (midnight start, 1-hour pacing, 4–9 s per-page jitter, browser-like headers) are tuned to fly under the radar for accounts with a handful of wishlists totaling around 1,000 items. Larger accounts or noisier IPs may still see occasional bot-blocks; the app preserves the prior state when this happens and saves the offending HTML to `data/diagnostics/`.
- **A failed scrape leaves every count untouched, on purpose** ("never clobber good data"), so a wishlist that has been bot-blocked for days still shows a matching Previous/Current pair on `/wishlists` and looks perfectly healthy. `last_scraped_at` is the only column that moves — it is flagged **stale** past `WISHLIST_STALE_AFTER_HOURS`. Read the age, not the counts.
- Amazon's HTML structure changes occasionally. If scrapes start returning 0 items *without* a "bot-blocked" status, check `data/diagnostics/` for the saved HTML and update the selectors in `app/scraper.py`.
- This is a single-user app; there is no auth on the web UI, and none on `/api/sync/*` either — which serves the whole database. Don't expose it to the public internet without a reverse proxy + auth in front, and firewall port 9060 to the peer / VPN subnet when running a mirror.
