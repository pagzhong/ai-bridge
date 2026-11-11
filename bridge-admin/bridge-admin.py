#!/usr/bin/env python3
"""AI Bridge member admin — a VPS-local web service.

Runs on the bridge server itself, bound to 127.0.0.1:8471, behind the
existing nginx Basic Auth at https://bridge.example.com:8443/admin/ .
Nothing here depends on any external machine: the page the bridge owner
opens performs the action on this server, immediately.

Operations per regular member (system accounts are refused server-side):
  revoke  - comment the member's line in the bridge authorized_keys and
            remove the member's ntfy (group) account.
  restore - uncomment the key line; recreate the ntfy account with a NEW
            random password, shown exactly once in the POST response so
            the owner can hand it to the member. The plaintext password
            is never stored anywhere (ntfy keeps only its hash).
  delete  - archive the public key line to a root-only file, remove the
            key line, remove the ntfy account, move the member's inbox
            aside to /srv/bridge/removed-<name>-<date>/ (not destroyed),
            and drop the member from the registry.

Every action is appended to /srv/bridge-admin/audit.log.
State shown on the page is derived live from authorized_keys and the
ntfy user list, not from cached flags.
"""
import html
import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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


# Site values come from the ONE deployment config, bridge.conf at the
# bridge root (template: config/bridge.conf.example in the repository).
# Environment variables override; fallbacks are placeholders/defaults.
BRIDGE_ROOT = os.environ.get("BRIDGE_ROOT", "/srv/bridge")
_SITE = _load_kv(os.environ.get(
    "BRIDGE_CONF_PATH", os.path.join(BRIDGE_ROOT, "bridge.conf")))
BASE = os.environ.get("BRIDGE_ADMIN_DIR") or _SITE.get("ADMIN_DIR") or "/srv/bridge-admin"
REG_PATH = os.path.join(BASE, "members.json")
AUDIT_PATH = os.path.join(BASE, "audit.log")
AK_PATH = os.path.join(BRIDGE_ROOT, ".ssh", "authorized_keys")
REMOVED_KEYS = "/root/bridge-removed-keys.txt"
INBOX_ROOT = os.path.join(BRIDGE_ROOT, "inbox")
NTFY = "/usr/bin/ntfy"
TOPIC = os.environ.get("NTFY_TOPIC") or _SITE.get("NTFY_TOPIC") or "bridge-group"
REVOKED_PREFIX = "#REVOKED# "
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,15}$")
TOKEN = secrets.token_urlsafe(24)  # per-process form token

