# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Self-hosted FastAPI app that watches Amazon **ebook** wishlists and surfaces deals, the full catalog, missing-price items, and price-drop history on a small server-rendered web UI (port 9060). Single-user, no auth on the UI. SQLite for storage, APScheduler for the daily cron, two interchangeable scraper backends.

`README.md` is the authoritative operator manual (env vars, deploy, login flow, troubleshooting, data model, status API). Read it before changing deploy/runtime behaviour. `Wishlist.md` is the original feature spec/changelog — useful for intent, not current state.

## Commands

```bash
# Local dev (from repo root)
python -m venv .venv && .venv/Scripts/activate   # Windows; use source .venv/bin/activate on Linux
pip install -r requirements.txt
uvicorn app.main:app --reload --port 9060        # open http://localhost:9060/

# Tests — there is no pytest suite. The single test is an end-to-end smoke
# test that runs every page + ingest + drop math against a fake payload (no network):
python scripts/_smoke.py                          # exit 0 = pass; uses a throwaway temp DB

# Production deploy on Ubuntu (idempotent — re-run after every change):
git pull && sudo bash scripts/install_systemd.sh # rsyncs to /opt/amazon-wishlist, refreshes venv, restarts unit
journalctl -u amazon-wishlist -f                  # service logs
sudo tail -f /opt/amazon-wishlist/data/scrape.log # scrape-specific rotating log
```

There is no linter/formatter config and no type-checker in CI; `# type: ignore` comments are used at a few call sites where dataclass/Literal typing is loose.

## Architecture

Request/scrape flow, top to bottom:

- **`app/main.py`** — FastAPI app + `lifespan` that calls `init_db()`, `start_scheduler()`, then `resume_if_interrupted()` (picks up a scrape a restart killed mid-run). Mounts three routers: `pages` (HTML), `api` (JSON + form posts), `login`.
- **`app/scheduler.py`** — APScheduler `BackgroundScheduler` in **server-local time** (`timezone=None`), one cron job `run_full_scrape` at `SCRAPE_HOUR:SCRAPE_MINUTE`. Also configures the rotating file log here.
- **`app/services.py`** — the core. Owns: the lock-guarded scrape-progress dict (module-level, single-process app) which is **mirrored to `data/scrape_progress.json` on every update** so a run survives a restart; `run_full_scrape(resume=False)` (the orchestrator); `resume_if_interrupted()`; `ingest_wishlist()`; and all the read-side query helpers that back the pages.
- **`app/scraper.py`** (httpx) and **`app/scraper_playwright.py`** (Playwright) — the two scraper backends. Same input/output contract: `fetch_wishlist(url, *, list_label) -> list[ScrapedItem]`. The Playwright module **reuses** `_parse_item_row`, `_is_antibot_stub`, `_next_page_url`, `_save_diagnostic`, `_amazon_root` from `scraper.py` — the page DOM is identical logged-in vs anonymous, only fetching differs. Keep parsing logic in `scraper.py` and import it; don't fork it.
- **`app/login_session.py`** — the in-app interactive Amazon login (see below).
- **`app/db.py`** — SQLite schema + `_migrate()` (additive `ALTER TABLE` steps, each a no-op if already applied) + a `connect()` context manager (WAL, `foreign_keys=ON`, autocommit `isolation_level=None`).
- **`app/routes/`** — thin HTTP layer; all real logic is in `services`/`login_session`. `pages.py` renders Jinja templates from `app/templates/`; `api.py` handles form posts and the scrape/purchased JSON endpoints; `login.py` proxies to the login-session singleton.

### Scraper-path selection (important invariant)

`config.use_playwright()` is re-evaluated **on every scrape run** and returns true iff `data/storage_state.json` exists and is >200 bytes. So dropping in (or `mv`-ing away) that file flips the backend between authenticated/anonymous with no restart. `run_full_scrape()` opens **one** Playwright context for the whole run (chromium spin-up is expensive) and falls back to httpx if Playwright import/launch fails — it logs `Scraper path: playwright|httpx` at the top of each run.

