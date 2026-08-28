# NordVPN netns tunnel for Ubuntu deal verification

Task: Step 4 of the prior PBR process involved setting up a NordVPN Tunnel to access Amazon.com. It failed and the NordVPN tunnel aspect is not built yet. I am including techinical details of how a VPN tunnel can be built. Note - this repo will be deployed to an Ubuntu Linux system. Implement this.

Actual implementation (vpn_netns_up.sh, vpn_netns_down.sh, litvpn.service, litrank-harvest.service, vpn_harvest.sh) 

The repo containing these files (LitRank_2026) is in "C:\Coding\loreshorn\LitRank_2026"

The core trick

NordLynx is WireGuard, but the NordVPN CLI has no per-process split tunnelling — nordvpn connect reroutes the whole host. So the script borrows the session instead of using it:

1. Briefly nordvpn connect on the host, which makes nordvpnd negotiate a WireGuard session and create an interface called nordlynx.
2. Read the session's secrets and parameters back out of that interface with wg show.
3. nordvpn disconnect — host routing returns to normal.
4. Rebuild an equivalent WireGuard interface from those parameters inside a Linux network namespace whose only default route is that interface.

Anything you run inside the namespace egresses through Nord; everything else on the box is untouched. It works because a WireGuard peer doesn't care which interface or namespace presents the key — the session is just (private key, peer public key, endpoint, assigned address).

Step by step (the essential commands)

NS=litvpn; IFACE=litwg
nv(){ runuser -u "$NVUSER" -- nordvpn "$@"; }   # CLI as the logged-in user, not root

# 1. Keep LAN/SSH/mgmt OFF the tunnel during the brief host-wide connect
nv set killswitch disabled
nv set technology nordlynx
nv allowlist add subnet 192.168.50.0/24     # your LAN
nv allowlist add subnet 100.64.0.0/10       # Tailscale CGNAT range
nv allowlist add port 22

# 2. Negotiate, harvest the session, disconnect
nv connect
for _ in $(seq 1 20); do wg show nordlynx >/dev/null 2>&1 && break; sleep 1; done
PRIV=$(wg show nordlynx private-key)
PEER=$(wg show nordlynx dump | awk 'NR==2{print $1}')      # peer public key
ENDPOINT=$(wg show nordlynx dump | awk 'NR==2{print $3}')  # server ip:port
ADDR=$(ip -4 addr show nordlynx | awk '/inet /{print $2; exit}')  # e.g. 10.5.0.2/32
nv disconnect

# 3. Rebuild it inside a namespace
ip netns add "$NS"
ip link add "$IFACE" type wireguard
ip link set "$IFACE" netns "$NS"                # move the iface INTO the netns
KEYF=$(mktemp); chmod 600 "$KEYF"; printf '%s\n' "$PRIV" > "$KEYF"
ip netns exec "$NS" wg set "$IFACE" private-key "$KEYF" peer "$PEER" \
    endpoint "$ENDPOINT" allowed-ips 0.0.0.0/0 persistent-keepalive 25
rm -f "$KEYF"
ip -n "$NS" addr add "$ADDR" dev "$IFACE"
ip -n "$NS" link set "$IFACE" up
ip -n "$NS" link set lo up
ip -n "$NS" route add default dev "$IFACE"     # the ONLY route: no leak path exists

# 4. Per-namespace DNS (the kernel reads /etc/netns/<NS>/resolv.conf for that netns)
mkdir -p /etc/netns/$NS
printf 'nameserver 103.86.96.100\nnameserver 1.1.1.1\n' > /etc/netns/$NS/resolv.conf

# 5. Prove it
ip netns exec "$NS" curl -s --max-time 15 https://api.ipify.org

Details that matter:
- allowed-ips 0.0.0.0/0 on the peer plus route add default dev $IFACE is what makes the namespace leak-proof: there is no other route, so a process can't fall back to the host's NIC.
- persistent-keepalive 25 keeps the NAT mapping alive on the Nord side.
- The WireGuard interface is created in the host and then moved with ip link set … netns. Encrypted UDP still leaves via the host's physical NIC — the namespace holds only the tunnel side. You don't need a veth pair or NAT.
- DNS goes through the tunnel because the resolver is reached via the namespace's only route. Without /etc/netns/<NS>/resolv.conf the process inherits the host's resolv.conf, which typically points at a LAN resolver the namespace cannot reach — a classic "tunnel works, nothing resolves" failure.

