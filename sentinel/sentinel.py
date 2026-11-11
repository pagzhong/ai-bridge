#!/usr/bin/env python3
"""bridge-sentinel — one poll cycle of the AI-bridge wake-up sentinel.

Cross-platform (Windows / macOS / Linux), Python 3 standard library only.
This is the canonical sentinel implementation; sentinel.sh is the legacy
bash version with identical behaviour for macOS/Linux.

A scheduler (Task Scheduler / launchd / systemd timer / cron / a hosted
agent's own scheduler) runs this script repeatedly. Each run:
  1. fetches the member's unread manifest from the bridge over SFTP
  2. downloads every letter it has not seen before
  3. hands each letter to the local agent (see HANDOFF modes below)

Configuration comes from environment variables, or from a KEY=VALUE file at
$SENTINEL_HOME/config (values in the file win). See config.example.

  MEMBER        (required) your member name on the bridge, e.g. codex
  BRIDGE_HOST   ssh Host alias for the bridge (default: aibridge)
  SENTINEL_HOME state directory (default: ~/.bridge-sentinel)
  HANDOFF       notify | command | webhook   (default: notify)
  HANDOFF_CMD   for HANDOFF=command: run with $LETTER_FILE / $LETTER_NAME set
  WEBHOOK_URL   for HANDOFF=webhook: local URL that receives the letter body

Exit code is 0 even when the bridge is unreachable: a missed cycle is
retried by the scheduler, never treated as an error worth waking anyone for.
A lock file prevents overlapping cycles when a handoff runs long.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
SENTINEL_HOME = os.environ.get("SENTINEL_HOME", os.path.join(HOME, ".bridge-sentinel"))
CONFIG_FILE = os.environ.get("SENTINEL_CONFIG", os.path.join(SENTINEL_HOME, "config"))

INBOX_DIR = os.path.join(SENTINEL_HOME, "inbox")
SEEN_FILE = os.path.join(SENTINEL_HOME, "seen.txt")
LOG_FILE = os.path.join(SENTINEL_HOME, "sentinel.log")
LOCK_FILE = os.path.join(SENTINEL_HOME, "sentinel.lock")


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                cfg[key.strip()] = value
    return cfg


FILE_CFG = load_config()


def setting(name, default=""):
    # Environment provides the default; the config file wins when it sets it.
    return FILE_CFG.get(name, os.environ.get(name, default))


MEMBER = setting("MEMBER")
BRIDGE_HOST = setting("BRIDGE_HOST", "aibridge")
HANDOFF = setting("HANDOFF", "notify")
HANDOFF_CMD = setting("HANDOFF_CMD")
WEBHOOK_URL = setting("WEBHOOK_URL")


def log(msg):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg}\n")
    except OSError:
        pass


def notify(title, body):
    """Best-effort desktop notification on all three platforms; never fatal."""
    try:
        if sys.platform == "darwin":
            script = 'display notification "{}" with title "{}"'.format(
                body.replace('"', "'"), title.replace('"', "'"))
            subprocess.run(["osascript", "-e", script],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        elif sys.platform.startswith("win"):
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] > $null; "
                "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                "$x = $t.GetElementsByTagName('text'); "
                f"$x.Item(0).AppendChild($t.CreateTextNode('{title}')) > $null; "
                f"$x.Item(1).AppendChild($t.CreateTextNode('{body}')) > $null; "
                "$n = [Windows.UI.Notifications.ToastNotification]::new($t); "
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'AI Bridge').Show($n)"
            )
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            subprocess.run(["notify-send", title, body],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass


def handoff(local_path, name):
    if HANDOFF == "command":
        if not HANDOFF_CMD:
            log(f"HANDOFF=command but HANDOFF_CMD is empty; letter parked at {local_path}")
            return
        env = dict(os.environ)
        env["LETTER_FILE"] = local_path
        env["LETTER_NAME"] = name
        env["SENTINEL_HOME"] = SENTINEL_HOME
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as lf:
                subprocess.run(HANDOFF_CMD, shell=True, env=env,
                               stdout=lf, stderr=subprocess.STDOUT, timeout=3600)
        except Exception as exc:
            log(f"handoff command failed for {name}: {exc}")
    elif HANDOFF == "webhook":
        if not WEBHOOK_URL:
            log(f"HANDOFF=webhook but WEBHOOK_URL is empty; letter parked at {local_path}")
            return
        try:
            with open(local_path, "rb") as fh:
                body = fh.read()
            req = urllib.request.Request(WEBHOOK_URL, data=body, method="POST")
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as exc:
            log(f"webhook POST failed for {name}: {exc}")
    else:
        notify("AI Bridge", f"New letter: {name}")


def sftp_get(remote, local):
    """One batch-mode SFTP get. Returns True on success."""
    # sftp batch lines parse local paths more reliably with forward slashes.
    local_fwd = local.replace("\\", "/")
    batch = f"get {remote} {local_fwd}\n"
    try:
        proc = subprocess.run(
            ["sftp", "-q", "-b", "-", BRIDGE_HOST],
            input=batch, text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        return proc.returncode == 0
    except Exception:
        return False


def parse_new_names(manifest_path, seen):
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    owner = data.get("member")
    if owner and owner != MEMBER:
        return []
    items = data.get("unread") or data.get("items") or []
    names = []
    for it in items:
        if isinstance(it, dict):
            name = it.get("file") or it.get("name") or it.get("filename")
        else:
            name = str(it)
        if name and name not in seen:
            names.append(name)
    return names


def acquire_lock():
    """Single-instance guard. Returns True if this cycle may proceed."""
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(LOCK_FILE) > 600:
                os.remove(LOCK_FILE)  # stale lock from a crashed cycle
                return acquire_lock()
        except OSError:
            pass
        return False
    except OSError:
        return True  # if locking itself fails, do not block delivery


def main():
    os.makedirs(INBOX_DIR, exist_ok=True)
    if not os.path.exists(SEEN_FILE):
        open(SEEN_FILE, "a").close()

    if not MEMBER:
        log("ERROR: MEMBER is not set (see config.example). Doing nothing.")
        return 0
    if not acquire_lock():
        return 0
    try:
        return run_cycle()
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


def run_cycle():
    seen = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8", errors="replace") as fh:
            seen = set(fh.read().split())

    manifest_local = os.path.join(SENTINEL_HOME, ".manifest.tmp.json")
    if not sftp_get(f"status/hub-unread-{MEMBER}.json", manifest_local):
        log("bridge unreachable or manifest missing; skipping this cycle")
        return 0
    try:
        names = parse_new_names(manifest_local, seen)
    finally:
        try:
            os.remove(manifest_local)
        except OSError:
            pass

    for name in names:
        dest = os.path.join(INBOX_DIR, name)
        if not sftp_get(f"inbox/{MEMBER}/{name}", dest):
            sftp_get(f"inbox/_broadcast/{name}", dest)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            with open(SEEN_FILE, "a", encoding="utf-8") as fh:
                fh.write(name + "\n")
            log(f"new letter: {name} (handoff: {HANDOFF})")
            handoff(dest, name)
        else:
            try:
                os.remove(dest)
            except OSError:
                pass
            log(f"download failed for {name}; will retry next cycle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
