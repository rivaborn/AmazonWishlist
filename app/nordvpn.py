"""Thin, credential-safe wrapper around the ``nordvpn`` CLI.

The live-deal verifier (``scripts/verify_deals.py``) needs to hop to a fresh
NordVPN exit IP (and a fresh browser fingerprint) every N books so the traffic
does not look like one machine hammering Amazon. This module is the *single*
place that talks to the ``nordvpn`` CLI, so any difference in the local CLI's
flags/output is adapted here, not in the orchestrator.

Credential safety (repo rule — never commit a secret or a rotating token):

* The login token is read from the ``NORDVPN_TOKEN`` environment variable,
  or passed as ``--nord-token``. It is **never** written to a committed file
  and has no default here. A token is the only automatable option left:
  current NordVPN clients (5.x) removed username/password login entirely and
  offer only browser SSO, ``--callback``, and ``--token``.
* ``NORDVPN_COUNTRIES`` (and optional ``NORDVPN_CITIES``) is the rotation pool
  so ``rotate()`` lands on a different server/exit IP each call.

ASSUMPTION (recorded): a ``nordvpn`` CLI is installed on the machine and
supports ``login`` / ``connect`` / ``disconnect`` / ``status``. If it is absent
or not connectable, :func:`_run` raises :class:`NordvpnError` with the clear
underlying reason — this is an environment prerequisite for the live-deal
verification, not something to paper over. Pacing / anti-bot and the
per-N-books invocation live in the orchestrator, not here.

There is a second, "tunnel mode" section (``netns_exists`` / ``tunnel_egress_ip``
/ ``netns_egress_ok`` / ``rebuild_tunnel`` / ``tunnel_rotate``) for the Ubuntu
deployment, where the verifier runs INSIDE a network namespace whose only
route is a NordLynx (WireGuard) tunnel (scripts/vpn_netns_up.sh +
amazon-wishlist-vpn.service). That path needs no CLI or credentials and never
raises — a missing tool is a clean False/None the caller logs.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    NORDVPN_CITIES,
    NORDVPN_CLI,
    NORDVPN_COUNTRIES,
    WISHLIST_VPN_ENDPOINT,
    WISHLIST_VPN_NS,
    WISHLIST_VPN_UNIT,
)

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
    "netns_exists",
    "tunnel_egress_ip",
    "netns_egress_ok",
    "rebuild_tunnel",
    "tunnel_rotate",
]

log = logging.getLogger("nordvpn")

# Environment variable names for the credentials (values never live here).
ENV_TOKEN = "NORDVPN_TOKEN"

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


def login(token: str | None = None) -> str:
    """Log in via ``nordvpn login --token …``.

    The token comes from the argument or the ``NORDVPN_TOKEN`` environment —
    never from a committed file. Generate it in the Nord Account dashboard
    (Services -> NordVPN -> Access token); it is revoked by ``nordvpn logout``.

    This used to shell ``login --username … --password …``, which every
    current client rejects: NordVPN 5.x offers only browser SSO, ``--callback``
    and ``--token`` (verified against 5.3.0, 2026-09-01). Password login is
    gone, so there is no fallback to keep.
    """
    tok = (token or os.environ.get(ENV_TOKEN, "")).strip()
    if not tok:
        raise NordvpnError(
            f"missing NordVPN token: set {ENV_TOKEN} or pass a token. Current "
            "clients have no username/password login — generate an access "
            "token in the Nord Account dashboard."
        )
    return _run(["login", "--token", tok])


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


# ---------- Tunnel mode (netns-based) ----------------------------------------
# On Ubuntu the live-deal verifier runs INSIDE a network namespace whose only
# route is a NordLynx (WireGuard) tunnel (scripts/vpn_netns_up.sh +
# amazon-wishlist-vpn.service). Inside that namespace there is no host CLI to
# drive, and the tunnel's egress IP is fixed for the tunnel's life — so this
# section is the netns analogue of the host-CLI wrapper above:
#
#   netns_exists      is the namespace bound (live)?
#   tunnel_egress_ip  the egress IPv4 of the CALLING process (a plain curl;
#                     for a process inside the namespace that IS the tunnel IP)
#   netns_egress_ok   does `ip netns exec <ns> curl` get an answer (tunnel live)?
#   rebuild_tunnel    best-effort `systemctl restart <unit>` (fresh session +
#                     fresh exit IP; the up script proves egress before it
#                     exits 0, and a Type=oneshot restart is synchronous)
#   tunnel_rotate     rebuild_tunnel() then tunnel_egress_ip() — the netns
#                     analogue of the host CLI rotate()
#
# All of them treat a missing tool (no ip/curl/systemctl) or a timeout as a
# clean failure (False/None) that the caller logs, never an exception — the
# orchestrator decides what "best effort" means. No credential handling here:
# the session is pre-negotiated by the tunnel unit as the operator's user.
# NOTE (recorded): a process already inside the namespace keeps its (old)
# namespace until it is restarted — a rebuild re-creates the namespace for
# NEWLY-launched consumers, so tunnel_rotate() is a best-effort IP refresh.

# Seconds for the egress curls (the endpoint is a tiny response; the ceiling is
# mostly the tunnel's worst-case first-packet latency).
_TUNNEL_TIMEOUT_SEC = 15.0


def _run_cmd(args: list[str], *, timeout: float = 30.0) -> tuple[int, str]:
    """Run an external tool, returning ``(rc, combined_output)`` — never raises.

    Unlike :func:`_run` (the nordvpn-CLI wrapper that raises
    :class:`NordvpnError`), the tunnel helpers treat a missing tool or a
    timeout as a clean, loggable failure: rc 127 (command not found) or 124
    (timed out), with a short note in the output. Overridable in tests.
    """
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"command not found: {args[0]!r}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s: {' '.join(args)}"
    out = (proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or "")
    return proc.returncode, out


def netns_exists(ns: str | None = None) -> bool:
    """True when the namespace is bound (i.e. the tunnel unit is active).

    Primary check: systemd holds the namespace by a symlink under
    ``/var/run/netns`` (no privileges needed). Fallback: ``ip netns list``
    (needs iproute2; a missing ``ip`` is a clean ``False``).
    """
    name = ns or WISHLIST_VPN_NS
    try:
        if Path("/var/run/netns", name).exists():
            return True
    except OSError:
        pass
    rc, out = _run_cmd(["ip", "netns", "list"], timeout=10.0)
    if rc != 0:
        return False
    # Lines look like "wlvpn: /var/run/netns/wlvpn".
    return any(line.split(":")[0].strip() == name for line in out.splitlines() if line.strip())


def tunnel_egress_ip(timeout: float | None = None) -> str | None:
    """The egress IPv4 of the CALLING process, or ``None`` when unreadable.

    A plain (un-namespaced) curl to ``WISHLIST_VPN_ENDPOINT``: for a process
    placed inside the tunnel namespace that is the tunnel's exit IP; on the
    plain host it is the host's own IP. Best-effort by design — any failure
    (no curl, timeout, no IP in the body) is ``None``, never an exception.
    """
    t = float(timeout) if timeout else _TUNNEL_TIMEOUT_SEC
    # Give the subprocess a little more slack than curl's own --max-time so
    # curl aborts first and we see a normal non-zero rc rather than a TimeoutExpired.
    rc, out = _run_cmd(
        ["curl", "-s", "--max-time", str(int(t)), WISHLIST_VPN_ENDPOINT], timeout=t + 5.0
    )
    if rc != 0:
        # WARNING, not debug: this is the single fact that distinguishes a dead
        # tunnel from a probe that merely could not run. curl rc 6 = DNS did not
        # resolve, 7 = connect refused, 28 = curl's own timeout, and _run_cmd's
        # synthetic 124 = the subprocess itself timed out (i.e. the box was too
        # busy to answer, not the tunnel). The verifier aborts a whole run on
        # this signal, so losing the reason at DEBUG made every abort
        # indistinguishable and cost days of misdiagnosis.
        log.warning(
            "tunnel_egress_ip: probe of %s failed (curl rc=%s): %s",
            WISHLIST_VPN_ENDPOINT, rc, out.strip()[:200] or "(no output)",
        )
        return None
    m = _IP_RE.search(out)
    return m.group(0) if m else None


def netns_egress_ok(ns: str | None = None, timeout: float | None = None) -> bool:
    """True when ``ip netns exec <ns> curl <endpoint>" gets a real answer.

    Requires root (``ip netns exec`` needs CAP_NET_ADMIN) or a scoped sudoers
    rule; a missing ``ip`` is a clean ``False``. rc 0 AND a non-empty body —
    the same bar scripts/vpn_netns_up.sh uses to prove egress.
    """
    name = ns or WISHLIST_VPN_NS
    t = float(timeout) if timeout else _TUNNEL_TIMEOUT_SEC
    rc, out = _run_cmd(
        ["ip", "netns", "exec", name, "curl", "-s", "--max-time", str(int(t)), WISHLIST_VPN_ENDPOINT],
        timeout=t + 10.0,
    )
    if not (rc == 0 and out.strip()):
        log.debug("netns_egress_ok: ns %s not live (rc=%s): %s", name, rc, out.strip()[:200])
        return False
    return True


def rebuild_tunnel(unit: str | None = None, timeout: float = 180.0) -> bool:
    """Best-effort ``systemctl restart <unit>`` — True when the tunnel (re)came up.

    The tunnel unit is Type=oneshot (RemainAfterExit), so a successful restart
    returns only after ExecStart (vpn_netns_up.sh) has finished — which itself
    verifies egress before exiting 0. Needs root; a non-root/failed restart is
    a clean ``False`` for the caller to log, never an exception.
    """
    name = unit or WISHLIST_VPN_UNIT
    rc, out = _run_cmd(["systemctl", "restart", name], timeout=timeout)
    if rc != 0:
        log.warning("rebuild_tunnel: systemctl restart %s failed (rc=%s): %s", name, rc, out.strip()[:200])
        return False
    return True


def tunnel_rotate(ns: str | None = None, unit: str | None = None) -> str | None:
    """Refresh the tunnel's exit IP: rebuild the tunnel, return the egress IP.

    The netns analogue of the host-CLI :func:`rotate`: a rebuilt tunnel
    negotiates a fresh WireGuard session (fresh assigned address / exit IP).
    Returns ``None`` when the rebuild fails or the egress IP is unreadable —
    the caller (scripts/verify_deals.py) treats that as "continue on the
    current IP" rather than an error. See the section note above about
    processes already inside the namespace keeping their old one.
    """
    if not rebuild_tunnel(unit):
        return None
    new_ip = tunnel_egress_ip()
    if not new_ip:
        log.warning("tunnel_rotate: rebuild ok but no egress IP was readable")
        return None
    log.info("tunnel rebuilt; egress IP now %s", new_ip)
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
    ap.add_argument("--nord-token", default=None, help=f"Access token (default: {ENV_TOKEN} env)")
    args = ap.parse_args()

    try:
        if args.login or args.nord_token:
            login(args.nord_token)
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
