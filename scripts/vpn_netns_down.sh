#!/usr/bin/env bash
# Tear down the `wlvpn` network namespace and its WireGuard interface.
# MUST run as root (ExecStop of amazon-wishlist-vpn.service). Idempotent: each
# piece is a no-op when already absent, so running it twice (or on a box that
# never came up) is safe.
#
# `--dry-run` (or WISHLIST_VPN_DRY_RUN=1) prints the plan and exits 0 without
# executing anything. NS/IFACE are read from the same WISHLIST_VPN_* knobs as
# vpn_netns_up.sh so the pair always addresses the same namespace.
set -uo pipefail

NS="${WISHLIST_VPN_NS:-wlvpn}"
IFACE="${WISHLIST_VPN_IFACE:-wlwg}"
DRY_RUN="${WISHLIST_VPN_DRY_RUN:-0}"
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log(){ echo "[wlvpn] $*"; }
run(){
  if [ "$DRY_RUN" = 1 ]; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

if [ "$DRY_RUN" = 1 ]; then
  log "DRY RUN — printing the teardown plan for NS=$NS IFACE=$IFACE; executing nothing."
else
  [ "${EUID:-$(id -u)}" -eq 0 ] || { log "ERROR: must run as root (normally via amazon-wishlist-vpn.service ExecStop)"; exit 1; }
fi

# Bring the interface down FIRST — deleting an *up* WireGuard interface can
# block. Then kill any leftover process still INSIDE the namespace: a lingering
# Chromium pins the netns and makes `ip netns del` block (this was the recurring
# ~90s teardown hang that marked every daily run failed despite the data work
# having completed). Finally delete with a hard `timeout` so teardown can never
# hang the stop job.
run ip link set "$IFACE" down 2>/dev/null || true
if [ -d "/var/run/netns/$NS" ]; then
  pids="$(ip netns pids "$NS" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    [ "$DRY_RUN" = 1 ] && echo "  [dry-run] kill leftover netns pids: $pids"
    for p in $pids; do run kill "$p" 2>/dev/null || true; done
    sleep 1
  fi
fi
run timeout 20 ip netns del "$NS" 2>/dev/null || true
run timeout 20 ip link del "$IFACE" 2>/dev/null || true
run rm -f "/etc/netns/$NS/resolv.conf"

if [ "$DRY_RUN" = 1 ]; then
  log "dry run complete: nothing was executed."
else
  log "namespace '$NS' torn down"
fi
