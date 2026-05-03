"""Login tab routes.

GET  /login                  → page with current status + iframe
GET  /api/login/status       → JSON status (polled by the page)
POST /api/login/start        → spawn the headful Chromium + VNC stack
POST /api/login/save         → dump storage_state.json, tear down
POST /api/login/cancel       → tear down without saving
POST /api/login/heartbeat    → reset the idle timer (called by the page poll)
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from .. import login_session

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("/login")
def login_page(request: Request):
    mgr = login_session.get_manager()
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "active": "login",
            "status": mgr.status(),
        },
    )


@router.get("/api/login/status")
def login_status():
    return login_session.get_manager().status()


@router.post("/api/login/start")
def login_start():
    try:
        return login_session.get_manager().start()
    except login_session.LoginError as e:
        raise HTTPException(500, str(e))


@router.post("/api/login/save")
def login_save():
    try:
        return login_session.get_manager().save()
    except login_session.LoginError as e:
        raise HTTPException(400, str(e))


@router.post("/api/login/cancel")
def login_cancel():
    return login_session.get_manager().cancel()


@router.post("/api/login/heartbeat")
def login_heartbeat():
    login_session.get_manager().heartbeat()
    return JSONResponse({"ok": True})
