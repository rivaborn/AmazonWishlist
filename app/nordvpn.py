"""Thin, credential-safe wrapper around the ``nordvpn`` CLI.

The live-deal verifier (``scripts/verify_deals.py``) needs to hop to a fresh
NordVPN exit IP (and a fresh browser fingerprint) every N books so the traffic
does not look like one machine hammering Amazon. This module is the *single*
place that talks to the ``nordvpn`` CLI, so any difference in the local CLI's
flags/output is adapted here, not in the orchestrator.

Credential safety (repo rule — never commit a secret or a rotating token):

* The account credentials are read from the ``NORDVPN_USERNAME`` /
  ``NORDVPN_PASSWORD`` environment variables, or passed as ``--nord-user`` /
  ``--nord-pass``. They are **never** written to a committed file and have no
  default here.
* ``NORDVPN_COUNTRIES`` (and optional ``NORDVPN_CITIES``) is the rotation pool
  so ``rotate()`` lands on a different server/exit IP each call.

ASSUMPTION (recorded): a ``nordvpn`` CLI is installed on the machine and
supports ``login`` / ``connect`` / ``disconnect`` / ``status``. If it is absent
or not connectable, :func:`_run` raises :class:`NordvpnError` with the clear
underlying reason — this is an environment prerequisite for the live-deal
verification, not something to paper over. Pacing / anti-bot and the
per-N-books invocation live in the orchestrator, not here.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from .config import NORDVPN_CITIES, NORDVPN_CLI, NORDVPN_COUNTRIES

__all__ = [
    "NordvpnError",
    "NordvpnState",
    "cli_present",
    "login",
    "connect",
    "disconnect",
    "status",
    "ip",
    "rotate",
    "reset_rotation",
]

log = logging.getLogger("nordvpn")

# Environment variable names for the credentials (values never live here).
ENV_USERNAME = "NORDVPN_USERNAME"
ENV_PASSWORD = "NORDVPN_PASSWORD"

# A best-effort IPv4 in CLI output (the exact ``status`` layout varies, so we
# grab the first public-looking dotted quad).
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_COUNTRY_RE = re.compile(r"(?:country|region|server location)\s*[:=]?\s*([A-Za-z][A-Za-z .'\-]+)", re.I)
_CITY_RE = re.compile(r"(?:city|location)\s*[:=]?\s*([A-Za-z][A-Za-z .'\-]+)", re.I)

# Module-level rotation cursor so consecutive picks advance through the pool.
_pick_i = 0


class NordvpnError(RuntimeError):
    """The ``nordvpn`` CLI is missing, errored, or refused the operation."""


@dataclass
class NordvpnState:
    """A best-effort snapshot of the VPN state from ``nordvpn status``."""

    connected: bool
    country: str | None = None
    city: str | None = None
    ip: str | None = None
    raw: str = field(default="", repr=False)


def cli_present() -> bool:
    """True when the ``nordvpn`` CLI resolves (on PATH, or via ``NORDVPN_CLI``)."""
    return shutil.which(NORDVPN_CLI) is not None


def _run(args: list[str], *, timeout: float = 90) -> str:
    """Run ``nordvpn <args>`` and return combined stdout+stderr.

    Raises :class:`NordvpnError` when the CLI is not installed, times out, or
    exits non-zero (with the CLI's own message). Overridable in tests.
    """
    cli = NORDVPN_CLI
    if shutil.which(cli) is None:
        raise NordvpnError(
            f"nordvpn CLI not found ({cli!r}): install it or point NORDVPN_CLI at its "
            "path. A working `nordvpn` CLI is an environment prerequisite for the "
            "live-deal verification."
        )
    try:
        proc = subprocess.run([cli, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise NordvpnError(f"nordvpn CLI not found ({cli!r}): is it installed and on PATH?") from exc
    except subprocess.TimeoutExpired as exc:
        raise NordvpnError(f"nordvpn {' '.join(args)} timed out after {timeout:.0f}s") from exc
    out = (proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise NordvpnError(f"nordvpn {' '.join(args)} failed (rc={proc.returncode}): {out.strip()}")
    return out


def login(username: str | None = None, password: str | None = None) -> str:
    """Log in via ``nordvpn login --username … --password …``.

    Credentials come from the arguments or the ``NORDVPN_USERNAME`` /
    ``NORDVPN_PASSWORD`` environment — never from a committed file.
    """
    user = (username or os.environ.get(ENV_USERNAME, "")).strip()
    pw = password or os.environ.get(ENV_PASSWORD, "")
    if not user or not pw:
        raise NordvpnError(
            f"missing NordVPN credentials: set {ENV_USERNAME}/{ENV_PASSWORD} "
            "or pass username/password"
        )
    return _run(["login", "--username", user, "--password", pw])


def connect(country: str, city: str | None = None) -> str:
    """``nordvpn connect <country> [city]``."""
    if city:
        return _run(["connect", country, city])
    return _run(["connect", country])


def disconnect() -> str:
    """``nordvpn disconnect``."""
    return _run(["disconnect"])


def status() -> NordvpnState:
    """Parse ``nordvpn status`` into a :class:`NordvpnState` (best-effort)."""
    out = _run(["status"])
    return _parse_status(out)


def ip() -> str | None:
    """The current exit IP (from ``status``), or ``None`` when unreadable."""
    return status().ip


def _parse_status(out: str) -> NordvpnState:
    """Best-effort parse of ``nordvpn status`` output (layout varies by CLI)."""
    low = out.lower()
    connected = ("connected" in low) and ("disconnected" not in low) and ("not connected" not in low)
    ipm = _IP_RE.search(out)
    cm = _COUNTRY_RE.search(out)
    ym = _CITY_RE.search(out)
    return NordvpnState(
        connected=connected,
        country=(cm.group(1).strip() if cm else None),
        city=(ym.group(1).strip() if ym else None),
        ip=(ipm.group(0) if ipm else None),
        raw=out,
    )


def _cycle_pick(options: list[str]) -> str:
    """Advance the rotation cursor and return one entry of ``options``.

    Deterministic-ish: the cursor increments each call, so consecutive picks
    walk the pool rather than re-picking the same entry.
    """
    global _pick_i
    if not options:
        raise NordvpnError("empty rotation pool")
    _pick_i = (_pick_i + 1) % len(options)
    return options[_pick_i]


def reset_rotation() -> None:
    """Reset the rotation cursor (for deterministic runs/tests)."""
    global _pick_i
    _pick_i = 0


def rotate() -> str:
    """Hop to a different server/exit IP and return the new exit IP.

    ``disconnect`` -> pick a ``(country, city)`` that differs from the current
    one -> ``connect`` -> read (and return) the new exit IP.
    """
    cur = status()
    pool = list(NORDVPN_COUNTRIES)
    if not pool:
        raise NordvpnError("NORDVPN_COUNTRIES is empty; nothing to rotate through")

    diff = [c for c in pool if c != cur.country] or (pool if not cur.country else [])
    if not diff:
        raise NordvpnError(
            f"cannot rotate away from current country {cur.country!r}: "
            "the pool offers no other country"
        )
    country = _cycle_pick(diff)

    city = None
    if NORDVPN_CITIES:
        cities = [c for c in NORDVPN_CITIES if c != cur.city] or list(NORDVPN_CITIES)
        city = _cycle_pick(cities)

    log.info("rotating NordVPN: %s -> %s%s", cur.country or "?", country, f"/{city}" if city else "")
    disconnect()
    connect(country, city)
    new_ip = ip()
    if not new_ip:
        raise NordvpnError("rotate(): connect succeeded but no exit IP was readable")
    if new_ip == cur.ip:
        log.warning("rotate(): exit IP unchanged (%s) — the server may reuse the same range", new_ip)
    return new_ip


def _main() -> int:
    """``python -m app.nordvpn`` — probe / control the VPN.

    Default prints status + exit IP; ``--login`` logs in first; ``--connect
    COUNTRY [CITY]`` connects; ``--rotate`` performs one rotation.
    """
    import argparse

    ap = argparse.ArgumentParser(description="Probe / control the NordVPN CLI.")
    ap.add_argument("--login", action="store_true", help="Log in before reporting status")
    ap.add_argument("--rotate", action="store_true", help="Rotate to a new exit IP, then report")
    ap.add_argument("--connect", nargs=2, metavar=("COUNTRY", "CITY"), help="Connect to COUNTRY [CITY]")
    ap.add_argument("--nord-user", default=None, help=f"Username (default: {ENV_USERNAME} env)")
    ap.add_argument("--nord-pass", default=None, help=f"Password (default: {ENV_PASSWORD} env)")
    args = ap.parse_args()

    try:
        if args.login or args.nord_user or args.nord_pass:
            login(args.nord_user, args.nord_pass)
            print("logged in")
        if args.connect:
            country, city = args.connect
            connect(country, city)
            print(f"connected to {country}" + (f" / {city}" if city else ""))
        if args.rotate:
            new_ip = rotate()
            print(f"rotated, new ip: {new_ip}")

        st = status()
    except NordvpnError as exc:
        print(f"nordvpn error: {exc}")
        return 1

    print(f"connected: {st.connected}")
    if st.country:
        print(f"country: {st.country}" + (f" / {st.city}" if st.city else ""))
    print(f"ip: {st.ip}")
    return 0 if st.connected and st.ip else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(_main())
