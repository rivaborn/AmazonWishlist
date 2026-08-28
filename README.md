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
- **/books** — every available book across all wishlists, sorted by current price ascending. Header shows total count, lowest, and highest.
- **/no-price** — split into "Kindle edition unavailable" and "Removed from Amazon" (HTTP 404).
- **/price-drops** — every historical snapshot that dropped vs. its baseline, filtered.
- **/wishlists** — add/remove wishlist URLs, run scrape on demand. On a secondary this becomes a read-only table plus a mirror-status panel. Each row shows when it was last scraped and the item count from that scrape. The Run-scrape button shows a live progress bar and a "Waiting until HH:MM:SS" indicator between paced scrapes.
- **/purchased** — books marked purchased. They are excluded from every other view and shown here regardless of whether they are still on a wishlist.
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

## Grimmory book catalog (data/grimmory.db)

A one-off export of the home-lab **Grimmory** (BookLore) ebook libraries into this repo's `data/` directory. It is a separate SQLite file from `wishlist.db` with its own schema — a static catalog snapshot (title, author, publisher, date published, ISBN). Nothing in the web app reads or writes it; it is rebuilt by hand, not on a cron.

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

A one-off pull of the daily [BookBub](https://www.bookbub.com) ebook-deal list, keyed off the **outbound link in the daily BookBub email**. That link is a signed, single-use auto-login: following it logs you into bookbub.com and lands on that day's daily-deals page. The deals themselves are served from `https://www.bookbub.com/ebook-deals/daily-deals?date=YYYYMMDD`, and any date can be opened in the same logged-in session.

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

A quick probe that prints the day's deals without writing the file (also supports `--headless`/`--headful` if Cloudflare keeps challenging headless):

```bash
python -m app.bookbub --link '<outbound link>' [--date YYYYMMDD]
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

### Configuration (env vars)

The session link is never committed (it is a rotating signed token). The `BOOKBUB_*` / `LLM_*` settings in `app/config.py`:

| var | default | meaning |
| --- | --- | --- |
| `BOOKBUB_LOGIN_LINK` | *(unset)* | The outbound auto-login link from the email. Supplied per run via `--link` or this var. |
| `BOOKBUB_DAILY_DEALS_BASE` | `https://www.bookbub.com/ebook-deals/daily-deals` | Daily-deals page; the day is the `?date=YYYYMMDD` query arg. |
| `BOOKBUB_DATE_FORMAT` | `%Y%m%d` | strftime format of the `?date=` value. |
| `BOOKBUB_OUTPUT` | `booklist.md` (repo root) | Where the report is written. |
| `DEALS_DB` | `data/deals.db` | The deals database (gitignored via `data/`): each day's deals, resolved Amazon links, and the owned-in-grimmory audit. |
| `BOOKBUB_BACKFILL_START` | `20260826` | Newest day the backfill processes, `YYYYMMDD`. |
| `BOOKBUB_BACKFILL_END` | `20260613` | Oldest day the backfill processes, `YYYYMMDD` (inclusive). |
| `BOOKBUB_BACKFILL_CHUNK` | `5` | Dates per login session (re-login between chunks). |
| `BOOKBUB_BACKFILL_DELAY` | `3` | Seconds between date navigations within a session (jittered ±25%). |
| `BOOKBUB_BACKFILL_BACKOFF` | `30` | Seconds to sleep between sessions; doubled when Cloudflare re-challenges are detected. |
| `BOOKBUB_BACKFILL_PROGRESS` | `data/backfill_progress.json` | Per-date status mirror for resuming (atomic writes; gitignored via `data/`). |
| `LLM_BASE_URL` | `http://192.168.1.40:11430/v1` | Local LLMConfig gateway (OpenAI-compatible) used for optional normalisation. |
| `LLM_MODEL` | *(unset = off)* | Model for `--llm-model`. The deterministic parse is the deliverable — an unavailable model/gateway only logs a warning and writes the parsed list as-is. |
| `LLM_TIMEOUT` | `120` | Timeout (seconds) for the LLM call. |

The local LLM gateway is an **optional normalisation step, off by default**: `--llm-model`/`LLM_MODEL` reformat the parsed list via `POST {LLM_BASE_URL}/chat/completions`. It never blocks the write — on any failure the raw parsed list is written instead.

## Notes / limitations

- Amazon actively rate-limits scrapers. The defaults (midnight start, 1-hour pacing, 4–9 s per-page jitter, browser-like headers) are tuned to fly under the radar for accounts with a handful of wishlists totaling around 1,000 items. Larger accounts or noisier IPs may still see occasional bot-blocks; the app preserves the prior state when this happens and saves the offending HTML to `data/diagnostics/`.
- **A failed scrape leaves every count untouched, on purpose** ("never clobber good data"), so a wishlist that has been bot-blocked for days still shows a matching Previous/Current pair on `/wishlists` and looks perfectly healthy. `last_scraped_at` is the only column that moves — it is flagged **stale** past `WISHLIST_STALE_AFTER_HOURS`. Read the age, not the counts.
- Amazon's HTML structure changes occasionally. If scrapes start returning 0 items *without* a "bot-blocked" status, check `data/diagnostics/` for the saved HTML and update the selectors in `app/scraper.py`.
- This is a single-user app; there is no auth on the web UI, and none on `/api/sync/*` either — which serves the whole database. Don't expose it to the public internet without a reverse proxy + auth in front, and firewall port 9060 to the peer / VPN subnet when running a mirror.
