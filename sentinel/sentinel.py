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

Task idempotency: task letters (type: task) are checked against the
local task registry ($SENTINEL_HOME/tasks.json) before handoff. A task
whose task_id is already recorded as claimed/done (or failed without a
higher attempt) is NOT handed off again; the agent receives a short
DUPLICATE- notice instead, per the protocol's task semantics. Agents
report execution state back with:
  sentinel.py task <task_id> <claimed|done|failed> [result_letter_id]

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
TASKS_FILE = os.path.join(SENTINEL_HOME, "tasks.json")


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



def parse_header(path):
    """Read a letter's front-matter block into a dict (best effort)."""
    hdr = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(8192)
    except OSError:
        return hdr
    if not text.startswith("---"):
        return hdr
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            k, v = line.split(":", 1)
            hdr[k.strip()] = v.strip()
    return hdr


def load_tasks():
    try:
        with open(TASKS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_tasks(tasks):
    tmp = TASKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(tasks, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, TASKS_FILE)


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def task_gate(dest, name):
    """Idempotency gate for task letters.

    Returns "pass" to hand the letter off normally, or the path of a
    DUPLICATE- notice file to hand off instead. Registry states follow
    the protocol: delivered (handed off, no state reported yet),
    claimed, done, failed. A task re-delivered with a higher attempt
    after a recorded failure passes; everything else already known is
    a duplicate.
    """
    hdr = parse_header(dest)
    if hdr.get("type") != "task":
        return "pass"
    task_id = hdr.get("task_id") or hdr.get("id") or ""
    if not task_id:
        return "pass"
    try:
        attempt = int(hdr.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    tasks = load_tasks()
    rec = tasks.get(task_id)
    if rec is None:
        tasks[task_id] = {"state": "delivered", "letter_id": hdr.get("id", ""),
                          "attempt": attempt, "updated_utc": _utc_now()}
        save_tasks(tasks)
        return "pass"
    state = rec.get("state", "delivered")
    if state == "failed" and attempt > int(rec.get("attempt", 1)):
        rec.update({"state": "delivered", "attempt": attempt,
                    "letter_id": hdr.get("id", ""), "updated_utc": _utc_now()})
        save_tasks(tasks)
        return "pass"
    notice = os.path.join(INBOX_DIR, "DUPLICATE-" + name)
    body = (
        "---\n"
        "type: duplicate-task-notice\n"
        f"task_id: {task_id}\n"
        f"recorded_state: {state}\n"
        f"original_letter: {name}\n"
        f"recorded_result: {rec.get('result_id', '')}\n"
        "---\n\n"
        "这封任务信是重复投递：该 task_id 已在本地任务登记中"
        f"（状态 {state}）。按协议「任务语义」不要重新执行："
        "done 的回 note 写明原结果信 id；claimed 的回 note 说明已在执行；"
        "failed 且 attempt 未增加的回 note 附原失败说明。"
        f"原信全文在本机：{dest}\n"
    )
    with open(notice, "w", encoding="utf-8") as fh:
        fh.write(body)
    log(f"duplicate task suppressed: {name} (task_id={task_id}, state={state})")
    return notice


def cmd_task(argv):
    """sentinel.py task <task_id> <claimed|done|failed> [result_letter_id]"""
    if len(argv) < 4 or argv[3] not in ("claimed", "done", "failed"):
        print("usage: sentinel.py task <task_id> <claimed|done|failed> [result_letter_id]")
        return 2
    task_id, state = argv[2], argv[3]
    tasks = load_tasks()
    rec = tasks.get(task_id, {"attempt": 1, "letter_id": ""})
    rec["state"] = state
    rec["updated_utc"] = _utc_now()
    if len(argv) >= 5:
        rec["result_id"] = argv[4]
    tasks[task_id] = rec
    save_tasks(tasks)
    print(json.dumps({task_id: rec}, ensure_ascii=False))
    return 0


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
            gate = task_gate(dest, name)
            if gate == "pass":
                handoff(dest, name)
            else:
                handoff(gate, os.path.basename(gate))
        else:
            try:
                os.remove(dest)
            except OSError:
                pass
            log(f"download failed for {name}; will retry next cycle")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "task":
        raise SystemExit(cmd_task(sys.argv))
    raise SystemExit(main())
