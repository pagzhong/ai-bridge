#!/bin/bash
# Uninstall bridge-sentinel on Linux.
# Usage: ./uninstall-linux.sh [purge]
#   default: stop the systemd user timer and remove the installed program
#            files. Your state (~/.bridge-sentinel) is KEPT.
#   purge:   also delete ~/.bridge-sentinel entirely.
set -eu

UNIT_DIR="$HOME/.config/systemd/user"
HOME_DIR="$HOME/.bridge-sentinel"

systemctl --user disable --now bridge-sentinel.timer >/dev/null 2>&1 || true
systemctl --user disable --now bridge-doorbell.service >/dev/null 2>&1 || true
rm -f "$UNIT_DIR/bridge-sentinel.service" "$UNIT_DIR/bridge-sentinel.timer" "$UNIT_DIR/bridge-doorbell.service"
systemctl --user daemon-reload >/dev/null 2>&1 || true
echo "removed systemd user timer bridge-sentinel.timer and doorbell service"

rm -f "$HOME_DIR/sentinel.py" "$HOME_DIR/sentinel.sh" "$HOME_DIR/doorbell.py"
rm -rf "$HOME_DIR/codex-adapter"

if [ "${1:-}" = "purge" ]; then
  rm -rf "$HOME_DIR"
  echo "purged $HOME_DIR (config, seen list, letters, logs)"
else
  echo "kept state in $HOME_DIR (config, seen list, letters, logs)"
  echo "run with 'purge' to delete it too"
fi
echo "uninstalled."
