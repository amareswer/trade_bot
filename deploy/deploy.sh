#!/usr/bin/env bash
# deploy.sh — one-shot VPS setup script for the crypto trading bot
#
# Usage:
#   1. SSH into your VPS as root (or a sudo user)
#   2. Copy this repo to the VPS (rsync / git clone)
#   3. Run:  sudo bash deploy/deploy.sh
#
# After first run, manage with:
#   sudo systemctl start   trade_bot
#   sudo systemctl stop    trade_bot
#   sudo systemctl restart trade_bot
#   sudo systemctl status  trade_bot
#   sudo journalctl -u trade_bot -f   # live logs

set -euo pipefail

INSTALL_DIR="/opt/trade_bot"
SERVICE_NAME="trade_bot"
PYTHON_BIN="python3"
VENV_DIR="$INSTALL_DIR/venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=== Trade Bot VPS Deploy ==="
echo "Install dir : $INSTALL_DIR"
echo "Repo source : $REPO_ROOT"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/6] Updating system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl

# ── 2. Copy repo to install dir ───────────────────────────────────────────────
echo "[2/6] Copying repo to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
      --exclude='venv' --exclude='logs' \
      "$REPO_ROOT/" "$INSTALL_DIR/"

# Keep logs directory writable
mkdir -p "$INSTALL_DIR/logs"
chown -R ubuntu:ubuntu "$INSTALL_DIR" 2>/dev/null || true

# ── 3. Python virtualenv ──────────────────────────────────────────────────────
echo "[3/6] Creating Python venv and installing dependencies..."
if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# ── 4. .env file ─────────────────────────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo ""
    echo "[4/6] WARNING: No .env found at $INSTALL_DIR/.env"
    echo "      Copy your .env file there before starting the bot:"
    echo "        scp .env user@your-vps:$INSTALL_DIR/.env"
    echo ""
else
    echo "[4/6] .env found — OK"
    chmod 600 "$INSTALL_DIR/.env"
fi

# ── 5. systemd service ────────────────────────────────────────────────────────
echo "[5/6] Installing systemd service..."
cp "$SCRIPT_DIR/trade_bot.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ── 6. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "[6/6] Deploy complete."
echo ""
echo "Next steps:"
echo "  1. Ensure $INSTALL_DIR/.env is present and correct"
echo "  2. sudo systemctl start $SERVICE_NAME"
echo "  3. sudo journalctl -u $SERVICE_NAME -f   (watch live logs)"
echo ""
echo "Useful commands:"
echo "  sudo systemctl restart $SERVICE_NAME   — restart after code update"
echo "  sudo systemctl stop    $SERVICE_NAME   — stop the bot"
echo "  sudo systemctl status  $SERVICE_NAME   — check health"
