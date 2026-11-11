#!/usr/bin/env python3
"""AI Bridge hub — the VPS-local duty service for the file mailbox.

Runs on the bridge server itself (any VPS) as a systemd service.
It watches the SFTP file mailbox under /srv/bridge/inbox, keeps the one
authoritative unread ledger in /srv/bridge-admin/hub.db (root-only dir:
the mailbox tree itself is writable by the shared bridge SFTP identity,
so the ledger must NOT live there), renders reading pages for new
letters with the existing bridge-render.sh, posts group notifications
to the ntfy topic bridge-group, and publishes per-member unread
manifests at /srv/bridge/status/hub-unread-<member>.json (written via
tmp file + rename, like every bridge file).

Ground rules (from the hub design, 2026-11-10):
  * The files in the inbox are the only truth. hub.db is an index and
    state cache; delete it and a full directory scan rebuilds it.
  * The hub never moves, deletes or rewrites a letter. A letter counts
    as done when it leaves the inbox (the recipient archives it per
    protocol) — no member self-report is involved.
  * Two sensing channels: inotify events for speed, plus a full
    reconciliation scan every RECONCILE_SECS that diffs directory vs
    ledger, so anything missed while the hub was down is caught up.
  * Live behaviour is gated by HUB_NOTIFY / HUB_RENDER in the
    deployment config (bridge.conf at the bridge root); both default
    to false
    (shadow mode: reconcile + ledger + manifests + logs only).
  * Group notifications reuse the sentinel's ntfy admin account from
    /etc/ntfy/sentinel.env and post direct to 127.0.0.1 via curl
    --resolve, exactly like the old sentinel did. The old
    bridge-sentinel service stays disabled; this service replaces it.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

def _load_kv(path):
    """Parse a KEY=VALUE config file; missing file means empty."""
    data = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
    except OSError:
        pass
    return data


# All site values come from the ONE deployment config file, bridge.conf,
# which lives at the bridge root (template: config/bridge.conf.example in
# the project repository). Environment variables override it; the built-in
# fallbacks are documentation placeholders. NTFY_RESOLVE is a curl
# --resolve pin ("host:port:127.0.0.1") for when ntfy lives on the same
# machine and public DNS would hairpin; empty disables the pin.
BRIDGE_ROOT = os.environ.get("BRIDGE_ROOT", "/srv/bridge")
BRIDGE_CONF_PATH = os.environ.get(
    "BRIDGE_CONF_PATH", os.path.join(BRIDGE_ROOT, "bridge.conf"))
SITE = _load_kv(BRIDGE_CONF_PATH)


def _site(key, env_key, default):
    return os.environ.get(env_key) or SITE.get(key) or default


INBOX_ROOT = os.path.join(BRIDGE_ROOT, "inbox")
BROADCAST_DIR = "_broadcast"
STATUS_DIR = os.path.join(BRIDGE_ROOT, "status")
ADMIN_DIR = _site("ADMIN_DIR", "BRIDGE_ADMIN_DIR", "/srv/bridge-admin")
DB_PATH = os.path.join(ADMIN_DIR, "hub.db")
CONF_PATH = os.path.join(ADMIN_DIR, "hub.conf")
MEMBERS_PATH = os.path.join(ADMIN_DIR, "members.json")
RENDER_SH = _site("RENDER_SH", "HUB_RENDER_SH", "/usr/local/bin/bridge-render.sh")
SENTINEL_ENV = "/etc/ntfy/sentinel.env"
NTFY_BASE = _site("NTFY_BASE", "HUB_NTFY_BASE", "https://bridge.example.com").rstrip("/")
NTFY_TOPIC = _site("NTFY_TOPIC", "HUB_NTFY_TOPIC", "bridge-group")
NTFY_URL = os.environ.get("HUB_NTFY_URL") or (NTFY_BASE + "/" + NTFY_TOPIC)
NTFY_RESOLVE = _site("NTFY_RESOLVE", "HUB_NTFY_RESOLVE", "")
WEB_BASE = _site("WEB_BASE", "HUB_WEB_BASE", "https://bridge.example.com:8443").rstrip("/")
RECONCILE_SECS = int(_site("RECONCILE_SECS", "HUB_RECONCILE_SECS", "300"))
INOTIFY_DEBOUNCE_SECS = 2
REMIND_AFTER_HOURS = int(_site("REMIND_AFTER_HOURS", "HUB_REMIND_AFTER_HOURS", "24"))
NO_SUBJECT = "（这封信没写主题）"
HEADER_KEYS = ("id", "from", "to", "created_utc", "type", "reply_to", "subject")


def now_utc():
    return datetime.now(timezone.utc)


def iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"{iso_z(now_utc())} hub: {msg}", flush=True)


def load_conf():
    """Live-mode switches. Primary source: HUB_NOTIFY / HUB_RENDER in
    bridge.conf (re-read on every call, so toggles apply without a
    restart). A legacy hub.conf JSON in the admin dir is honoured when
    bridge.conf does not set the keys. No config at all means shadow
    mode (both off)."""
    conf = {"notify": False, "render": False}
    site = _load_kv(BRIDGE_CONF_PATH)
    if "HUB_NOTIFY" in site or "HUB_RENDER" in site:
        conf["notify"] = site.get("HUB_NOTIFY", "").lower() in ("1", "true", "yes")
        conf["render"] = site.get("HUB_RENDER", "").lower() in ("1", "true", "yes")
        return conf
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for k in conf:
            if isinstance(data.get(k), bool):
                conf[k] = data[k]
    except (OSError, ValueError):
        pass
    return conf


def load_members():
    """Members with an inbox, from the bridge-admin registry (read-only)."""
    try:
        with open(MEMBERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log(f"members registry unreadable: {e}")
        return []
    out = []
    for m in data.get("members", []):
        if m.get("inbox") and not m.get("system"):
            out.append({"name": m["name"], "inbox": m["inbox"]})
    return out


def parse_header(path):
    """Minimal YAML front-matter reader (stdlib only, first ':' splits)."""
    fields = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = f.readline()
            if first.strip() != "---":
                return fields
            for _ in range(60):
                line = f.readline()
                if not line or line.strip() == "---":
                    break
                if ":" not in line:
                    continue
                key, val = line.split(":", 1)
                key = key.strip()
                val = val.strip().strip("\r\n")
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                if key in HEADER_KEYS:
                    fields[key] = val
    except OSError:
        pass
    return fields


def parse_created(fields, path):
    raw = (fields.get("created_utc") or "").strip()
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
    except OSError:
        return None


def scan_inbox(members):
    """Ground truth: {(rel_path, recipient): info} for every letter."""
    found = {}
    dirs = [(m["inbox"], m["name"], False) for m in members]
    dirs.append((BROADCAST_DIR, None, True))
    for dirname, owner, is_bcast in dirs:
        dpath = os.path.join(INBOX_ROOT, dirname)
        try:
            names = sorted(os.listdir(dpath))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".md"):
                continue
            fpath = os.path.join(dpath, name)
            if not os.path.isfile(fpath):
                continue
            rel = f"inbox/{dirname}/{name}"
            recipients = [m["name"] for m in members] if is_bcast else [owner]
            for recip in recipients:
                found[(rel, recip)] = {
                    "path": fpath,
                    "dirname": dirname,
                    "file": name,
                    "broadcast": is_bcast,
                }
    return found


def db_open():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS letters(
             rel_path TEXT NOT NULL,
             recipient TEXT NOT NULL,
             file TEXT NOT NULL,
             broadcast INTEGER NOT NULL DEFAULT 0,
             letter_id TEXT, sender TEXT, subject TEXT,
             created_utc TEXT, created_ts REAL,
             fingerprint TEXT,
             first_seen_utc TEXT, notified_utc TEXT,
             rendered INTEGER NOT NULL DEFAULT 0,
             reminded INTEGER NOT NULL DEFAULT 0,
             done INTEGER NOT NULL DEFAULT 0,
             done_utc TEXT,
             PRIMARY KEY(rel_path, recipient))"""
    )
    conn.commit()
    return conn


