#!/usr/bin/env bash
# Install Amazon Wishlist Tracker as a systemd service on Ubuntu.
# Run as root from the repo root: sudo bash scripts/install_systemd.sh
set -euo pipefail

APP_USER="wishlist"
APP_DIR="/opt/amazon-wishlist"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root" >&2
  exit 1
fi

if ! id -u "$APP_USER" &>/dev/null; then
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

# Login-tab infrastructure: virtual X display, VNC, and the noVNC web client.
# (Chromium's own runtime deps are installed by `playwright install --with-deps`
#  below — that picks the right package set per Ubuntu version.)
# Plus the wlvpn tunnel's hard deps: wireguard-tools (the `wg` tool) and curl
# (the namespace egress check in scripts/vpn_netns_up.sh).
DEBIAN_FRONTEND=noninteractive apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  xvfb x11vnc websockify novnc curl wireguard-tools

mkdir -p "$APP_DIR"
# NOTE: .cache MUST be excluded — it holds the Playwright chromium browser, which
# lives only in $APP_DIR (never in the repo). Without this exclude, `--delete`
# wipes the browser on every deploy, and on Ubuntu versions newer than the
# installed Playwright supports the reinstall below can't put it back.
rsync -a --delete \
  --exclude=".git" --exclude=".venv" --exclude="data" --exclude=".cache" --exclude="__pycache__" \
  --exclude=".env" \
  "$REPO_DIR"/ "$APP_DIR"/
mkdir -p "$APP_DIR/data"