### The "never clobber good data" rule (most important behavioural invariant)

A wishlist's membership (`wishlist_book`) is **replaced wholesale** inside `ingest_wishlist()` only on a *successful* scrape. When a scrape fails, we must NOT ingest an empty/partial list, or we'd wipe items the wishlist still has. The scrapers encode this with three exception types raised instead of returning a short list:

- `BotDetected` — Amazon's anti-automation stub on the **first** page (`_is_antibot_stub`: body < 30KB + captcha/automation marker).
- `FetchFailed` — HTTP/network error, *or* anti-bot/zero-rows on a **later** page (partial pagination is treated as failure, not truncation).
- `LoginExpired` — Playwright only; saved session is logged out. Aborts the whole run (no point continuing with a dead session).

- `SuspiciousShrink` — raised by `ingest_wishlist()`, not by a scraper: the fetch succeeded but came back below `INGEST_SHRINK_FLOOR` (0.8) of the stored membership. Pagination can end early *without* erroring, and the ingest replaces membership wholesale, so a short-but-clean scrape silently drops everything it missed (2026-08-10: list 5 ingested 320 of 554; 2026-08-12: list 7 ingested 170 of 407). The first short scrape is refused and recorded in `wishlist.pending_shrink_count`; a **second consecutive** short scrape confirms the shrink is real and is accepted, so a genuinely pruned list recovers within a day instead of stranding. Any normal-sized scrape clears the marker.

`run_full_scrape()` catches each, marks that wishlist's count 0, advances progress, and leaves the prior DB state intact. Any page yielding zero rows or the anti-bot stub gets its raw HTML dumped to `data/diagnostics/<timestamp>_<label>.html` for selector debugging. **If you touch the scrapers, preserve this: failures raise, successes return the full list.**

⚠️ **The corollary nobody expects: a failed scrape makes the `/wishlists` row look healthier, not worse.** Preserving prior state means `previous_item_count`, the membership count *and* `last_scraped_at` all stay put, so a list bot-blocked for a week still renders a perfectly matched Previous/Current pair. Those two columns are a scrape-*size* sanity check, never a change log. The only column that moves is the age — hence the `stale` flag computed in `list_wishlists()`. Anything new that reports wishlist health must key off `stale_hours`, not off the counts.

### Pagination: end-of-list is not "no next link"

Amazon keeps issuing fresh `paginationToken`s well past the end of a wishlist, each re-serving rows already collected — so `_next_page_url()` never dries up, the `seen_urls` guard never matches (every token is unique), and dedupe-by-ASIN absorbs the repeats invisibly. Before this was fixed a 520-item list burned all 100 pages every night, ~75 of them pure duplicates; four lists doing that is ~300 wasted requests/day against a throwaway account, which is request budget spent buying anti-bot blocks. Two bounds, both in `config.py` and shared by **both** scrapers:

- **`MAX_STALE_PAGES`** (3) — stop after N consecutive pages that add no new ASIN. This is the real end-of-list signal; the counter resets on any page that adds something.
- **`MAX_PAGES_PER_WISHLIST`** (100) — the hard budget. Exiting on it *while a next page is still offered* means we hold a prefix, so `_check_pagination_complete()` (in `scraper.py`, imported by the Playwright path) raises `FetchFailed`. Reaching the cap is never a successful scrape.

### Pacing (anti-bot)

`run_full_scrape()` paces wishlist starts to at most one per `SCRAPE_PER_WISHLIST_SECONDS` (default 3600s) and jitters per-page requests by `REQUEST_DELAY_MIN..MAX`. During the pacing gap, progress shows `waiting=true` + `next_starts_at`. The gap is computed from the **wall-clock** `last_started_at` (persisted), not a monotonic clock, so it's honoured across a restart. Set `WISHLIST_PER_LIST_SECONDS=0` to disable pacing for one-off local testing.

