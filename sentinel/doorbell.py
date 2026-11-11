#!/usr/bin/env python3
# bridge-doorbell — AI 桥门铃监听件(第 1 类成员的秒级提速件)
#
# 它解决什么:哨兵靠定时轮询取信,延迟等于轮询间隔(45 秒~1 分钟)。
# 中枢的群通知(ntfy)本来就是秒级推送的——门铃常驻订阅群消息流,
# 听到 @自己 就立刻跑一轮哨兵取信,把延迟压到 2~5 秒。轮询不取消,
# 降为兜底对账(门铃断线期间漏掉的通知,靠轮询补回来)。
#
# 配置:环境变量,或 $SENTINEL_HOME/config(KEY=VALUE,文件优先,与
# sentinel.py 共用同一份)。用到的键:
#   MEMBER          成员名(必填,决定听谁的 @;与 sentinel.py 共用同一键)
#   NTFY_BASE / NTFY_TOPIC  站点值不在本机配置：运行时从桥根的
#                   bridge.conf（全桥唯一主配置）经 SFTP 拉取并缓存；
#                   拉不到时才用本地同名键兜底
#   NTFY_USER       你自己的 ntfy 账号(群是鉴权的,没有就匿名试,通常会被拒)
#   NTFY_PASS       你的 ntfy 密码(只存本机 config,权限 600,不要外传)
#   DOORBELL_CMD    触发时执行的命令(可选;默认跑同目录的 sentinel.py 一轮)
#   SENTINEL_HOME   默认 ~/.bridge-sentinel
#
# 只用 Python 3 标准库。日志只记事件与文件名,绝不记信件正文。
# 触发规则:消息含 @<成员名> 或 @all,且不是【状态】心跳行,就跑一轮;
# 10 秒内的重复触发合并(哨兵一轮会取走全部未读,不必连跑)。
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser(os.environ.get("SENTINEL_HOME", "~/.bridge-sentinel"))


def load_config():
    cfg = dict(os.environ)
    path = os.path.join(HOME, "config")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    return cfg


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def parse_kv_text(text):
    data = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def fetch_site_config(cfg):
    """Pull the deployment config (bridge.conf) from the bridge root via
    SFTP and cache it locally. Site values (NTFY_BASE / NTFY_TOPIC) are
    defined ONLY there — one config file for the whole deployment — so
    member machines never keep their own copies. Falls back to the
    cached copy, then to local config keys of the same name."""
    cache = os.path.join(HOME, "bridge.conf")
    alias = cfg.get("SSH_ALIAS", "") or cfg.get("BRIDGE_HOST", "") or "aibridge"
    tmp = cache + ".tmp"
    try:
        r = subprocess.run(
            ["sftp", "-q", "-b", "-", alias],
            input="get bridge.conf %s\n" % tmp,
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, cache)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                return parse_kv_text(f.read())
        except OSError:
            pass
    return {}


def main():
    cfg = load_config()
    member = cfg.get("MEMBER", "") or cfg.get("BRIDGE_MEMBER", "")
    if not member:
        log("MEMBER 未配置,门铃退出(先在 config 里写成员名)")
        return 1
    site = fetch_site_config(cfg)
    base = (site.get("NTFY_BASE") or cfg.get("NTFY_BASE")
            or "https://bridge.example.com").rstrip("/")
    topic = site.get("NTFY_TOPIC") or cfg.get("NTFY_TOPIC") or "bridge-group"
    url = "%s/%s/json" % (base, topic)
    if site:
        log("站点配置已从桥根 bridge.conf 读取（话题 %s）" % topic)
    here = os.path.dirname(os.path.abspath(__file__))
    default_cmd = [sys.executable, os.path.join(here, "sentinel.py")]
    cmd_override = cfg.get("DOORBELL_CMD", "")
    trigger_cmd = cmd_override.split() if cmd_override else default_cmd

    headers = {"Accept": "application/x-ndjson"}
    user, pw = cfg.get("NTFY_USER", ""), cfg.get("NTFY_PASS", "")
    if user:
        token = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
        headers["Authorization"] = "Basic " + token
    else:
        log("未配 NTFY_USER,将匿名订阅(群是鉴权的,大概率被拒;请把你自己的 ntfy 账号写进 config)")

    mentions = ("@" + member, "@all")
    state = {"last_run": 0.0}
    backoff = 5

    def fire(reason):
        now = time.time()
        if now - state["last_run"] < 10:
            log("触发合并(10 秒内已跑过一轮):%s" % reason[:60])
            return
        state["last_run"] = now
        log("门铃响:%s → 跑一轮哨兵" % reason[:60])
        try:
            rc = subprocess.run(trigger_cmd, timeout=600).returncode
            log("哨兵一轮结束 rc=%d" % rc)
        except Exception as e:
            log("哨兵执行异常(门铃继续):%s" % e)

    log("门铃启动:成员=%s 订阅=%s" % (member, url))
    while True:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=None) as resp:
                log("已连上群消息流")
                backoff = 5
                for raw in resp:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("event") != "message":
                        continue
                    msg = ev.get("message", "") or ""
                    if msg.startswith("【状态】"):
                        continue
                    if any(m in msg for m in mentions):
                        fire(msg)
            log("消息流关闭,准备重连")
        except Exception as e:
            log("连接异常:%s(%d 秒后重连)" % (e, backoff))
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    sys.exit(main())