def reconcile(conn, members, conf):
    """One full diff of directory (truth) vs ledger. Idempotent."""
    found = scan_inbox(members)
    rows = {
        (r[0], r[1]): r
        for r in conn.execute(
            "SELECT rel_path, recipient, fingerprint, notified_utc,"
            " rendered, reminded, done FROM letters"
        )
    }
    changed = False
    new_letters = {}  # rel_path -> info (+header), for render/notify

    for (rel, recip), info in found.items():
        st = os.stat(info["path"])
        fp = f"{st.st_mtime_ns}:{st.st_size}"
        row = rows.get((rel, recip))
        if row is None:
            fields = parse_header(info["path"])
            created = parse_created(fields, info["path"])
            conn.execute(
                "INSERT INTO letters(rel_path, recipient, file, broadcast,"
                " letter_id, sender, subject, created_utc, created_ts,"
                " fingerprint, first_seen_utc)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rel,
                    recip,
                    info["file"],
                    1 if info["broadcast"] else 0,
                    fields.get("id") or None,
                    fields.get("from") or None,
                    fields.get("subject") or None,
                    fields.get("created_utc") or (iso_z(created) if created else None),
                    created.timestamp() if created else None,
                    fp,
                    iso_z(now_utc()),
                ),
            )
            changed = True
            log(f"indexed {rel} -> {recip}")
            new_letters.setdefault(rel, info)
        else:
            if row[6]:  # was done but the file is back (restore/move-back)
                conn.execute(
                    "UPDATE letters SET done=0, done_utc=NULL"
                    " WHERE rel_path=? AND recipient=?",
                    (rel, recip),
                )
                changed = True
                log(f"reopened {rel} -> {recip}")
            if row[2] != fp:  # same path, changed bytes: refresh header only
                fields = parse_header(info["path"])
                created = parse_created(fields, info["path"])
                conn.execute(
                    "UPDATE letters SET fingerprint=?, letter_id=?,"
                    " sender=?, subject=?, created_utc=?, created_ts=?"
                    " WHERE rel_path=? AND recipient=?",
                    (
                        fp,
                        fields.get("id") or None,
                        fields.get("from") or None,
                        fields.get("subject") or None,
                        fields.get("created_utc")
                        or (iso_z(created) if created else None),
                        created.timestamp() if created else None,
                        rel,
                        recip,
                    ),
                )
                changed = True
                log(f"refreshed {rel} -> {recip} (fingerprint changed)")

    for (rel, recip), row in rows.items():
        if (rel, recip) not in found and not row[6]:
            conn.execute(
                "UPDATE letters SET done=1, done_utc=?"
                " WHERE rel_path=? AND recipient=?",
                (iso_z(now_utc()), rel, recip),
            )
            changed = True
            log(f"done {rel} -> {recip} (left inbox)")

    if changed:
        conn.commit()
    return new_letters


