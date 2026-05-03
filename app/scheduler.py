import logging
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import LOG_PATH, SCRAPE_HOUR, SCRAPE_MINUTE
from .services import run_full_scrape

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


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
    sched.add_job(
        run_full_scrape,
        trigger=CronTrigger(hour=SCRAPE_HOUR, minute=SCRAPE_MINUTE),
        id="daily_scrape",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    log.info("Scheduler started; daily scrape at %02d:%02d local", SCRAPE_HOUR, SCRAPE_MINUTE)
    _scheduler = sched
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
