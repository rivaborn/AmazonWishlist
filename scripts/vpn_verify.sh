#!/usr/bin/env bash
# Launch the Amazon Wishlist live-deal verifier (scripts/verify_deals.py)
# INSIDE the `wlvpn` network namespace so all of its Amazon traffic (Playwright
# price reads) egresses through the Nord WireGuard tunnel, while the rest of the
# box stays on its normal connection. This is the interactive counterpart of
# amazon-wishlist-verify.service — the unit needs no sudo; this wrapper does.
#
# It runs the verifier as the INVOKING user: `ip netns exec` needs root, so the
# root part is scoped to that single command and runuser drops back to the
# invoking user for the python process itself.
#
# The tunnel must already be up:  systemctl start amazon-wishlist-vpn.service
# (the wrapper refuses to run when the namespace is missing).
#
# When invoked by the operator directly (the intended way), it needs this
# scoped sudoers rule — scoped to this one command, NOT blanket sudo — e.g. in
# /etc/sudoers.d/amazon-wishlist-verify:
#   <operator> ALL=(ALL) NOPASSWD: /usr/sbin/ip netns exec wlvpn runuser -u <operator> -- *
# When invoked as `sudo bash scripts/vpn_verify.sh` instead, no sudoers rule is
# needed (the script is already root and SUDO_USER selects the verifier user).
#
# Usage:
#   bash scripts/vpn_verify.sh --check
#   bash scripts/vpn_verify.sh --limit 25 --rotate-every 10
# All extra arguments are forwarded to verify_deals.py.
set -euo pipefail

cd "$(dirname "$0")"; ROOT="$PWD/.."
NS="${WISHLIST_VPN_NS:-wlvpn}"
# The invoking (non-root) user: normally us, or the user who sudo'd us.
U="${SUDO_USER:-$(id -un)}"

if [ ! -e "/var/run/netns/$NS" ]; then
  echo "ERROR: netns '$NS' not found; bring the VPN namespace up first" >&2
  echo "       (systemctl start amazon-wishlist-vpn.service)." >&2
  exit 1
fi

# Repo venv python if present (local dev), else the system python3.
PY="${VERIFY_PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3

# `ip netns exec` needs root: scoped sudo for the operator, a no-op when the
# script is already root.
if [ "$(id -u)" = 0 ]; then
  NETNS_EXEC=(ip netns exec)
else
  NETNS_EXEC=(sudo ip netns exec)
fi

exec "${NETNS_EXEC[@]}" "$NS" runuser -u "$U" -- \
  env WISHLIST_VPN_NS="$NS" "$PY" "$ROOT/scripts/verify_deals.py" \
  --netns "$NS" "$@"