def page_url(info):
    rel_html = f"inbox/{info['dirname']}/{info['file']}"
    if rel_html.endswith(".md"):
        rel_html = rel_html[:-3] + ".html"
    return f"{WEB_BASE}/{rel_html}"


_ntfy_creds = None


def ntfy_creds():
    """Admin creds from the sentinel env file (values never logged)."""
    global _ntfy_creds
    if _ntfy_creds is not None:
        return _ntfy_creds
    user = pw = ""
    try:
        with open(SENTINEL_ENV, encoding="utf-8") as f:
            for line in f:
                if "=" not in line:
                    continue
                k, v = line.strip().split("=", 1)
                v = v.strip().strip("\"'")
                if "USER" in k.upper():
                    user = v
                elif "PASS" in k.upper():
                    pw = v
    except OSError as e:
        log(f"sentinel env unreadable: {e}")
    _ntfy_creds = (user, pw)
    return _ntfy_creds


def ntfy_post(title, body):
    user, pw = ntfy_creds()
    if not user or not pw:
        log("notify skipped: no ntfy creds")
        return False
    title = title.replace("\r", " ").replace("\n", " ").strip()
    for attempt in range(3):
        try:
            curl_cmd = [
                "curl", "-s", "--max-time", "10",
                "-w", "\n%{http_code}",
            ]
            if NTFY_RESOLVE:
                curl_cmd += ["--resolve", NTFY_RESOLVE]
            r = subprocess.run(
                curl_cmd + [
                    "-u", f"{user}:{pw}",
                    "-H", f"Title: {title}",
                    "-H", "Markdown: yes",
                    "--data-raw", body,
                    NTFY_URL,
                ],
                capture_output=True, text=True, timeout=20,
            )
            # ntfy answers errors with JSON too: only HTTP 200 plus a
            # message object counts as delivered.
            payload, _, code = r.stdout.rpartition("\n")
            if (
                r.returncode == 0
                and code.strip() == "200"
                and (
                    '"event":"message"' in payload
                    or '"event": "message"' in payload
                )
            ):
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(2)
    log(f"notify FAILED title={title!r}")
    return False