Putting a process inside

Two options, both in the repo:

Ad hoc (vpn_harvest.sh): sudo ip netns exec $NS runuser -u $USER -- env VAR=… /path/python app.py. ip netns exec needs root, so drop privileges back with runuser. Needs a scoped sudoers rule for /usr/sbin/ip netns exec <NS> runuser -u <user> -- *.

systemd (litrank-harvest.service) — cleaner, no sudo at all:
[Unit]
After=litvpn.service
Requires=litvpn.service
BindsTo=litvpn.service       # if the tunnel unit stops, this stops too

[Service]
User=fksogbetun
NetworkNamespacePath=/var/run/netns/litvpn
BindReadOnlyPaths=/etc/netns/litvpn/resolv.conf:/etc/resolv.conf
NetworkNamespacePath places the service in the namespace at start; the bind mount is needed because systemd doesn't apply the /etc/netns convention itself.

The tunnel unit (litvpn.service) is Type=oneshot + RemainAfterExit=yes so the namespace persists as a dependency target, with ExecStop tearing it down.

Production gotchas (each cost an incident)

- The boot race. nordvpnd going active is not nordvpnd being ready: a consumer that ran nv connect 2 s after it died with Unable to access interface: No such device because nordlynx didn't exist yet. Fix is in the unit: Restart=on-failure, RestartSec=20, StartLimitBurst=5 — and note Restart= is honoured for Type=oneshot on systemd 255, contrary to common belief. Retry at the source, because a consumer that Requires=/BindsTo= the tunnel fails with result "dependency" and its own Restart= never applies.
- Allowlist before connecting. The host connect is brief, but without the LAN/SSH/Tailscale allowlist it will drop your SSH session and stall NFS for those seconds. Parametrize the LAN subnet per host (LITRANK_LAN_SUBNET) rather than forking the script.
- Idempotency. The script first checks whether the namespace exists and has working egress (curl through it) and adopts it if so. Rebuilding on every restart would churn the exit IP for no reason.
- The tunnel can go stale. NordLynx rotates its peer key periodically; when it does, the namespace's copy is dead and your app accrues failures without crashing. Nothing auto-detects this — the harvest's monitor watches the failure rate; you'll want a similar health check that curls through the namespace and restarts the tunnel unit.
- The egress IP is fixed for the tunnel's life and changes only on rebuild. In practice needrestart after unattended-upgrades restarts the unit a few times a month.
- Kernel-serviced I/O bypasses the namespace. NFS writes from the process still go direct, because the mount is serviced in the host namespace. Convenient here, but if your app must keep all traffic on the VPN, be aware filesystem-level network I/O is outside the sandbox.
- Run the CLI as the logged-in user (runuser -u … nordvpn), who must be in the nordvpn group with nordvpn login --token done once; wg show … private-key needs root.

For your app

Prerequisites: nordvpn CLI (logged in), wireguard-tools, a user in the nordvpn group. Everything is parametrized by NS, IFACE, LAN_SUBNET, NVUSER and the DNS — pick unique NS/IFACE names per app so two tunnels coexist (each gets its own Nord session and exit IP). Copy vpn_netns_up.sh / vpn_netns_down.sh / litvpn.service, rename, and give your service the three NetworkNamespacePath/BindReadOnlyPaths/BindsTo lines.

