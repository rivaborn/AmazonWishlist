"""In-app interactive Amazon login.

When the user opens the Login tab and clicks Start, we spin up:

  - Xvfb              — a virtual display
  - Chromium (headful) under that display, driven by Playwright so we can
                        dump storage_state on save. Loads existing session
                        if present (resume partial sessions).
  - x11vnc            — bridges the Xvfb display to a TCP VNC port
  - websockify        — wraps the VNC port as a WebSocket and serves the
                        bundled noVNC web client

The user's browser embeds the noVNC client in an iframe and gets a real
Amazon login page. On Save the server calls `context.storage_state(path=…)`
and writes `data/storage_state.json` atomically. On Cancel or idle timeout
the stack is torn down without writing anything.

Only one session at a time. Guarded by a lock; second Start while one is
running returns the existing session.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import (
    CHROMIUM_USER_DATA_DIR,
    LOGIN_IDLE_TIMEOUT_SEC,
    NOVNC_WEB_DIR,
    STORAGE_STATE,
    VNC_PORT,
    XVFB_DISPLAY,
    XVFB_RESOLUTION,
)

log = logging.getLogger(__name__)

# x11vnc default. Xvfb display ":99" maps to TCP port 5900 + 99 = 5999, but
# x11vnc with `-rfbport` lets us pin to anything. We'll pin to 5900 since
# we're using -localhost and it's wrapped by websockify anyway.
_VNC_LOCAL_PORT = 5900


class LoginError(RuntimeError):
    pass


class _Session:
    """All the runtime handles for one in-flight login session."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.started_at: float = time.monotonic()
        self.last_heartbeat: float = self.started_at

        self.xvfb: Optional[subprocess.Popen] = None
        self.x11vnc: Optional[subprocess.Popen] = None
        self.websockify: Optional[subprocess.Popen] = None

        # Playwright handles
        self.playwright = None
        self.browser = None
        self.context = None  # BrowserContext
        self.page = None     # initial page

        self.error: Optional[str] = None


class LoginSessionManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: Optional[_Session] = None
        self._watchdog: Optional[threading.Thread] = None

    # ---- public API -------------------------------------------------------

    def start(self) -> dict:
        """Spawn the VNC stack + a Playwright-controlled headful Chromium.

        Returns a status dict the route layer can hand to the template.
        Idempotent: if a session is already running, returns its status.
        """
        with self._lock:
            if self._session is not None:
                self._session.last_heartbeat = time.monotonic()
                return self._public_status_locked()

            sess = _Session(token=secrets.token_urlsafe(24))
            try:
                self._spawn(sess)
            except Exception as e:
                log.exception("Failed to start login session")
                self._teardown(sess)
                raise LoginError(str(e)) from e
            self._session = sess

        # Start watchdog outside the lock.
        self._ensure_watchdog()
        return self.status()

    def save(self) -> dict:
        """Dump storage_state to disk, then tear the stack down."""
        with self._lock:
            sess = self._session
            if sess is None:
                raise LoginError("no active login session")
            try:
                self._dump_storage_state(sess)
            finally:
                self._teardown(sess)
                self._session = None
        return self.status()

    def cancel(self) -> dict:
        """Tear the stack down without touching storage_state."""
        with self._lock:
            sess = self._session
            if sess is not None:
                self._teardown(sess)
                self._session = None
        return self.status()

    def status(self) -> dict:
        with self._lock:
            return self._public_status_locked()

    def heartbeat(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.last_heartbeat = time.monotonic()

    # ---- internals --------------------------------------------------------

    def _public_status_locked(self) -> dict:
        sess = self._session
        running = sess is not None
        token = sess.token if sess else None
        started_at = (
            datetime.fromtimestamp(time.time() - (time.monotonic() - sess.started_at))
            .isoformat(timespec="seconds")
            if sess else None
        )
        # storage_state on disk
        try:
            stat = STORAGE_STATE.stat() if STORAGE_STATE.is_file() else None
        except OSError:
            stat = None
        if stat is not None:
            saved_at = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
            saved_age_sec = int(time.time() - stat.st_mtime)
        else:
            saved_at = None
            saved_age_sec = None

        return {
            "running": running,
            "token": token,
            "started_at": started_at,
            "idle_timeout_sec": LOGIN_IDLE_TIMEOUT_SEC,
            "vnc_port": VNC_PORT,
            "saved_storage_state_at": saved_at,
            "saved_storage_state_age_sec": saved_age_sec,
            "error": sess.error if sess else None,
        }

    def _spawn(self, sess: _Session) -> None:
        # 1. Verify the binaries we need exist before doing anything else.
        for binname in ("Xvfb", "x11vnc", "websockify"):
            if shutil.which(binname) is None:
                raise LoginError(
                    f"required binary '{binname}' not found in PATH; "
                    f"install it on the host (apt install xvfb x11vnc websockify novnc)"
                )

        # 2. Xvfb
        sess.xvfb = subprocess.Popen(
            ["Xvfb", XVFB_DISPLAY, "-screen", "0", XVFB_RESOLUTION, "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Give Xvfb a moment to create its socket.
        for _ in range(20):
            if Path(f"/tmp/.X11-unix/X{XVFB_DISPLAY.lstrip(':')}").exists():
                break
            time.sleep(0.1)

        # 3. Playwright + Chromium under that display.
        from playwright.sync_api import sync_playwright  # type: ignore

        sess.playwright = sync_playwright().start()
        os.environ["DISPLAY"] = XVFB_DISPLAY  # picked up by chromium subprocess
        CHROMIUM_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

        sess.browser = sess.playwright.chromium.launch(
            headless=False,
            args=[
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-translate",
                "--disable-infobars",
                # Headless-friendly hardening for systemd-restricted env
                "--no-zygote",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-crash-reporter",
                "--disable-breakpad",
                "--no-crash-upload",
            ],
        )
        # storage_state is only valid on new_context, not launch_persistent_context.
        # Resume an existing session if a saved file is present, otherwise start fresh.
        new_ctx_kwargs = {"viewport": {"width": 1280, "height": 800}}
        if STORAGE_STATE.is_file() and STORAGE_STATE.stat().st_size > 200:
            new_ctx_kwargs["storage_state"] = str(STORAGE_STATE)
        sess.context = sess.browser.new_context(**new_ctx_kwargs)
        sess.page = sess.context.new_page()
        sess.page.goto("https://www.amazon.com/", wait_until="domcontentloaded")

        # 4. x11vnc — bridge Xvfb to localhost VNC.
        sess.x11vnc = subprocess.Popen(
            [
                "x11vnc",
                "-display", XVFB_DISPLAY,
                "-rfbport", str(_VNC_LOCAL_PORT),
                "-localhost",
                "-shared",
                "-forever",
                "-quiet",
                "-nopw",
                "-noxdamage",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for x11vnc to listen.
        time.sleep(0.5)

        # 5. websockify — wrap the VNC port as a WebSocket + serve noVNC HTML.
        ws_args = [
            "websockify",
            f"--web={NOVNC_WEB_DIR}",
            f"0.0.0.0:{VNC_PORT}",
            f"localhost:{_VNC_LOCAL_PORT}",
        ]
        sess.websockify = subprocess.Popen(
            ws_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)

    def _dump_storage_state(self, sess: _Session) -> None:
        if sess.context is None:
            raise LoginError("no browser context to dump from")
        STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORAGE_STATE.with_suffix(".json.tmp")
        sess.context.storage_state(path=str(tmp))
        os.replace(tmp, STORAGE_STATE)
        try:
            os.chmod(STORAGE_STATE, 0o600)
        except OSError:
            pass
        log.info("storage_state.json saved (%d bytes)", STORAGE_STATE.stat().st_size)

    def _teardown(self, sess: _Session) -> None:
        # Best-effort kill of every spawned bit. Order: chromium → x11vnc →
        # websockify → Xvfb. Each step is wrapped so a failure doesn't strand
        # the rest.
        for closer, label in (
            (lambda: sess.context and sess.context.close(), "chromium-context"),
            (lambda: sess.browser and sess.browser.close(), "chromium-browser"),
            (lambda: sess.playwright and sess.playwright.stop(), "playwright"),
        ):
            try:
                closer()
            except Exception:
                log.exception("teardown step %s failed", label)

        for proc, label in (
            (sess.x11vnc, "x11vnc"),
            (sess.websockify, "websockify"),
            (sess.xvfb, "Xvfb"),
        ):
            if proc is None:
                continue
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                log.exception("teardown step %s failed", label)

    def _ensure_watchdog(self) -> None:
        if self._watchdog and self._watchdog.is_alive():
            return
        t = threading.Thread(target=self._watchdog_loop, daemon=True, name="login-watchdog")
        t.start()
        self._watchdog = t

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(15)
            with self._lock:
                sess = self._session
                if sess is None:
                    return  # nothing to watch — exit thread
                idle = time.monotonic() - sess.last_heartbeat
                if idle > LOGIN_IDLE_TIMEOUT_SEC:
                    log.warning(
                        "login session idle for %ds (>%ds); tearing down",
                        int(idle), LOGIN_IDLE_TIMEOUT_SEC,
                    )
                    self._teardown(sess)
                    self._session = None
                    return


# Singleton accessor
_manager: Optional[LoginSessionManager] = None
_manager_lock = threading.Lock()


def get_manager() -> LoginSessionManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = LoginSessionManager()
        return _manager
