import logging
import subprocess
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config, settings
from .config import (BOOKBUB_HOUR_DEFAULT, BOOKBUB_MINUTE_DEFAULT, LOG_PATH,
   SCRAPE_HOUR, SCRAPE_MINUTE, SYNC_HOUR, SYNC_MINUTE,
   OWNED_UPDATE_DAY, OWNED_UPDATE_HOUR, OWNED_UPDATE_MINUTE)
from .services import run_full_scrape

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

# The one-shot systemd unit the scheduler's bookbub job triggers (it places
# bookbub_daily.py inside the wlvpn netns and tears the VPN down when done —
# the app itself can never be placed in the netns; see install_systemd.sh for
# the scoped sudoers rule that lets the app user run these exact commands).
BOOKBUB_UNIT = "amazon-wishlist-bookbub.service"


def _configured_times() -> tuple[int, int, int, int]:
    """(scrape_hour, scrape_minute, bookbub_hour, bookbub_minute) server-local.

    Stored settings override the env/config defaults (the Settings tab is the
    single place to change them); a missing/corrupt setting falls back to the
    default, so a fresh install behaves exactly as it always has (scrape at
    SCRAPE_HOUR:SCRAPE_MINUTE, BookBub at 18:00).
    """
    return (
        settings.get_int("scrape_hour", SCRAPE_HOUR),
        settings.get_int("scrape_minute", SCRAPE_MINUTE),
        settings.get_int("bookbub_hour", BOOKBUB_HOUR_DEFAULT),
        settings.get_int("bookbub_minute", BOOKBUB_MINUTE_DEFAULT),
    )


def run_bookbub() -> None:
    """Trigger the daily BookBub updater unit (the scheduler's bookbub job).

    Runs ``sudo -n systemctl start amazon-wishlist-bookbub.service`` — the
    oneshot that fetches the day's deals, refreshes the owned-in-grimmory
    flags, dedupes, and re-verifies, all inside the wlvpn netns, tearing the
    VPN down when it finishes. Called ONLY at the configured daily time:
    a CronTrigger never fires at startup, so a restart never triggers a
    BookBub/Grimmory update. On a box without systemd (local dev) or without
    the scoped sudoers rule, this logs a warning and changes nothing.
    """
    try:
        proc = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", BOOKBUB_UNIT],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        log.warning(
            "bookbub daily: %s unavailable on this box (no systemd?); skipping",
            BOOKBUB_UNIT,
        )
        return
    if proc.returncode != 0:
        log.warning(
            "bookbub daily: systemctl start %s failed (rc=%d): %s",
            BOOKBUB_UNIT,
            proc.returncode,
            (proc.stderr or proc.stdout).strip()[:300],
        )
    else:
        log.info("bookbub daily: triggered %s", BOOKBUB_UNIT)


def _configure_log() -> None:
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler
    _configure_log()
    sched = BackgroundScheduler(timezone=None)  # server local time

    # The daily times come from the settings table when present (the Settings
    # tab) and fall back to the env/config defaults. Both jobs are pure
    # CronTriggers: they fire ONLY at their scheduled times, never at startup
    # (a restart therefore never triggers a scrape or a BookBub/Grimmory
    # update — requirement 4).
    scrape_hour, scrape_minute, bookbub_hour, bookbub_minute = _configured_times()

    # Exactly one of these two jobs, ever. A secondary that came up as a primary
    # would start scraping Amazon from a second IP against the same throwaway
    # account, which is the whole thing mirror mode exists to avoid -- hence the
    # role is logged loudly at every startup.
    if config.is_secondary():
        from .sync_client import run_sync

        sched.add_job(
            run_sync,
            trigger=CronTrigger(hour=SYNC_HOUR, minute=SYNC_MINUTE),
            id="sync_pull",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        log.info(
            "Role: SECONDARY (mirror). Scraping disabled; syncing from %s daily at %02d:%02d local",
            config.PRIMARY_URL or "<WISHLIST_PRIMARY_URL unset!>",
            SYNC_HOUR,
            SYNC_MINUTE,
        )
    else:
        sched.add_job(
            run_full_scrape,
            trigger=CronTrigger(hour=scrape_hour, minute=scrape_minute),
            id="daily_scrape",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # The daily BookBub cycle (fetch + owned refresh + dedup + re-verify)
        # runs at ONE configurable time via the oneshot unit above.
        sched.add_job(
            run_bookbub,
            trigger=CronTrigger(hour=bookbub_hour, minute=bookbub_minute),
            id="bookbub_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # Monthly "Update Owned Books" (refresh grimmory.db + move owned books to
        # Purchased) on the 1st, off-peak. Runs on the HOST (the Grimmory server
        # is unreachable from the netns); it is long, so it runs in a background
        # thread. Lazy import: owned_update pulls grimmory/build deps.
        from . import owned_update  # noqa: E402
        sched.add_job(
            owned_update.trigger_owned_update,
            trigger=CronTrigger(day=OWNED_UPDATE_DAY, hour=OWNED_UPDATE_HOUR,
                                minute=OWNED_UPDATE_MINUTE),
            id="owned_update",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        log.info(
            "Role: PRIMARY. Daily scrape at %02d:%02d local; BookBub daily at "
            "%02d:%02d local",
            scrape_hour,
            scrape_minute,
            bookbub_hour,
            bookbub_minute,
        )

    sched.start()
    _scheduler = sched
    return sched


def reschedule_jobs() -> None:
    """Re-trigger the primary's cron jobs from the current settings.

    Called by the Settings API after a save so a time change takes effect on
    the very next day without a restart. No-op when the scheduler is not
    running or on a secondary (whose only job, sync_pull, is env-driven, not
    settings-driven).
    """
    if _scheduler is None or not _scheduler.running or config.is_secondary():
        return
    scrape_hour, scrape_minute, bookbub_hour, bookbub_minute = _configured_times()
    _scheduler.reschedule_job(
        "daily_scrape", trigger=CronTrigger(hour=scrape_hour, minute=scrape_minute)
    )
    _scheduler.reschedule_job(
        "bookbub_daily", trigger=CronTrigger(hour=bookbub_hour, minute=bookbub_minute)
    )
    log.info(
        "Rescheduled: daily scrape %02d:%02d, BookBub daily %02d:%02d (server local)",
        scrape_hour,
        scrape_minute,
        bookbub_hour,
        bookbub_minute,
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