- [ ] Step 1: Add the two tunnel scripts, ported from the LitRank_2026 reference (vpn_netns_up.sh / vpn_netns_down.sh) and adapted for this repo. They build a Linux network namespace whose ONLY route is a NordLynx (WireGuard) tunnel, so only processes placed in it egress through NordVPN; the host (SSH/LAN/mgmt) is untouched. Every knob comes from the environment with defaults (NS=wlvpn, IFACE=wlwg via WISHLIST_VPN_NS/WISHLIST_VPN_IFACE; the logged-in Nord user via WISHLIST_VPN_USER; allowlist LAN via WISHLIST_VPN_LAN_SUBNET; resolver(s) via WISHLIST_VPN_DNS) so no forking per host. vpn_netns_up.sh is idempotent (adopts/reuses a live namespace; rebuilds a stale one), then: drives the nordvpn CLI as WISHLIST_VPN_USER via runuser (killswitch disabled, technology nordlynx, allowlist LAN + 100.64.0.0/10 + port 22), briefly `nordvpn connect`, harvests `wg show nordlynx` private-key/peer-public/endpoint + the assigned addr, `nordvpn disconnect`, rebuilds an equivalent wireguard iface moved into the namespace with allowed-ips 0.0.0.0/0 + persistent-keepalive 25, sets per-namespace DNS (default 103.86.96.100,1.1.1.1) at /etc/netns/<NS>/resolv.conf, and verifies egress via `ip netns exec <NS> curl https://api.ipify.org`. Both scripts support an explicit --dry-run mode that PRINTS the exact command sequence and exits 0 without executing anything (so the logic is reviewable on the non-Ubuntu build box). vpn_netns_down.sh tears the namespace/iface/resolv.conf down. Recorded assumption: the nordvpn CLI + wireguard-tools exist on the target Ubuntu host and the account is logged in once via `nordvpn login --token` (used only as the operator's user, never committed).
  - files: scripts/vpn_netns_up.sh, scripts/vpn_netns_down.sh
  - verification: bash -n scripts/vpn_netns_up.sh scripts/vpn_netns_down.sh (both exit 0, no syntax errors) AND WISHLIST_VPN_DRY_RUN=1 bash scripts/vpn_netns_up.sh (prints the full command plan: netns/iface names, runuser allowlist+connect, wg harvest, reconnect flags, per-netns DNS, egress check; exits 0 and creates no namespace) AND WISHLIST_VPN_DRY_RUN=1 bash scripts/vpn_netns_down.sh likewise prints and exits 0.
- [ ] Step 2: Add the tunnel systemd unit amazon-wishlist-vpn.service at the repo root (next to the existing amazon-wishlist.service) and wire it into scripts/install_systemd.sh. Unit: Type=oneshot + RemainAfterExit=yes so the namespace persists as a dependency target; Requires=After=nordvpnd.service + network-online.target; boot-race resilient restart (StartLimitIntervalSec=600 StartLimitBurst=5, Restart=on-failure RestartSec=20, TimeoutStartSec=180 — retries at the source because Requires/BindsTo consumers fail with result 'dependency' and their own Restart never applies); ExecStart=scripts/vpn_netns_up.sh and ExecStop=scripts/vpn_netns_down.sh (root, /bin/bash); EnvironmentFile=-/etc/default/amazon-wishlist so WISHLIST_VPN_* (user, LAN subnet, NS) are per-host; WantedBy=multi-user.target. install_systemd.sh: apt install wireguard-tools (and document the nordvpn CLI package + one-time `nordvpn login --token <USER>` as prerequisites — it is a .deb from Nord, not a distro package, so it installs in a best-effort/tolerated block), install -m 644 the new unit to /etc/systemd/system/, systemctl daemon-reload, enable amazon-wishlist-vpn.service (start it too so every deploy brings the tunnel up). Recorded assumption: NordVPN CLI login/group is provisioned out-of-band by the operator; install only installs/enables, never stores credentials.
  - files: amazon-wishlist-vpn.service, scripts/install_systemd.sh
  - verification: bash -n scripts/install_systemd.sh (exit 0) AND systemd-analyze verify amazon-wishlist-vpn.service (if systemd-analyze exists; on the Windows box this is skipped) AND grep confirms the unit declares Type=oneshot, RemainAfterExit=yes, Requires=nordvpnd.service, ExecStart=/bin/bash scripts/vpn_netns_up.sh, ExecStop=/bin/bash scripts/vpn_netns_down.sh, and EnvironmentFile=-/etc/default/amazon-wishlist.
- [ ] Step 3: Add the consumer that runs scripts/verify_deals.py INSIDE the namespace, in two forms matching the reference (systemd unit primary, ad-hoc wrapper for interactive use). amazon-wishlist-verify.service: User=wishlist, WorkingDirectory=/opt/amazon-wishlist, NetworkNamespacePath=/var/run/netns/wlvpn (places the service in the tunnel at start), BindReadOnlyPaths=/etc/netns/wlvpn/resolv.conf:/etc/resolv.conf (systemd does not apply the /etc/netns convention itself), Requires=+BindsTo=amazon-wishlist-vpn.service so the verifier stops if the tunnel stops, EnvironmentFile=-/etc/default/amazon-wishlist (DEALS_DB, VERIFY_*, WISHLIST_VPN_*), ExecStart=/opt/amazon-wishlist/.venv/bin/python scripts/verify_deals.py --netns wlvpn, Restart=on-failure. scripts/vpn_verify.sh: ad-hoc launcher that runs `sudo ip netns exec wlvpn runuser -u <op> -- env ... python scripts/verify_deals.py --netns wlvpn "$@"` (drops root back to the invoking user; documented scoped-sudoers rule for /usr/sbin/ip netns exec). Recorded assumption: the fixed tunnel IP is accepted for a normal run (per the reference); per-book fingerprint rotation remains the fast anti-bot signal. Neither file stores credentials — the tunnel is already negotiated by the vpn unit as the operator's user.
  - files: amazon-wishlist-verify.service, scripts/vpn_verify.sh
  - verification: bash -n scripts/vpn_verify.sh (exit 0) AND grep confirms amazon-wishlist-verify.service declares NetworkNamespacePath=/var/run/netns/wlvpn, BindReadOnlyPaths=/etc/netns/wlvpn/resolv.conf:/etc/resolv.conf, Requires=amazon-wishlist-vpn.service, BindsTo=amazon-wishlist-vpn.service, and ExecStart runs scripts/verify_deals.py with --netns wlvpn, and that it contains no NORDVPN_USERNAME/NORDVPN_PASSWORD line.
- [ ] Step 4: Add netns-aware helpers (a 'tunnel mode') to app/nordvpn.py WITHOUT touching the existing host-CLI wrapper, plus their config knobs in app/config.py. New config: WISHLIST_VPN_NS (default 'wlvpn'), WISHLIST_VPN_IFACE (default 'wlwg'), WISHLIST_VPN_UNIT (default 'amazon-wishlist-vpn.service'), WISHLIST_VPN_ENDPOINT (default 'https://api.ipify.org'). New pure/mockable functions in app/nordvpn.py: netns_exists(ns=...) (checks /var/run/netns/ or `ip netns list`), tunnel_egress_ip(timeout) (blocking curl to the endpoint through the host, returns the egress IPv4 or None), netns_egress_ok(ns=...) (curl through `ip netns exec ns curl` succeeds — the tunnel is live), rebuild_tunnel(unit=...) (best-effort `systemctl restart <unit>`; returns bool), and tunnel_rotate(ns=..., unit=...) = rebuild_tunnel() then tunnel_egress_ip() returning the fresh IP — the netns analogue of the host CLI rotate(). All treat missing tools (no ip/curl/systemctl) as a clean failure (return False/None) that the caller logs rather than throws. No credential handling here — the tunnel is pre-negotiated. Recorded assumption: within a namespace the egress IP is fixed until a rebuild, so tunnel_rotate() refreshes the IP by rebuilding the tunnel rather than by a per-N host nordvpn.rotate() (kept for dev boxes without the netns).
  - files: app/config.py, app/nordvpn.py
  - verification: python -m py_compile app/nordvpn.py app/config.py (exit 0) AND a python -c import check that app.nordvpn exposes netns_exists/tunnel_egress_ip/netns_egress_ok/rebuild_tunnel/tunnel_rotate and that app.config has WISHLIST_VPN_NS/IFACE/UNIT/ENDPOINT with the documented defaults. (Live netns call is Ubuntu-only; verified there by the operator.)
- [ ] Step 5: Reconcile scripts/verify_deals.py so it can run in tunnel mode on Ubuntu: add `--netns NS` (default empty -> existing host-CLI behavior preserved for dev). When --netns is set: (1) 'ensure VPN up' becomes nordvpn.netns_egress_ok(ns) and, if the namespace is missing or has no egress, nordvpn.rebuild_tunnel() then re-check — clear one-line error + exit 1 if it still can't egress (environment prerequisite, matching the CLI path's handling); (2) the every-`--rotate-every` hop becomes nordvpn.tunnel_rotate(ns) (rebuild -> fresh exit IP) + fresh_fingerprint() differing in all three fields, logged and non-fatal if a rebuild is not permitted (script continues with fingerprint-only rotation); (3) `--check` and the per-book read/mark_verified/resume scope logic are unchanged. Keep the host-CLI ensure/login/rotate() path untouched when --netns is empty. All changes are additive and guarded so the CLI path's existing tests/harness still pass. Recorded assumption (carried from the reference): per-N exit-IP rotation in tunnel mode is satisfied by a best-effort tunnel rebuild to a fresh IP, with a stable IP accepted when no rebuild is possible; the real 375-book pass runs against the real data/deals.db on Ubuntu via amazon-wishlist-verify.service / scripts/vpn_verify.sh, while step verification uses a bounded TEMP copy.
  - files: scripts/verify_deals.py
  - verification: python -m py_compile scripts/verify_deals.py (exit 0) AND `python scripts/verify_deals.py --check` still prints 375 pending (no args change means no behavior change for the CLI path) AND the existing bounded harness on a TEMP copy of data/deals.db (VPN/netns layer stubbed via monkeypatch of nordvpn.netns_egress_ok/tunnel_rotate to return True/'203.0.113.11') runs `--netns wlvpn --limit 2 --rotate-every 2` to completion: exactly 2 rows get deal_status in {current,expired,unknown} with verified_at set and current_price populated (NULL only when unknown), 1 tunnel_rotate + fresh fingerprint occurs after 2 books, the re-run is a fast no-op, and the real data/deals.db is untouched (381 rows, 0 verified).
