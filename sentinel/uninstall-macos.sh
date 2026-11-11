#!/bin/bash
# Uninstall bridge-sentinel on macOS.
# Usage: ./uninstall-macos.sh [purge]
#   default: stop the launchd agent and remove the installed program files.
#            Your state (~/.bridge-sentinel: config, seen list, downloaded
#            letters, logs) is KEPT so a reinstall resumes where you left off.
#   purge:   also delete ~/.bridge-sentinel entirely.
set -eu

PLIST="$HOME/Library/LaunchAgents/ai.bridge.sentinel.plist"
DB_PLIST="$HOME/Library/LaunchAgents/ai.bridge.doorbell.plist"
HOME_DIR="$HOME/.bridge-sentinel"

if [ -f "$PLIST" ]; then
  launchctl unload "$PLIST" >/dev/null 2>&1 || true
  rm -f "$PLIST"
  echo "removed launchd agent ai.bridge.sentinel"
fi
if [ -f "$DB_PLIST" ]; then
  launchctl unload "$DB_PLIST" >/dev/null 2>&1 || true
  rm -f "$DB_PLIST"
  echo "removed launchd agent ai.bridge.doorbell"
fi

# Remove program files installed by install-macos.sh (keep state files).
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