if [[ ! -d "$APP_DIR/.venv" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
# On some Ubuntu builds, `python3 -m venv` silently skips the pip bootstrap.
# Force ensurepip if pip didn't land.
if [[ ! -x "$APP_DIR/.venv/bin/pip" ]]; then
  "$APP_DIR/.venv/bin/python" -m ensurepip --upgrade --default-pip
fi
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Playwright browser binary (chromium). Cache lives under the app dir so the
# `wishlist` system user can read it under systemd hardening.
#
# On Ubuntu releases newer than the installed Playwright officially lists, the
# install fails an OS-version gate even though the Linux x64 binary is fine. We
# set PLAYWRIGHT_HOST_PLATFORM_OVERRIDE so it treats the host as a supported
# release, try `--with-deps` first (installs the apt runtime libs on a fresh
# box), then fall back to a binary-only install, and finally tolerate a failure
# entirely — the browser is usually already cached, and a missing browser only
# degrades the scraper to the anonymous httpx path rather than breaking startup.
# Crucially, this step must never abort the deploy: the service restart below
# is what ships the new code.
export PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.cache/playwright"
export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE="${PLAYWRIGHT_HOST_PLATFORM_OVERRIDE:-ubuntu24.04-x64}"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
PW="$APP_DIR/.venv/bin/python -m playwright install"
if ! $PW --with-deps chromium; then
  echo "playwright '--with-deps chromium' failed (OS-version gate?); retrying binary-only" >&2
  if ! $PW chromium; then
    echo "WARNING: could not install the chromium browser; the scraper will fall " >&2
    echo "         back to the anonymous httpx path until it is installed manually." >&2
  fi
fi

# Ensure the directories the service writes to exist with the right owner.
mkdir -p "$APP_DIR/data/.chrome-login" "$APP_DIR/data/diagnostics"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ---- wlvpn NordVPN tunnel prerequisites (optional for the app itself) -------
# WireGuard kernel module: built into stock Ubuntu 22.04+ kernels, but load it
# on boxes where it ships as a module. Tolerated: a missing module only breaks
# the tunnel, never the app.
modprobe wireguard 2>/dev/null || \
  echo "note: 'modprobe wireguard' failed — if the module is missing, the wlvpn tunnel cannot come up" >&2

# The nordvpn CLI ships from Nord's own apt repo, not from Ubuntu. Install the
# `nordvpn-release` package (it drops in the repo + signing key) and then apt
# the client, rather than pinning a versioned .deb: the previously pinned URL
# (nordvpn_6.4-1_all.deb) started 404ing once Nord reorganised the pool, and
# apt also gets the box future client updates for free. Best-effort throughout
# — a failure here must never abort a deploy (the service restart below is what
# ships the code), it only leaves the tunnel unavailable.
# After (re)install, the operator must log in ONCE as WISHLIST_VPN_USER:
#   nordvpn login --token <TOKEN>
# The token comes from the Nord Account dashboard. Current NordVPN clients have
# NO username/password login — only browser SSO, --callback, or --token — so a
# token is the only automatable option. Credentials are never stored in this
# repo; the tunnel only reads the session back out of the nordlynx interface.
# Set NORDVPN_RELEASE_DEB_URL to override the repo package.
NORDVPN_RELEASE_DEB_URL="${NORDVPN_RELEASE_DEB_URL:-https://repo.nordvpn.com/deb/nordvpn/debian/pool/main/n/nordvpn-release/nordvpn-release_1.0.0_all.deb}"
if command -v nordvpn >/dev/null 2>&1; then
  echo "nordvpn CLI already present: $(command -v nordvpn)"
elif ! (curl -fsSL "$NORDVPN_RELEASE_DEB_URL" -o /tmp/nordvpn-release.deb         && dpkg -i /tmp/nordvpn-release.deb         && apt-get update -qq         && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nordvpn); then
  echo "WARNING: could not install the nordvpn CLI via $NORDVPN_RELEASE_DEB_URL" >&2
  echo "         (network block, or Nord moved the repo package). Install it by hand" >&2
  echo "         from https://nordvpn.com/download/linux/, run 'nordvpn login --token <TOKEN>'" >&2
  echo "         as WISHLIST_VPN_USER, then 'systemctl restart amazon-wishlist-vpn.service'." >&2
fi
rm -f /tmp/nordvpn-release.deb

# The client's postinst adds the *installing* box's login user to the nordvpn
# group, which is not necessarily WISHLIST_VPN_USER — and vpn_netns_up.sh drives
# the CLI as that user (`runuser -u "$NVUSER" -- nordvpn ...`), so ensure it is
# a member. Tolerated: no group means no tunnel, but the deploy still succeeds.
if [ -n "${WISHLIST_VPN_USER:-}" ] && getent group nordvpn >/dev/null 2>&1; then
  usermod -aG nordvpn "$WISHLIST_VPN_USER" 2>/dev/null     || echo "note: could not add '$WISHLIST_VPN_USER' to the nordvpn group" >&2
fi

install -m 644 "$APP_DIR/amazon-wishlist.service" /etc/systemd/system/amazon-wishlist.service
# The wlvpn tunnel unit: installed but NOT a boot-time unit (no [Install]
# section) — it comes up on demand via the bookbub unit's Requires= and is
# torn down by that unit's ExecStopPost, so the VPN is only connected while a
# run is in use. It still needs `nordvpn login --token` done once by
# WISHLIST_VPN_USER (see above); until then it cannot come up, and a bookbub
# run then fails fast (Requires=) — which must never block a deploy.
install -m 644 "$APP_DIR/amazon-wishlist-vpn.service" /etc/systemd/system/amazon-wishlist-vpn.service
# BookBub daily updater (scripts/bookbub_daily.py): a oneshot that runs the
# FETCH on the HOST (headful Chromium can't launch inside the netns) and then
# triggers amazon-wishlist-verify.service for the in-netns re-check. It is NO
# LONGER timer-driven: the APP schedules the run (app/scheduler.py, at the
# BookBub time set in the Settings tab) by starting this unit via the scoped
# sudoers rule provisioned below — so this deploy must NOT start it (a run
# blocks for the whole cycle, ~an hour).
install -m 644 "$APP_DIR/amazon-wishlist-bookbub.service" /etc/systemd/system/amazon-wishlist-bookbub.service
# The netns verifier: the daily re-check runs headless inside the wlvpn
# namespace via this unit (started by bookbub_daily at the end of its run,
# `--recheck`); it also serves ad-hoc full rechecks. No [Install] section, so
# it is never enabled at boot — only the daily trigger / manual start runs it.
install -m 644 "$APP_DIR/amazon-wishlist-verify.service" /etc/systemd/system/amazon-wishlist-verify.service
# Retire the timer if a previous install left it (the app now schedules the run).
systemctl disable --now amazon-wishlist-bookbub.timer 2>/dev/null || true
systemctl remove amazon-wishlist-bookbub.timer 2>/dev/null || true
systemctl daemon-reload
systemctl enable amazon-wishlist.service
# The wlvpn tunnel is no longer a boot-time unit: disable it if a previous
# install enabled it, and stop it if it is up right now (VPN down unless a run
# is in use).
systemctl disable --now amazon-wishlist-vpn.service 2>/dev/null || true
systemctl stop amazon-wishlist-vpn.service 2>/dev/null || true
# Scoped sudoers rule (NOT blanket sudo — same pattern as the vpn_verify.sh
# scoped rule): lets the app (the wishlist user) start the bookbub unit on
# schedule, lets bookbub_daily start the netns verify unit for the re-check,
# and lets those units' ExecStopPost tear the VPN down. visudo -cf validates
# the rule; a malformed one aborts the deploy (set -e).
cat > /etc/sudoers.d/amazon-wishlist <<'SUDOERS'
wishlist ALL=(root) NOPASSWD: /usr/bin/systemctl start amazon-wishlist-bookbub.service, /usr/bin/systemctl stop amazon-wishlist-vpn.service, /usr/bin/systemctl start amazon-wishlist-verify.service
SUDOERS
chmod 0440 /etc/sudoers.d/amazon-wishlist
visudo -cf /etc/sudoers.d/amazon-wishlist
systemctl restart amazon-wishlist.service
systemctl status --no-pager amazon-wishlist.service || true
# "inactive (dead)" for the tunnel unit here is the healthy state: it is only
# active while the daily BookBub run is in progress.
systemctl status --no-pager amazon-wishlist-vpn.service || true

echo
echo "Installed. Visit http://<host>:9060/"
