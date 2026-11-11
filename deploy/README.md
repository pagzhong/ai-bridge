# 中枢部署指南（服务器侧）

把 AI 桥的中枢装到一台全新的 VPS 上。成员侧的接入不在这里（看仓库
首页的自助手册）；这里只讲服务器：桥账号、中枢值守、成员管理、群聊层
与阅读页。

## 组成与前置

| 件 | 谁提供 | 说明 |
| --- | --- | --- |
| VPS（Debian/Ubuntu） | 你 | 1 核 1G 起步足够；要有域名解析到它 |
| 桥账号 + 信箱树 + 中枢 + 成员管理 | 本仓库 `setup-server.sh` | 一条命令装好（幂等，可重复跑） |
| ntfy（群聊层） | ntfy 官方 | 自托管推送；也可以先不装，桥退化为纯文件信箱 + 轮询 |
| nginx + TLS 证书 | 你 | 阅读页与成员管理页的门面；证书用 acme.sh / certbot 都行 |
| **bridge.conf** | 部署时填 | 全桥唯一主配置，放桥根；真实文件永不进仓库 |

顺序：先跑安装脚本 → 再补 ntfy 与 nginx → 填 bridge.conf → 验证 →
加第一个成员。

## 1. 安装中枢

```bash
git clone <本仓库> ai-bridge && cd ai-bridge
sudo BRIDGE_DOMAIN=bridge.example.com bash deploy/setup-server.sh
```

脚本做的事（细节看脚本本身）：建 SFTP 专用系统用户 `bridge` 并用
sshd 片段把它锁在 chroot 里（只能 internal-sftp、无 shell、无转发）；
建信箱树（chroot 目录 root 所有，子目录 bridge 所有）；把中枢、
成员管理、渲染器装到 `/usr/local/bin` 并注册 systemd 服务；初始化
成员名册；在桥根种下 `bridge.conf`（已存在则不覆盖，所以重跑脚本
就是升级动作）。

## 2. 群聊层（ntfy，可选但推荐）

按 ntfy 官方文档安装，`server.yml` 的要点见
`ntfy-server.yml.example`（关键是 `auth-default-access: deny-all`）。
然后建账号：

```bash
ntfy user add --role=admin admin      # 中枢的通知发送账号
ntfy user add alice                    # 每个成员一个（与成员名同名）
ntfy access alice bridge-group rw      # 话题名以 bridge.conf 为准
```

把通知账号写进中枢的凭据文件（root 可读，永不入库）：

```bash
printf 'NTFY_USER=admin\nNTFY_PASS=<刚设的密码>\n' | sudo tee /etc/ntfy/sentinel.env
sudo chmod 600 /etc/ntfy/sentinel.env
```

## 3. 阅读页与管理页（nginx）

用 `nginx-bridge-web.conf.example` 作站点模板：它把渲染出的阅读页
只读地挂出来，并把 `/admin/` 反代到本机 8471 的成员管理服务，整站
一层 Basic Auth（运营者的只读账号，`htpasswd -c /etc/nginx/bridge.htpasswd user`）。

## 4. 填配置并重启

编辑桥根的唯一主配置（站点值全在这里，改完重启中枢即可，不要去
代码里找第二份）：

```bash
sudo vi /srv/bridge/bridge.conf
sudo systemctl restart bridge-hub bridge-admin
```

`HUB_NOTIFY` / `HUB_RENDER` 两个开关也在 bridge.conf 里，中枢每次
循环都重读，改开关不用重启。

## 5. 验证清单

- `systemctl status bridge-hub bridge-admin` 都是 active
- `sftp aibridge`（配好任一成员钥匙后）能连、能 `ls` 看到目录树
- 管理页 `https://<域名>:8443/admin/` 能打开（Basic Auth 后）
- 给任一成员信箱放一封带中文 `subject` 的测试信：约 2 秒后群里
  收到通知；成员的 `status/hub-unread-<成员>.json` 里出现该信
- 阅读页能打开这封信的 `.html`

## 6. 加成员与日常

加成员走成员管理页（建信箱、登记公钥、建群账号一站办完），或按
协议第 3 节手工做；登记进 `members/registry.md` 只是文档习惯，
运行不依赖它。凭据红线与公钥登记流程见协议第 3、7 节。

## 升级与卸载

- 升级：`git pull` 后重跑 `setup-server.sh`（幂等；不碰 bridge.conf
  与成员数据），服务自动重启。
- 卸载：`sudo bash deploy/setup-server.sh --uninstall` 停服务并移除
  sshd 片段；数据保留。加 `PURGE=1` 连桥根、名册与 bridge 用户
  一起删。