CSS = """
body{font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
background:#f6f4ef;color:#1c2430;margin:0;padding:20px 14px 48px}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:24px;margin:6px 0 2px}
.sub{color:#5b6572;font-size:14px;margin-bottom:18px}
.card{background:#fff;border:1px solid #e3ded2;border-radius:12px;padding:16px 18px;margin-bottom:14px}
.card h2{font-size:19px;margin:0 0 4px;display:inline-block}
.badge{font-size:13px;border-radius:20px;padding:2px 10px;margin-left:8px;vertical-align:2px}
.ok{background:#e2f2e4;color:#1d6b2f}.off{background:#fbe3e0;color:#a02c1d}.sys{background:#e8e6df;color:#5b6572}
.meta{color:#5b6572;font-size:13.5px;line-height:1.55;word-break:break-all}
.btns{margin-top:12px}
button{font-size:16px;border:0;border-radius:9px;padding:10px 20px;margin-right:10px;cursor:pointer}
.b-revoke{background:#b3261e;color:#fff}.b-restore{background:#1d6b2f;color:#fff}
.b-delete{background:#fff;color:#b3261e;border:1.5px solid #b3261e}
.msg{border-radius:12px;padding:14px 18px;margin-bottom:16px;font-size:16px;line-height:1.6}
.good{background:#e2f2e4;border:1px solid #9fd3a8}.bad{background:#fbe3e0;border:1px solid #eda79d}
.pw{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:19px;background:#fff;
border:1.5px dashed #1d6b2f;border-radius:9px;padding:10px 14px;margin-top:8px;word-break:break-all}
.log{background:#fff;border:1px solid #e3ded2;border-radius:12px;padding:14px 18px;font-size:13.5px}
.log div{padding:3px 0;border-bottom:1px solid #f0ede4;color:#3c4654}
.log div:last-child{border-bottom:0}
a{color:#0b5cad}
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(msg):
    with open(AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(f"{now_iso()} {msg}\n")


def load_reg():
    with open(REG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_reg(reg):
    tmp = REG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REG_PATH)


def run(cmd, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, (p.stdout + p.stderr).strip()


def ak_lines():
    with open(AK_PATH, encoding="utf-8") as f:
        return f.read().splitlines()


def ak_write(lines):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    shutil.copy2(AK_PATH, f"{AK_PATH}.bak-{ts}")
    data = "\n".join(lines) + "\n"
    with open(AK_PATH, "r+", encoding="utf-8") as f:
        f.seek(0)
        f.write(data)
        f.truncate()


def key_state(member):
    """active | revoked | gone | none — judged from authorized_keys itself."""
    target = member.get("key_line")
    if not target:
        return "none"
    for ln in ak_lines():
        if ln == target:
            return "active"
        if ln == REVOKED_PREFIX + target:
            return "revoked"
    return "gone"


def ntfy_users():
    _rc, out = run([NTFY, "user", "list"])
    users = []
    for ln in out.splitlines():
        m = re.match(r"^user (\S+) \(", ln)
        if m:
            users.append(m.group(1))
    return users


def do_revoke(m):
    if key_state(m) != "active":
        return False, "钥匙当前不是生效状态，不能吊销。", None
    lines = [REVOKED_PREFIX + ln if ln == m["key_line"] else ln for ln in ak_lines()]
    ak_write(lines)
    if m.get("ntfy_user"):
        rc, out = run([NTFY, "user", "remove", m["ntfy_user"]])
        if rc != 0:
            audit(f"REVOKE {m['name']} PARTIAL ntfy remove failed: {out}")
            return False, f"钥匙已停用，但删除群账号失败：{html.escape(out)}", None
    audit(f"REVOKE {m['name']} ok")
    return True, "已吊销：SFTP 钥匙已停用、群账号已删除。这个成员现在连不上桥，也进不了群。", None


def do_restore(m):
    if key_state(m) != "revoked":
        return False, "钥匙当前不是已吊销状态，不能恢复。", None
    lines = [ln[len(REVOKED_PREFIX):] if ln == REVOKED_PREFIX + m["key_line"] else ln
             for ln in ak_lines()]
    ak_write(lines)
    new_pw = None
    if m.get("ntfy_user"):
        new_pw = secrets.token_urlsafe(12)
        rc, out = run([NTFY, "user", "add", m["ntfy_user"]],
                      env_extra={"NTFY_PASSWORD": new_pw})
        if rc != 0:
            audit(f"RESTORE {m['name']} PARTIAL ntfy add failed: {out}")
            return False, f"钥匙已恢复，但重建群账号失败：{html.escape(out)}", None
        rc, out = run([NTFY, "access", m["ntfy_user"], TOPIC, "rw"])
        if rc != 0:
            audit(f"RESTORE {m['name']} PARTIAL ntfy access failed: {out}")
            return False, f"群账号已重建，但授权失败：{html.escape(out)}", None
    audit(f"RESTORE {m['name']} ok (new group password issued)")
    msg = ("已恢复：SFTP 钥匙重新生效，群账号已重建。"
           "群的新密码只显示这一次，请你亲手转交给该成员——页面和服务器都不存明文。")
    return True, msg, new_pw


def do_delete(m):
    with open(REMOVED_KEYS, "a", encoding="utf-8") as f:
        f.write(f"# removed {now_iso()} via admin page\n{m['key_line']}\n")
    lines = [ln for ln in ak_lines()
             if ln != m["key_line"] and ln != REVOKED_PREFIX + m["key_line"]]
    ak_write(lines)
    if m.get("ntfy_user"):
        run([NTFY, "user", "remove", m["ntfy_user"]])
    moved = ""
    if m.get("inbox"):
        src = os.path.join(INBOX_ROOT, m["inbox"])
        if os.path.isdir(src):
            dst = os.path.join(
                BRIDGE_ROOT,
                f"removed-{m['name']}-{datetime.now(timezone.utc).strftime('%Y%m%d')}")
            shutil.move(src, dst)
            moved = f"信箱已移到 {dst}（没有销毁）。"
    reg = load_reg()
    reg["members"] = [x for x in reg["members"] if x["name"] != m["name"]]
    save_reg(reg)
    audit(f"DELETE {m['name']} ok")
    return True, f"已删除：钥匙已除名（公钥封存在 root 档案里），群账号已删除。{moved}", None


ACTIONS = {"revoke": do_revoke, "restore": do_restore, "delete": do_delete}


def member_card(m, users):
    name = html.escape(m["name"])
    disp = html.escape(m.get("display", m["name"]))
    note = html.escape(m.get("note", ""))
    if m.get("system"):
        badge = '<span class="badge sys">系统账号 · 只展示</span>'
        btns = ""
        state = note
    else:
        ks = key_state(m)
        has_ntfy = m.get("ntfy_user") in users if m.get("ntfy_user") else False
        if ks == "active":
            badge = '<span class="badge ok">正常</span>'
            state = "SFTP 钥匙生效中" + ("，群账号在" if has_ntfy else "，群账号不在")
            btns = (f'<button class="b-revoke" type="submit" name="action" value="revoke">吊销</button>'
                    f'<button class="b-delete" type="submit" name="action" value="delete" '
                    f'onclick="return confirm(\'确定要删除 {disp} 吗？钥匙和群账号都会除名，信箱会被移走封存。\')">删除</button>')
        elif ks == "revoked":
            badge = '<span class="badge off">已吊销</span>'
            state = "SFTP 钥匙已停用，群账号已删除"
            btns = (f'<button class="b-restore" type="submit" name="action" value="restore">恢复</button>'
                    f'<button class="b-delete" type="submit" name="action" value="delete" '
                    f'onclick="return confirm(\'确定要删除 {disp} 吗？\')">删除</button>')
        else:
            badge = '<span class="badge off">钥匙缺失</span>'
            state = "在 authorized_keys 里找不到这个成员的钥匙，请找 Muse 检查"
            btns = ""
    fp = html.escape(m.get("fingerprint", ""))
    fp_html = f'<div class="meta">钥匙指纹：{fp}</div>' if fp else ""
    form_open = "" if m.get("system") or not btns else (
        f'<form method="post" action="action">'
        f'<input type="hidden" name="token" value="{TOKEN}">'
        f'<input type="hidden" name="member" value="{name}">')
    form_close = "</form>" if form_open else ""
    btn_html = f'<div class="btns">{btns}</div>' if btns else ""
    return (f'<div class="card"><h2>{disp}</h2>{badge}'
            f'<div class="meta">{html.escape(state)}</div>{fp_html}'
            f'{form_open}{btn_html}{form_close}</div>')


def render_page(message=None, ok=True, new_pw=None):
    reg = load_reg()
    users = ntfy_users()
    cards = "".join(member_card(m, users) for m in reg["members"])
    msg_html = ""
    if message:
        cls = "good" if ok else "bad"
        pw_html = f'<div class="pw">{html.escape(new_pw)}</div>' if new_pw else ""
        msg_html = f'<div class="msg {cls}">{html.escape(message)}{pw_html}</div>'
    log_html = ""
    if os.path.exists(AUDIT_PATH):
        with open(AUDIT_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()[-10:]
        if lines:
            items = "".join(f"<div>{html.escape(ln)}</div>" for ln in reversed(lines))
            log_html = f'<div class="log"><b>最近操作记录</b>{items}</div>'
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>桥成员管理</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>桥成员管理</h1>
<div class="sub">AI 桥 · 服务器本地服务 &nbsp;|&nbsp; <a href="../">← 回信件目录</a></div>
{msg_html}{cards}{log_html}
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", ""):
            self._send(render_page())
        elif path == "/health":
            self._send("ok")
        else:
            self._send("not found", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/action":
            self._send("not found", 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        if form.get("token", [""])[0] != TOKEN:
            self._send(render_page("表单已过期，请刷新页面再试。", ok=False), 400)
            return
        name = form.get("member", [""])[0]
        action = form.get("action", [""])[0]
        if not NAME_RE.match(name) or action not in ACTIONS:
            self._send(render_page("请求不合法。", ok=False), 400)
            return
        reg = load_reg()
        m = next((x for x in reg["members"] if x["name"] == name), None)
        if m is None:
            self._send(render_page("名单里没有这个成员。", ok=False), 404)
            return
        if m.get("system"):
            audit(f"REFUSED {action} on system account {name}")
            self._send(render_page("系统账号不能在这里处置。", ok=False), 403)
            return
        try:
            ok, msg, new_pw = ACTIONS[action](m)
        except Exception as e:  # keep the page alive, surface the failure
            audit(f"{action.upper()} {name} ERROR {e}")
            ok, msg, new_pw = False, f"执行出错：{e}", None
        self._send(render_page(msg, ok=ok, new_pw=new_pw))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8471), Handler)
    print("bridge-admin listening on 127.0.0.1:8471", flush=True)
    server.serve_forever()
