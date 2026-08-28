#!/usr/bin/env bash
# Establish the `wlvpn` network namespace with a NordLynx (WireGuard) tunnel so
# that ONLY processes placed in this namespace egress through NordVPN; the rest
# of the box (SSH / LAN / mgmt) keeps its normal connection. MUST run as root —
# it is normally started by amazon-wishlist-vpn.service (see install_systemd.sh).
#
# NordLynx == WireGuard, but the NordVPN CLI has no per-process split tunnel, so
# we briefly connect on the host to negotiate a WireGuard session, read back its
# keys/endpoint, disconnect (restoring host routing), and rebuild an equivalent
# WireGuard interface inside the namespace.
#
# Idempotent: if the namespace already exists with working egress, it is left
# as-is. Rebuilding on every restart would churn the exit IP for no reason.
#
# Per-host knobs (all optional, set in /etc/default/amazon-wishlist so a
# single script serves every host):
#   WISHLIST_VPN_NS          namespace name            (default wlvpn)
#   WISHLIST_VPN_IFACE       WireGuard iface name      (default wlwg)
#   WISHLIST_VPN_USER        the operator user whose `nordvpn login --token` +
#                            nordvpn-group membership is borrowed for the CLI
#                            (NO default: the CLI cannot run as root)
#   WISHLIST_VPN_LAN_SUBNET  the host's LAN, kept OFF the tunnel during the
#                            brief host connect (default 192.168.1.0/24 — set
#                            it per host or the SSH session drops for seconds)
#   WISHLIST_VPN_DNS         comma-separated per-namespace resolvers
#                            (default 103.86.96.100,1.1.1.1)
#   WISHLIST_VPN_DRY_RUN=1   (or `--dry-run`) prints the command plan and exits
#                            0 without executing anything.
#
# Prerequisites: nordvpn CLI installed + logged in for WISHLIST_VPN_USER,
# wireguard-tools (wg) installed, that user in the nordvpn group.
set -euo pipefail

NS="${WISHLIST_VPN_NS:-wlvpn}"
IFACE="${WISHLIST_VPN_IFACE:-wlwg}"
LAN_SUBNET="${WISHLIST_VPN_LAN_SUBNET:-192.168.1.0/24}"
DNS_LIST="${WISHLIST_VPN_DNS:-103.86.96.100,1.1.1.1}"
NVUSER="${WISHLIST_VPN_USER:-}"
DRY_RUN="${WISHLIST_VPN_DRY_RUN:-0}"
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log(){ echo "[wlvpn] $*"; }