def render_letter(conn, info):
    try:
        r = subprocess.run(
            [RENDER_SH, info["path"]],
            capture_output=True, text=True, timeout=120,
        )
        ok = r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    log(f"render {'ok' if ok else 'FAILED'} {info['path']}")
    return ok


def live_actions(conn, members, conf):
    """Render + notify + remind for everything pending. Live mode only."""
    by_rel = {}
    for rel, recip, bcast, subject, notified, rendered in conn.execute(
        "SELECT rel_path, recipient, broadcast, subject, notified_utc,"
        " rendered FROM letters WHERE done=0"
    ):
        by_rel.setdefault(rel, []).append(
            {
                "recipient": recip,
                "broadcast": bool(bcast),
                "subject": subject,
                "notified": notified,
                "rendered": rendered,
            }
        )
    member_inbox = {m["name"]: m["inbox"] for m in members}
    for rel, recips in sorted(by_rel.items()):
        dirname = rel.split("/")[1]
        fname = rel.split("/")[-1]
        info = {
            "path": os.path.join(BRIDGE_ROOT, rel),
            "dirname": dirname,
            "file": fname,
        }
        subject = next((r["subject"] for r in recips if r["subject"]), None)
        title = subject or NO_SUBJECT
        if conf["render"] and not all(r["rendered"] for r in recips):
            if render_letter(conn, info):
                conn.execute(
                    "UPDATE letters SET rendered=1 WHERE rel_path=?", (rel,)
                )
                conn.commit()
        if not conf["notify"]:
            continue
        pending = [r for r in recips if not r["notified"]]
        if not pending:
            continue
        if recips[0]["broadcast"]:
            body = f"@all 新广播：[{fname}]({page_url(info)})"
            targets = pending
        else:
            recip = recips[0]["recipient"]
            body = f"@{recip} 新信：[{fname}]({page_url(info)})"
            targets = pending
        if ntfy_post(title, body):
            ts = iso_z(now_utc())
            for r in targets:
                conn.execute(
                    "UPDATE letters SET notified_utc=?"
                    " WHERE rel_path=? AND recipient=?",
                    (ts, rel, r["recipient"]),
                )
            conn.commit()
            log(f"notified {rel} ({len(targets)} recipient rows)")

    if not conf["notify"]:
        return
    # One-shot reminders for letters unread past the threshold.
    now = now_utc()
    for rel, recip, bcast, subject, created_ts, reminded in conn.execute(
        "SELECT rel_path, recipient, broadcast, subject, created_ts,"
        " reminded FROM letters WHERE done=0 AND reminded=0"
        " AND created_ts IS NOT NULL"
    ):
        age_h = (now.timestamp() - created_ts) / 3600.0
        if age_h < REMIND_AFTER_HOURS:
            continue
        dirname = rel.split("/")[1]
        fname = rel.split("/")[-1]
        info = {"dirname": dirname, "file": fname}
        who = "@all" if bcast else f"@{recip}"
        title = f"催办：{subject or NO_SUBJECT}"
        body = f"{who} 未读催办：[{fname}]({page_url(info)})"
        if ntfy_post(title, body):
            conn.execute(
                "UPDATE letters SET reminded=1"
                " WHERE rel_path=? AND recipient=?",
                (rel, recip),
            )
            conn.commit()
            log(f"reminded {rel} -> {recip} (age {age_h:.1f}h)")


