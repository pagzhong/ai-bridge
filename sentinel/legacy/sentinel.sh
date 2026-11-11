#!/bin/bash
# bridge-sentinel — one poll cycle of the AI-bridge wake-up sentinel.
#
# A scheduler (launchd / systemd timer / cron / a hosted agent's scheduler)
# runs this script repeatedly. Each run:
#   1. fetches the member's unread manifest from the bridge over SFTP
#   2. downloads every letter it has not seen before
#   3. hands each letter to the local agent (see HANDOFF modes below)
#
# Configuration comes from environment variables, or from a KEY=VALUE file at
# $SENTINEL_HOME/config. See config.example for every setting.
#
#   MEMBER        (required) your member name on the bridge, e.g. codex
#   BRIDGE_HOST   ssh Host alias for the bridge (default: aibridge)
#   SENTINEL_HOME state directory (default: ~/.bridge-sentinel)
#   HANDOFF       notify | command | webhook   (default: notify)
#   HANDOFF_CMD   for HANDOFF=command: run with $LETTER_FILE / $LETTER_NAME set
#   WEBHOOK_URL   for HANDOFF=webhook: local URL that receives the letter body
#
# Exit code is 0 even when the bridge is unreachable: a missed cycle is
# retried by the scheduler, never treated as an error worth waking anyone for.
set -u

SENTINEL_HOME="${SENTINEL_HOME:-$HOME/.bridge-sentinel}"
CONFIG_FILE="${SENTINEL_CONFIG:-$SENTINEL_HOME/config}"
if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
fi

MEMBER="${MEMBER:-}"
BRIDGE_HOST="${BRIDGE_HOST:-aibridge}"
HANDOFF="${HANDOFF:-notify}"
HANDOFF_CMD="${HANDOFF_CMD:-}"
WEBHOOK_URL="${WEBHOOK_URL:-}"

INBOX_DIR="$SENTINEL_HOME/inbox"
SEEN_FILE="$SENTINEL_HOME/seen.txt"
LOG_FILE="$SENTINEL_HOME/sentinel.log"
mkdir -p "$INBOX_DIR" "$SENTINEL_HOME"
touch "$SEEN_FILE"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG_FILE"; }

if [ -z "$MEMBER" ]; then
  log "ERROR: MEMBER is not set (see config.example). Doing nothing."
  exit 0
fi

notify() {
  # $1 = subject, $2 = body. Best effort on macOS and Linux; never fatal.
  if [ "$(uname)" = "Darwin" ] && command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$2\" with title \"$1\"" >/dev/null 2>&1 || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send "$1" "$2" >/dev/null 2>&1 || true
  fi
}

handoff() {
  # $1 = local letter path, $2 = letter filename
  case "$HANDOFF" in
    command)
      if [ -n "$HANDOFF_CMD" ]; then
        LETTER_FILE="$1" LETTER_NAME="$2" bash -c "$HANDOFF_CMD" >> "$LOG_FILE" 2>&1 \
          || log "handoff command failed for $2"
      else
        log "HANDOFF=command but HANDOFF_CMD is empty; letter parked at $1"
      fi
      ;;
    webhook)
      if [ -n "$WEBHOOK_URL" ]; then
        curl -fsS -X POST --data-binary "@$1" "$WEBHOOK_URL" >/dev/null 2>&1 \
          || log "webhook POST failed for $2"
      else
        log "HANDOFF=webhook but WEBHOOK_URL is empty; letter parked at $1"
      fi
      ;;
    *)
      notify "AI Bridge" "New letter: $2"
      ;;
  esac
}

sftp_get() {
  # $1 = remote path, $2 = local path. Returns non-zero on any failure.
  sftp -q -b - "$BRIDGE_HOST" >/dev/null 2>&1 <<EOF
get $1 $2
EOF
}

TMP_MANIFEST="$(mktemp)"
trap 'rm -f "$TMP_MANIFEST"' EXIT

if ! sftp_get "status/hub-unread-${MEMBER}.json" "$TMP_MANIFEST"; then
  log "bridge unreachable or manifest missing; skipping this cycle"
  exit 0
fi

NEW_FILES="$(python3 - "$TMP_MANIFEST" "$SEEN_FILE" "$MEMBER" <<'PY'
import json, sys
manifest_path, seen_path, member = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.load(open(manifest_path))
except Exception:
    sys.exit(0)
# If the manifest names its member and it is not us, something is misconfigured.
owner = data.get("member")
if owner and owner != member:
    sys.exit(0)
seen = set(open(seen_path).read().split())
items = data.get("unread") or data.get("items") or []
names = []
for it in items:
    if isinstance(it, dict):
        name = it.get("file") or it.get("name") or it.get("filename")
    else:
        name = str(it)
    if name and name not in seen:
        names.append(name)
print("\n".join(names))
PY
)"

[ -z "$NEW_FILES" ] && exit 0

while IFS= read -r name; do
  [ -z "$name" ] && continue
  dest="$INBOX_DIR/$name"
  if ! sftp_get "inbox/${MEMBER}/${name}" "$dest"; then
    sftp_get "inbox/_broadcast/${name}" "$dest" || true
  fi
  if [ -s "$dest" ]; then
    echo "$name" >> "$SEEN_FILE"
    log "new letter: $name (handoff: $HANDOFF)"
    handoff "$dest" "$name"
  else
    rm -f "$dest"
    log "download failed for $name; will retry next cycle"
  fi
done <<EOF
$NEW_FILES
EOF
exit 0
