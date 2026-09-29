#!/usr/bin/env bash
# Launch the Amazon Wishlist live-deal verifier (scripts/verify_deals.py)
# INSIDE the `wlvpn` network namespace so all of its Amazon traffic (Playwright
# price reads) egresses through the Nord WireGuard tunnel, while the rest of the
# box stays on its normal connection. This is the interactive counterpart of
# amazon-wishlist-verify.service.
#
# The tunnel belongs to VPNManager (github.com/rivaborn/VPNManager, since
# 2026-09-29): lease `wishlist` = netns wlvpn. This wrapper acquires the lease if
# it is not up (one paced, serialized NordVPN connect), runs the verifier inside it
# with `vpnmgr exec` — as the INVOKING user, never root — and releases the lease
# afterwards if it was the one that brought it up (the lease is on demand).
#
# Needs VPNManager's sudoers grant for the operator (`vpnmgr *`), installed by
# VPNManager's deploy/install.sh. Run it as the operator, not with sudo.
#
# Usage:
#   bash scripts/vpn_verify.sh --check
#   bash scripts/vpn_verify.sh --limit 25 --rotate-every 10
# All extra arguments are forwarded to verify_deals.py.
set -uo pipefail

cd "$(dirname "$0")"; ROOT="$PWD/.."
NS="${WISHLIST_VPN_NS:-wlvpn}"
LEASE="${WISHLIST_VPN_LEASE:-wishlist}"
[ "$(id -u)" != 0 ] || { echo "run as the operator user, not root (vpnmgr exec runs the verifier as you)" >&2; exit 1; }
command -v vpnmgr >/dev/null || { echo "ERROR: vpnmgr not installed (github.com/rivaborn/VPNManager)" >&2; exit 1; }

# Repo venv python if present (local dev), else the system python3.
PY="${VERIFY_PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3

was_up=0
sudo -n vpnmgr check "$LEASE" --json 2>/dev/null | grep -q '"healthy": true' && was_up=1
if [ "$was_up" = 0 ]; then
  sudo -n vpnmgr acquire "$LEASE" --json >/dev/null \
    || { echo "ERROR: could not acquire VPNManager lease '$LEASE' (sudo vpnmgr status)" >&2; exit 1; }
fi

sudo -n vpnmgr exec "$LEASE" -- env WISHLIST_VPN_NS="$NS" WISHLIST_VPN_LEASE="$LEASE" \
  "$PY" "$ROOT/scripts/verify_deals.py" --netns "$NS" "$@"
rc=$?

if [ "$was_up" = 0 ]; then
  sudo -n vpnmgr release "$LEASE" --json >/dev/null || echo "WARNING: release of $LEASE failed" >&2
fi
exit $rc
