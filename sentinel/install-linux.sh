#!/bin/bash
# Install bridge-sentinel on Linux (systemd user timer).
# Usage: ./install-linux.sh <member-name>
# Example: ./install-linux.sh claude
set -eu

MEMBER="${1:-}"
if [ -z "$MEMBER" ]; then
  echo "usage: $0 <member-name>" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$HOME/.bridge-sentinel"
mkdir -p "$HOME_DIR"

cp "$SRC_DIR/sentinel.py" "$HOME_DIR/sentinel.py"
chmod +x "$HOME_DIR/sentinel.py"
cp "$SRC_DIR/doorbell.py" "$HOME_DIR/doorbell.py"
chmod +x "$HOME_DIR/doorbell.py"
if [ -d "$SRC_DIR/codex-adapter" ]; then
  mkdir -p "$HOME_DIR/codex-adapter"
  cp "$SRC_DIR/codex-adapter/codex_adapter.py" "$SRC_DIR/codex-adapter/README.md" "$HOME_DIR/codex-adapter/"
fi

if [ ! -f "$HOME_DIR/config" ]; then
  sed "s/^MEMBER=.*/MEMBER=$MEMBER/" "$SRC_DIR/config.example" > "$HOME_DIR/config"
  echo "wrote $HOME_DIR/config (MEMBER=$MEMBER) — edit HANDOFF there before relying on delivery"
else
  echo "kept existing $HOME_DIR/config"
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cp "$SRC_DIR/systemd/bridge-sentinel.service" "$UNIT_DIR/"
cp "$SRC_DIR/systemd/bridge-sentinel.timer" "$UNIT_DIR/"
cp "$SRC_DIR/systemd/bridge-doorbell.service" "$UNIT_DIR/"

systemctl --user daemon-reload
systemctl --user enable --now bridge-sentinel.timer
echo "installed: systemd user timer bridge-sentinel.timer runs sentinel.py every 45s"
if grep -q '^NTFY_USER=.' "$HOME_DIR/config"; then
  systemctl --user enable --now bridge-doorbell.service
  echo "installed: doorbell bridge-doorbell.service (group-notice listener, ~2-5s delivery)"
else
  echo "doorbell NOT enabled: fill NTFY_USER/NTFY_PASS (your own ntfy account) in $HOME_DIR/config,"
  echo "  then run: systemctl --user enable --now bridge-doorbell.service"
fi
echo "test one cycle now: python3 $HOME_DIR/sentinel.py ; then read $HOME_DIR/sentinel.log"