### Resume after restart (progress persistence)

A full run takes hours (1 list/hour), so it overlaps `apt-daily-upgrade`; a security upgrade to a linked lib (e.g. `libssl`) makes `needrestart` restart the service mid-scrape. To survive that, `_progress` is mirrored to `data/scrape_progress.json` on **every** `_progress_update()` (atomic tmp+`os.replace`), including the remaining-wishlist queue `pending_ids` and `last_started_at`. On startup `resume_if_interrupted()` (called from `lifespan`) reloads the file and, if `pending_ids` is non-empty and the run is younger than `RESUME_MAX_AGE_SEC`, relaunches `run_full_scrape(resume=True)` in a daemon thread to scrape **only** the unfinished lists. Invariant making `pending_ids` a reliable "was interrupted" signal: a normal finish drains it via `_complete_wishlist()`, and a `LoginExpired` abort clears it — so only an abrupt process death leaves it populated. Don't increment `done`/`items_total` by hand; route per-wishlist completion through `_complete_wishlist()` so the queue and counters stay in sync.

### In-app login stack (`login_session.py`)

The Login tab logs into a **separate, throwaway** Amazon account whose session powers the authenticated scraper. `LoginSessionManager` is a lock-guarded singleton allowing one session at a time. Start spawns, in order: `Xvfb` (virtual display) → Playwright-driven **headful** Chromium on that display → `x11vnc` → `websockify` (wraps VNC as a WebSocket and serves the bundled noVNC client at `WISHLIST_VNC_PORT`). The browser embeds noVNC in an iframe; the user logs into Amazon by hand. Save calls `context.storage_state(path=…)` writing `data/storage_state.json` atomically (tmp + `os.replace`, chmod 0600); teardown kills all subprocesses. A watchdog thread tears the stack down after `WISHLIST_LOGIN_IDLE_TIMEOUT` idle (the page sends heartbeats while active). Note: `DISPLAY` must be set in `os.environ` *before* `sync_playwright().start()` — the driver forks and captures env at that moment.

### Read-side queries & price math

The page views (`deals`, `all_books_by_price`, `no_price_books`, `price_drop_history`, `purchased_books`) all build off snapshots in `price_snapshot` (append-only). The shared `_LATEST_BASE` CTE pulls the latest snapshot per ASIN plus the previous price and the all-time high, joined to `book` and `wishlist_book` so only books **currently on a wishlist** appear (purchased books are the exception — they show regardless of membership). Drop math (`_row_to_book`) computes dollar/percent drop against a **basis**: `prev` (previous observed price) or `list` (Amazon's strikethrough/list price), selected per-request via the `basis` query param. Prices are stored as integer cents throughout; only format to dollars at the template boundary.

## Conventions & gotchas

- **Prices are integer cents** end to end (`*_price_cents`). `_to_cents()` is the only parser.
- **`availability`** is exactly one of `available` | `kindle_unavailable` | `page_404` (see `models.Availability`). `/books` and `/deals` show only `available`; `/no-price` splits the other two.
- **Amazon DOM drift is expected.** If scrapes return 0 items *without* a bot-block status, the selectors in `scraper.py` (`_parse_item_row`, the `NEXT_TOKEN_PATTERNS`/`LEK_TOKEN_RE` pagination tokens) are stale — check `data/diagnostics/` and update them there; the Playwright path inherits the fix automatically.
- **systemd hardening is deliberately soft** (`amazon-wishlist.service`): `PrivateTmp=false`, `ProtectSystem=false` because Chromium's multi-process model (zygote/crashpad, shared `/tmp` sockets) breaks under the stricter settings. Don't "harden" these without testing the Login flow and a Playwright scrape end to end.
- The `PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64` env in the service file is a workaround for Playwright's OS-version gate on newer Ubuntu — see README troubleshooting before removing it.
- The `data/` dir (DB, logs, `storage_state.json`, `diagnostics/`, chromium profile) is gitignored and preserved across deploys; never commit it.