# run <cmd...>: execute in normal mode, print in dry-run mode.
run(){
  if [ "$DRY_RUN" = 1 ]; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

# Drive the nordvpn CLI as the operator user (in the `nordvpn` group), not root.
nv(){ runuser -u "$NVUSER" -- nordvpn "$@"; }

if [ "$DRY_RUN" = 1 ]; then
  log "DRY RUN — printing the command plan; executing nothing."
  log "plan: NS=$NS IFACE=$IFACE USER=${NVUSER:-<unset>} LAN_SUBNET=$LAN_SUBNET DNS=$DNS_LIST"
fi

# ---- preflight (exec mode only; dry run prints what it would check) ----------
if [ "$DRY_RUN" = 1 ]; then
  log "preflight would check: EUID=0, command -v nordvpn, command -v wg, id -u WISHLIST_VPN_USER, nv account (logged in?)"
else
  [ "${EUID:-$(id -u)}" -eq 0 ] || { log "ERROR: must run as root (normally via amazon-wishlist-vpn.service)"; exit 1; }
  command -v nordvpn >/dev/null || { log "ERROR: nordvpn CLI not installed"; exit 1; }
  command -v wg      >/dev/null || { log "ERROR: wireguard-tools (wg) missing — apt install wireguard-tools"; exit 1; }
  [ -n "$NVUSER" ] || { log "ERROR: WISHLIST_VPN_USER is not set: the nordvpn CLI must be driven as the operator user who ran \`nordvpn login --token\` (set it in /etc/default/amazon-wishlist)"; exit 1; }
  id -u "$NVUSER" >/dev/null 2>&1 || { log "ERROR: user '$NVUSER' does not exist (WISHLIST_VPN_USER)"; exit 1; }
  nv account >/dev/null 2>&1 || { log "ERROR: nordvpn not logged in for '$NVUSER' (run: nordvpn login --token <TOKEN>)"; exit 1; }
fi

# ---- idempotent: adopt a live namespace, otherwise tear down stale bits ------
if [ "$DRY_RUN" = 1 ]; then
  log "idempotency: if 'ip netns list | grep -qw $NS' AND 'ip netns exec $NS curl -s --max-time 8 https://api.ipify.org' succeed -> adopt and exit 0; else rebuild"
else
  if ip netns list | grep -qw "$NS"; then
    if ip netns exec "$NS" curl -s --max-time 8 https://api.ipify.org >/dev/null 2>&1; then
      log "namespace '$NS' already up with working egress; leaving as-is."
      exit 0
    fi
    log "stale '$NS' present; rebuilding."
  fi
  ip netns del "$NS" 2>/dev/null || true
  ip link  del "$IFACE" 2>/dev/null || true
fi

# ---- brief host connect: keep LAN/SSH/Tailscale OFF the tunnel --------------
log "connecting NordVPN (as ${NVUSER:-<unset>}) to negotiate a WireGuard session..."
run nv set killswitch disabled    || true
run nv set technology nordlynx    || true
run nv allowlist add subnet "$LAN_SUBNET"  || true
run nv allowlist add subnet 100.64.0.0/10  || true   # Tailscale CGNAT range
run nv allowlist add port 22              || true   # SSH
run nv connect

# ---- harvest the WireGuard session, then restore host routing ----------------
if [ "$DRY_RUN" = 1 ]; then
  log "harvest (after waiting up to 20 s for the 'nordlynx' iface):"
  echo "  [dry-run] wg show nordlynx private-key                              # PRIV (session private key)"
  echo "  [dry-run] wg show nordlynx dump | awk '\$1==\"peer\"{print \$2; exit}'         # PEER (peer public key)"
  echo "  [dry-run] wg show nordlynx dump | awk '\$1==\"endpoint\"{print \$2; exit}'  # ENDPOINT (server ip:port)"
  echo "  [dry-run] ip -4 addr show nordlynx | awk '/inet /{print \$2; exit}'  # ADDR (assigned, e.g. 10.5.0.2/32)"
  echo "  [dry-run] nv disconnect                         # host routing back to normal"
  echo "  [dry-run] (abort if any of PRIV/PEER/ENDPOINT/ADDR is empty)"
else
  for _ in $(seq 1 20); do wg show nordlynx >/dev/null 2>&1 && break; sleep 1; done
  PRIV=$(wg show nordlynx private-key 2>/dev/null || true)
  # `wg show <iface> dump` is indented multi-line ("nordlynx:" / "\tpeer <key>" /
  # "\t\tendpoint <ip:port>" ...), so parse by the KEY on each line rather than by
  # fixed line/field numbers — indentation and the header line vary by version.
  PEER=$(wg show nordlynx dump | awk '$1=="peer"{print $2; exit}')
  ENDPOINT=$(wg show nordlynx dump | awk '$1=="endpoint"{print $2; exit}')
  ADDR=$(ip -4 addr show nordlynx | awk '/inet /{print $2; exit}')
  nv disconnect >/dev/null
  sleep 1
  if [ -z "$PRIV" ] || [ -z "$PEER" ] || [ -z "$ENDPOINT" ] || [ -z "$ADDR" ]; then
    log "ERROR: could not extract WireGuard session (priv/peer/endpoint/addr missing)"
    exit 1
  fi
fi

# ---- rebuild an equivalent WireGuard interface INSIDE the namespace ----------
log "rebuilding the session as namespace iface '$IFACE' in netns '$NS'..."
run ip netns add "$NS"
run ip link add "$IFACE" type wireguard
run ip link set "$IFACE" netns "$NS"      # move the iface INTO the netns
if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] KEYF=\$(mktemp); chmod 600 KEYF; printf '%s\n' \"\$PRIV\" > KEYF   # session private key, mode 0600"
else
  KEYF=$(mktemp); chmod 600 "$KEYF"; printf '%s\n' "$PRIV" > "$KEYF"
fi
# allowed-ips 0.0.0.0/0 + the single default route below make the namespace
# leak-proof: there is no other route, so a process cannot fall back to the
# host's NIC. persistent-keepalive 25 keeps the NAT mapping alive on the Nord
# side. Encrypted UDP still leaves via the host's physical NIC.
run ip netns exec "$NS" wg set "$IFACE" \
    private-key "${KEYF:-(0600 keyfile)}" peer "${PEER:-(peer public key)}" \
    endpoint "${ENDPOINT:-(server ip:port)}" allowed-ips 0.0.0.0/0 \
    persistent-keepalive 25
[ "$DRY_RUN" = 1 ] || rm -f "${KEYF:-}"
run ip -n "$NS" addr add "${ADDR:-(assigned addr)}" dev "$IFACE"
run ip -n "$NS" link set "$IFACE" up
run ip -n "$NS" link set lo up
run ip -n "$NS" route add default dev "$IFACE"

# ---- per-namespace DNS (the kernel reads /etc/netns/<NS>/resolv.conf) -------
# Without it the namespace inherits the host resolv.conf, which typically points
# at a LAN resolver the namespace cannot reach — the classic "tunnel works,
# nothing resolves" failure.
run mkdir -p "/etc/netns/$NS"
if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] /etc/netns/$NS/resolv.conf would contain:"
  for r in ${DNS_LIST//,/ }; do printf '      nameserver %s\n' "$r"; done
else
  { for r in ${DNS_LIST//,/ }; do printf 'nameserver %s\n' "$r"; done; } \
    > "/etc/netns/$NS/resolv.conf"
fi

# ---- prove egress works -------------------------------------------------------
if [ "$DRY_RUN" = 1 ]; then
  echo "  [dry-run] sleep 3; EXIT_IP=\$(ip netns exec '$NS' curl -s --max-time 15 https://api.ipify.org)   # abort (exit 1) if empty"
  log "dry run complete: nothing was executed."
  exit 0
else
  sleep 3
  EXIT_IP=$(ip netns exec "$NS" curl -s --max-time 15 https://api.ipify.org || true)
  [ -n "$EXIT_IP" ] || { log "ERROR: no egress from namespace after build"; exit 1; }
  log "namespace '$NS' up; VPN egress IP = $EXIT_IP"
fi
