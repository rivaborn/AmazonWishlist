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

# The nordvpn CLI ships as a .deb from Nord, not a distro package, and its
# versioned filename changes over time, so this is best-effort: a stale URL
# must never abort a deploy (the service restart below is what ships the code).
# After (re)install, the operator must log in ONCE as WISHLIST_VPN_USER:
#   nordvpn login --token <TOKEN>
# (the token comes from the NordVPN account; the credentials are never stored
# in this repo — the tunnel only reads the session back out of the nordlynx
# interface). Set NORDVPN_DEB_URL to override the pinned .deb.
NORDVPN_DEB_URL="${NORDVPN_DEB_URL:-https://repo.nordvpn.com/deb/nordvpn/stable/lucid/pool/main/n/nordvpn/nordvpn_6.4-1_all.deb}"
if command -v nordvpn >/dev/null 2>&1; then
  echo "nordvpn CLI already present: $(command -v nordvpn)"
elif ! (curl -fsSL "$NORDVPN_DEB_URL" -o /tmp/nordvpn.deb && dpkg -i /tmp/nordvpn.deb); then
  echo "WARNING: could not install the nordvpn CLI from $NORDVPN_DEB_URL" >&2
  echo "         (stale versioned URL or network block). Install it manually from" >&2
  echo "         https://repo.nordvpn.com/deb/nordvpn/, run 'nordvpn login --token <TOKEN>'" >&2
  echo "         as WISHLIST_VPN_USER, then 'systemctl restart amazon-wishlist-vpn.service'." >&2
fi
rm -f /tmp/nordvpn.deb

install -m 644 "$APP_DIR/amazon-wishlist.service" /etc/systemd/system/amazon-wishlist.service
# The wlvpn tunnel unit: persists the namespace at boot so the live-deal
# verifier can run inside it. Its start is best-effort — until the operator has
# run `nordvpn login --token` it cannot come up, and that must never block a
# deploy (the up script also exits 1 with a clear message when its
# prerequisites are missing; StartLimitBurst bounds the boot-race retries).
install -m 644 "$APP_DIR/amazon-wishlist-vpn.service" /etc/systemd/system/amazon-wishlist-vpn.service
# BookBub daily updater (scripts/bookbub_daily.py): a oneshot service that
# runs inside the wlvpn netns, fired daily at 18:00 local by its timer. The
# TIMER is the unit that gets enabled; the service's start below is
# best-effort — a missing BOOKBUB_LOGIN_LINK makes the run exit 2, and that
# must never block a deploy.
install -m 644 "$APP_DIR/amazon-wishlist-bookbub.service" /etc/systemd/system/amazon-wishlist-bookbub.service
install -m 644 "$APP_DIR/amazon-wishlist-bookbub.timer" /etc/systemd/system/amazon-wishlist-bookbub.timer
systemctl daemon-reload
systemctl enable amazon-wishlist.service
systemctl enable amazon-wishlist-vpn.service
systemctl enable amazon-wishlist-bookbub.timer
systemctl restart amazon-wishlist.service
systemctl start amazon-wishlist-vpn.service || true
systemctl start amazon-wishlist-bookbub.timer || true
systemctl start amazon-wishlist-bookbub.service || true
systemctl status --no-pager amazon-wishlist.service || true
systemctl status --no-pager amazon-wishlist-vpn.service || true
systemctl status --no-pager amazon-wishlist-bookbub.service || true
systemctl list-timers --all --no-pager | grep -F bookbub || true

echo
echo "Installed. Visit http://<host>:9060/"
