#!/bin/bash
# Install bridge-sentinel on macOS (launchd user agent).
# Usage: ./install-macos.sh <member-name> [poll-seconds]
# Example: ./install-macos.sh codex 45
set -eu

MEMBER="${1:-}"
INTERVAL="${2:-45}"
if [ -z "$MEMBER" ]; then
  echo "usage: $0 <member-name> [poll-seconds]" >&2
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
PYTHON_BIN="$(command -v python3 || echo /usr/bin/python3)"

if [ ! -f "$HOME_DIR/config" ]; then
  sed "s/^MEMBER=.*/MEMBER=$MEMBER/" "$SRC_DIR/config.example" > "$HOME_DIR/config"
  echo "wrote $HOME_DIR/config (MEMBER=$MEMBER) — edit HANDOFF there before relying on delivery"
else
  echo "kept existing $HOME_DIR/config"
fi

PLIST="$HOME/Library/LaunchAgents/ai.bridge.sentinel.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__HOME__|$HOME|g" -e "s|__INTERVAL__|$INTERVAL|g" -e "s|__PYTHON__|$PYTHON_BIN|g" \
  "$SRC_DIR/launchd/ai.bridge.sentinel.plist" > "$PLIST"

launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST"
echo "installed: launchd agent ai.bridge.sentinel runs sentinel.py every ${INTERVAL}s"

DB_PLIST="$HOME/Library/LaunchAgents/ai.bridge.doorbell.plist"
sed -e "s|__HOME__|$HOME|g" -e "s|__PYTHON__|$PYTHON_BIN|g" \
  "$SRC_DIR/launchd/ai.bridge.doorbell.plist" > "$DB_PLIST"
if grep -q '^NTFY_USER=.' "$HOME_DIR/config"; then
  launchctl unload "$DB_PLIST" >/dev/null 2>&1 || true
  launchctl load "$DB_PLIST"
  echo "installed: doorbell ai.bridge.doorbell (group-notice listener, ~2-5s delivery; log: $HOME_DIR/doorbell.log)"
else
  launchctl unload "$DB_PLIST" >/dev/null 2>&1 || true
  echo "doorbell plist written but NOT loaded: fill NTFY_USER/NTFY_PASS (your own ntfy account) in"
  echo "  $HOME_DIR/config, then run: launchctl load $DB_PLIST"
fi
echo "test one cycle now: python3 $HOME_DIR/sentinel.py ; then read $HOME_DIR/sentinel.log"
