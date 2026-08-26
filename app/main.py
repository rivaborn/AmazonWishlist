import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config
from .db import init_db
from .routes import api as api_routes
from .routes import login as login_routes
from .routes import pages as page_routes
from .routes import sync as sync_routes
from .scheduler import start_scheduler, stop_scheduler
from .services import resume_if_interrupted


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    if config.is_secondary():
        # A mirror never scrapes, so there is no interrupted run to resume.
        # Sync once at startup instead of waiting out the interval -- a restart
        # is exactly when the local copy is most likely to be behind.
        from .sync_client import run_sync

        threading.Thread(target=run_sync, name="sync-startup", daemon=True).start()
    else:
        # If a previous run was killed mid-scrape (e.g. the service was restarted
        # by an OS library upgrade), pick up where it left off instead of waiting
        # for the next daily cron and leaving some wishlists on stale data.
        resume_if_interrupted()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title="Amazon Wishlist Tracker", lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)
app.include_router(page_routes.router)
app.include_router(api_routes.router)
app.include_router(login_routes.router)
app.include_router(sync_routes.router)