- [ ] Step 6: Document the Ubuntu NordVPN-tunnel deployment in README.md. Extend the existing 'Verifying deals are still live' + Configuration sections: a new subsection describing the netns/nordlynx (WireGuard) tunnel — amazon-wishlist-vpn.service (oneshot+RemainAfterExit, enabled at boot by install_systemd.sh; Requires/After nordvpnd; boot-race Restart resilience), the tunnel scripts (scripts/vpn_netns_up.sh / vpn_netns_down.sh), prerequisites (nordvpn CLI installed + `nordvpn login --token` once as the operator user, wireguard-tools, operator in the nordvpn group), that ONLY the verifier's traffic egresses via Nord (host SSH/NFS/mgmt unaffected), the LAN/SSH/Tailscale allowlist warning (WISHLIST_VPN_LAN_SUBNET, 100.64.0.0/10, port 22) for the brief host connect, per-host settings via /etc/default/amazon-wishlist (WISHLIST_VPN_USER/NS/IFACE/LAN_SUBNET/DNS), how to run the verifier in the tunnel (amazon-wishlist-verify.service and scripts/vpn_verify.sh, both invoking verify_deals.py --netns wlvpn), the steady-state behaviour (tunnel egress IP is fixed for its life, changes only on rebuild; per-N rotate = best-effort rebuild + fresh fingerprint), and the credential model (account logged in once by the operator, never in the repo). Add the WISHLIST_VPN_* rows to the config env-var table.
  - files: README.md
  - verification: grep -n -i -E 'vpn_netns_up|vpn_netns_down|amazon-wishlist-vpn|amazon-wishlist-verify|--netns|wlvpn|allowlist|wireguard|WISHLIST_VPN' README.md — success is lines in the BookBub/deals/verify area covering the tunnel unit, the --netns invocation, the allowlist warning, and the WISHLIST_VPN_* env config.
