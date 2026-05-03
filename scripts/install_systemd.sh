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

# System packages used by the Login tab (Xvfb + x11vnc + websockify + noVNC)
# and the chromium that Playwright drives. apt-get is idempotent — runs cheap
# on subsequent installs.
APT_PKGS=(
  xvfb x11vnc websockify novnc
  # Chromium runtime deps (subset of `playwright install --with-deps`)
  libnss3 libatk1.0-0t64 libcups2t64 libxkbcommon0
  libxcomposite1 libxdamage1 libxrandr2 libgbm1
  libpango-1.0-0 libcairo2 libasound2t64
)
DEBIAN_FRONTEND=noninteractive apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${APT_PKGS[@]}"

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

# Playwright browser binary (chromium). Cache lives under the app dir so the
# `wishlist` system user can read it under systemd hardening.
export PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.cache/playwright"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
"$APP_DIR/.venv/bin/python" -m playwright install chromium

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