def write_manifests(conn, members):
    """Per-member unread manifests in status/, tmp + rename (atomic)."""
    now = now_utc()
    for m in members:
        rows = conn.execute(
            "SELECT file, rel_path, letter_id, sender, subject,"
            " created_utc, created_ts, broadcast, notified_utc"
            " FROM letters WHERE recipient=? AND done=0"
            " ORDER BY COALESCE(created_ts, 0), file",
            (m["name"],),
        ).fetchall()
        unread = []
        for f, rel, lid, sender, subject, c_utc, c_ts, bcast, notified in rows:
            age = None
            if c_ts:
                age = round((now.timestamp() - c_ts) / 3600.0, 1)
            unread.append(
                {
                    "file": f,
                    "path": rel,
                    "id": lid,
                    "from": sender,
                    "subject": subject,
                    "created_utc": c_utc,
                    "age_hours": age,
                    "broadcast": bool(bcast),
                    "state": "notified" if notified else "indexed",
                }
            )
        doc = {
            "member": m["name"],
            "generated_utc": iso_z(now),
            "unread_count": len(unread),
            "unread": unread,
        }
        target = os.path.join(
            STATUS_DIR, f"hub-unread-{m['name'].lower()}.json"
        )
        tmp = f"{target}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.chmod(tmp, 0o644)
        os.rename(tmp, target)


def cycle(conn):
    members = load_members()
    conf = load_conf()
    new = reconcile(conn, members, conf)
    if conf["notify"] or conf["render"]:
        live_actions(conn, members, conf)
    write_manifests(conn, members)
    return len(new)


def watch_dirs(members):
    dirs = [os.path.join(INBOX_ROOT, m["inbox"]) for m in members]
    dirs.append(os.path.join(INBOX_ROOT, BROADCAST_DIR))
    return dirs


def main():
    conn = db_open()
    log(f"bridge-hub starting (pid {os.getpid()}), initial reconcile")
    cycle(conn)
    proc = None
    last_reconcile = time.monotonic()

    def start_watcher():
        dirs = watch_dirs(load_members())
        try:
            return subprocess.Popen(
                [
                    "inotifywait", "-m", "-q",
                    "-e", "create", "-e", "moved_to",
                    "-e", "delete", "-e", "moved_from",
                    "--format", "%w|%f",
                ]
                + dirs,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            log(f"inotifywait unavailable: {e}; reconcile-only mode")
            return None

    proc = start_watcher()
    import select

    while True:
        timeout = RECONCILE_SECS - (time.monotonic() - last_reconcile)
        if timeout <= 0:
            cycle(conn)
            last_reconcile = time.monotonic()
            continue
        if proc is None or proc.poll() is not None:
            if proc is not None:
                log("inotifywait exited; restarting watcher")
            proc = start_watcher()
            time.sleep(min(timeout, 5))
            continue
        r, _, _ = select.select([proc.stdout], [], [], timeout)
        if not r:
            continue  # timeout reached; loop top reconciles
        line = proc.stdout.readline()
        if not line:
            continue
        parts = line.strip().split("|", 1)
        if len(parts) == 2 and parts[1].endswith(".md"):
            time.sleep(INOTIFY_DEBOUNCE_SECS)
            # drain any burst of queued events before one reconcile
            while select.select([proc.stdout], [], [], 0.2)[0]:
                if not proc.stdout.readline():
                    break
            cycle(conn)
            last_reconcile = time.monotonic()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
