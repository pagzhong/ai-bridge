#!/bin/bash
# AI Bridge — server (hub) installer.
# Run as root on a fresh Debian/Ubuntu VPS:
#   sudo BRIDGE_DOMAIN=bridge.example.com bash deploy/setup-server.sh
# Idempotent: safe to re-run (also how you upgrade — it refreshes the
# installed code and units, never touches bridge.conf or member data).
#
# What it does:
#   1. creates the SFTP-only system user `bridge` (chrooted to /srv/bridge)
#   2. creates the mailbox directory tree with chroot-correct ownership
#   3. installs an sshd snippet confining `bridge` to internal-sftp
#   4. installs hub + member-admin + renderer to /usr/local/bin and
#      their systemd units, initialises the member registry
#   5. seeds /srv/bridge/bridge.conf from the template (first run only)
# It does NOT install ntfy / nginx / TLS certificates — see deploy/README.md
# for those steps (they depend on your DNS and certificate setup).
#
# Uninstall:  sudo bash deploy/setup-server.sh --uninstall
#             (add PURGE=1 to also delete /srv/bridge and /srv/bridge-admin)
set -eu

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE_ROOT="${BRIDGE_ROOT:-/srv/bridge}"
ADMIN_DIR="${ADMIN_DIR:-/srv/bridge-admin}"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now bridge-hub.service bridge-admin.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/bridge-hub.service /etc/systemd/system/bridge-admin.service
  rm -f /usr/local/bin/bridge-hub.py /usr/local/bin/bridge-admin.py \
        /usr/local/bin/bridge-render.py /usr/local/bin/bridge-render.sh
  rm -f /etc/ssh/sshd_config.d/60-ai-bridge.conf
  systemctl daemon-reload >/dev/null 2>&1 || true
  (sshd -t && systemctl reload ssh) >/dev/null 2>&1 || true
  echo "services and sshd snippet removed."
  if [ "${PURGE:-}" = "1" ]; then
    rm -rf "$BRIDGE_ROOT" "$ADMIN_DIR"
    userdel bridge >/dev/null 2>&1 || true
    echo "purged $BRIDGE_ROOT, $ADMIN_DIR and the bridge user."
  else
    echo "kept data in $BRIDGE_ROOT and $ADMIN_DIR (PURGE=1 deletes them too)."
  fi
  exit 0
fi

[ "$(id -u)" = "0" ] || { echo "run as root (sudo)" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

# 1) the bridge user (no shell, home = bridge root)
if ! id bridge >/dev/null 2>&1; then
  useradd --system --home-dir "$BRIDGE_ROOT" --shell /usr/sbin/nologin bridge
  echo "created system user: bridge"
fi

# 2) directory tree — chroot dir itself must be root-owned; the writable
#    subdirectories belong to bridge.
mkdir -p "$BRIDGE_ROOT"
chown root:root "$BRIDGE_ROOT"; chmod 755 "$BRIDGE_ROOT"
for d in inbox shared archive tmp status log; do
  mkdir -p "$BRIDGE_ROOT/$d"; chown bridge:bridge "$BRIDGE_ROOT/$d"
done
mkdir -p "$BRIDGE_ROOT/inbox/_broadcast"; chown bridge:bridge "$BRIDGE_ROOT/inbox/_broadcast"
mkdir -p "$BRIDGE_ROOT/.ssh"; chown bridge:bridge "$BRIDGE_ROOT/.ssh"; chmod 700 "$BRIDGE_ROOT/.ssh"
touch "$BRIDGE_ROOT/.ssh/authorized_keys"
chown bridge:bridge "$BRIDGE_ROOT/.ssh/authorized_keys"; chmod 600 "$BRIDGE_ROOT/.ssh/authorized_keys"
# daily purge of the delivery staging area
printf '#!/bin/sh\nfind %s/tmp -type f -mmin +1440 -delete 2>/dev/null || true\n' "$BRIDGE_ROOT" > /etc/cron.daily/ai-bridge-tmp-purge
chmod +x /etc/cron.daily/ai-bridge-tmp-purge

# 3) sshd confinement for the bridge account
cat > /etc/ssh/sshd_config.d/60-ai-bridge.conf <<'SSHD'
Match User bridge
    ChrootDirectory /srv/bridge
    ForceCommand internal-sftp
    PasswordAuthentication no
    AllowTcpForwarding no
    X11Forwarding no
SSHD
sshd -t && (systemctl reload ssh || systemctl reload sshd)

# 4) code + units + registry
install -m 0755 "$SRC_DIR/hub/bridge-hub.py" /usr/local/bin/bridge-hub.py
install -m 0755 "$SRC_DIR/hub/bridge-render.py" /usr/local/bin/bridge-render.py
printf '#!/bin/sh\nexec /usr/bin/python3 /usr/local/bin/bridge-render.py "$@"\n' > /usr/local/bin/bridge-render.sh
chmod 0755 /usr/local/bin/bridge-render.sh
install -m 0755 "$SRC_DIR/bridge-admin/bridge-admin.py" /usr/local/bin/bridge-admin.py
install -m 0644 "$SRC_DIR/hub/bridge-hub.service" /etc/systemd/system/bridge-hub.service
install -m 0644 "$SRC_DIR/bridge-admin/bridge-admin.service" /etc/systemd/system/bridge-admin.service
mkdir -p "$ADMIN_DIR"; chown root:root "$ADMIN_DIR"; chmod 700 "$ADMIN_DIR"
[ -f "$ADMIN_DIR/members.json" ] || printf '{"members": []}\n' > "$ADMIN_DIR/members.json"

# 5) the one deployment config (seed once, never overwritten)
if [ ! -f "$BRIDGE_ROOT/bridge.conf" ]; then
  cp "$SRC_DIR/config/bridge.conf.example" "$BRIDGE_ROOT/bridge.conf"
  if [ -n "${BRIDGE_DOMAIN:-}" ]; then
    sed -i "s/bridge\.example\.com/${BRIDGE_DOMAIN}/g" "$BRIDGE_ROOT/bridge.conf"
  fi
  chown root:bridge "$BRIDGE_ROOT/bridge.conf"; chmod 640 "$BRIDGE_ROOT/bridge.conf"
  echo "seeded $BRIDGE_ROOT/bridge.conf — EDIT IT with your real values."
else
  echo "kept existing $BRIDGE_ROOT/bridge.conf"
fi

systemctl daemon-reload
systemctl enable --now bridge-hub.service bridge-admin.service
echo
echo "AI Bridge hub installed. Remaining pieces (see deploy/README.md):"
echo "  1. ntfy server + its notify account (hub reads creds from /etc/ntfy/sentinel.env)"
echo "  2. nginx site for reading pages + /admin/ (deploy/nginx-bridge-web.conf.example)"
echo "  3. edit $BRIDGE_ROOT/bridge.conf, then: systemctl restart bridge-hub bridge-admin"
echo "  4. add the first member (member-admin page, or protocol section 3)"
