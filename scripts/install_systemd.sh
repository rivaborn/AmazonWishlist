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
DEBIAN_FRONTEND=noninteractive apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  xvfb x11vnc websockify novnc

mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude=".git" --exclude=".venv" --exclude="data" --exclude="__pycache__" \
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

# Playwright browser binary (chromium) + its system runtime deps. Cache lives
# under the app dir so the `wishlist` system user can read it under systemd
# hardening. `--with-deps` picks the right apt packages for this Ubuntu rev
# (libatk-bridge, libnss3, libxkbcommon, etc.) so we don't have to maintain a
# hand-rolled list that drifts.
export PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.cache/playwright"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
"$APP_DIR/.venv/bin/python" -m playwright install --with-deps chromium

# Ensure the directories the service writes to exist with the right owner.
mkdir -p "$APP_DIR/data/.chrome-login" "$APP_DIR/data/diagnostics"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

install -m 644 "$APP_DIR/amazon-wishlist.service" /etc/systemd/system/amazon-wishlist.service
systemctl daemon-reload
systemctl enable amazon-wishlist.service
systemctl restart amazon-wishlist.service
systemctl status --no-pager amazon-wishlist.service || true

echo
echo "Installed. Visit http://<host>:9060/"
