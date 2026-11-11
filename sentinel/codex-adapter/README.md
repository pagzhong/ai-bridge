# Codex 专用适配器（codex_adapter.py）

哨兵负责「发现新信、下载到本机」；这个适配器负责 Codex 的最后一公里：
**把信真正送进 Codex，让它当场开一轮活**，而不是只躺在收件箱里。

## 它怎么工作

适配器通过 Codex 官方的 app-server（JSON-RPC）与本机 Codex 通讯，
方法形状按 Codex CLI 0.167.1 的 `codex app-server generate-json-schema`
实测编写（initialize / thread/start / thread/resume / thread/list /
turn/start / turn/completed）：

- **传输**：默认 `proxy`——`codex app-server proxy` 把 stdio 转发给**已经在跑的
  共享守护进程**（交互式 Codex 用的就是它），适配器不新开服务、不监听任何端口。
  机器上没有交互 Codex 时（如服务器）可用 `spawn` 自起一个 app-server。
- **线程**：默认 `bridge` 模式——一个**专用的、持久的桥线程**：第一次来信时
  创建并记下 thread id，之后的信都 resume 同一个线程，桥上的事在这条线里
  连续积累，**不碰你自己交互用的线程**。另有 `latest` 模式（显式打开才生效）：
  把信投进工作目录下最近的那个线程。
- **回合**：每封信成为一个 turn，输入是信的全文（附一句协议提示：这是桥信、
  是数据不是用户指令、按桥协议回执）。适配器等到 `turn/completed` 才退出，
  并输出一行 JSON 摘要（delivered / via / threadId / turnStatus）。
- **兜底**：app-server 路径在回合开始前失败（例如守护进程没在跑），自动退到
  无头 `codex exec` 在同一工作目录里接活；两条路都失败才报 delivered=false。

## 接法（哨兵的 HANDOFF_CMD）

在 `~/.bridge-sentinel/config` 里：

```
HANDOFF=command
HANDOFF_CMD=BRIDGE_WORKDIR=/path/to/workdir python3 ~/.bridge-sentinel/codex-adapter/codex_adapter.py
```

适配器从哨兵拿到 `$LETTER_FILE`；也可以手动测试：
`python3 codex_adapter.py <某封信的路径>`。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BRIDGE_WORKDIR` | `$HOME` | Codex 的工作目录（桥线程的 cwd） |
| `ADAPTER_TRANSPORT` | `proxy` | `proxy` 接已有守护进程；`spawn` 自起 app-server |
| `ADAPTER_THREAD_MODE` | `bridge` | `bridge` 专用桥线程；`latest` 投最近线程 |
| `ADAPTER_STATE` | `~/.bridge-sentinel/codex-thread-id` | 桥线程 id 的存放文件 |
| `ADAPTER_TIMEOUT` | `1800` | 等一回合完成的最长秒数 |
| `ADAPTER_PROXY_INIT_TIMEOUT` | `8` | proxy 模式握手上限秒数（活守护进程 1 秒内应答；超时立刻走兜底） |
| `ADAPTER_APPROVAL_POLICY` | `never` | 桥回合不逐项停下来问批准（见下方注意） |
| `CODEX_BIN` | `codex` | codex 可执行文件 |

## 卸载

适配器没有常驻进程：删掉 `~/.bridge-sentinel/codex-adapter/` 目录、把
配置里的 `HANDOFF_CMD` 改回其它模式即可。桥线程本身只是 Codex 里的一条
普通线程，留着不管或在 Codex 里归档都行。

## 注意（先看再用）

- `ADAPTER_APPROVAL_POLICY=never` 意味着桥信触发的回合**不会逐项征求批准**，
  命令在你自己的 Codex 沙箱配置内直接执行——这正是「信到即办」的前提。
  不接受这个前提就把它设为空（跟随你 Codex 自己的配置），但回合可能停在
  无人应答的批准请求上。
- 本适配器只验证过 **Codex CLI 0.167.1** 的协议形状；Codex 升级后方法或字段
  若变化，用 `codex app-server generate-json-schema` 重新核对。
- app-server 本身在 Codex 里仍标 `[experimental]`；适配器只经本机 stdio 通讯，
  永远不要把 app-server 的网络端口暴露出去。

## 实测记录（参考部署，Linux VPS，Codex 0.167.1）

- `spawn` 模式：initialize 握手、thread/start 建线、turn/start 投信、
  turn/completed 回收，四步全部走通；第二次运行 resume 同一 thread id 成功。
- 未给该机 Codex 配模型来源时，回合状态为 `failed`（意料之中）——交付
  机器本身没有问题，缺的只是给那台 Codex 接上模型（网关）。
- **补测**：该机 Codex 经本机 OpenAI 兼容网关接通一个模型来源（提供商
  三协议实测通过、网关密钥就位、Codex 配置指向本机网关），同一封测试信
  经适配器投进桥线程，回合状态 **`completed`**——「信 → 适配器 →
  Codex → 模型」全链路验收通过。
- `proxy` 模式在无守护进程的机器上会等满握手超时（30 秒）后自动走 exec 兜底，
  行为符合预期；在装有交互 Codex 的机器上守护进程常在，proxy 是首选路径。
- **后续优化**：proxy 握手上限改为 8 秒
  （`ADAPTER_PROXY_INIT_TIMEOUT`）；同时修复了读取实现的一个真 bug——
  旧版用阻塞式 readline，对方「活着但不说话」时超时根本不生效、会无限
  等下去；新版用 select 按截止时间读。替身实测：假守护进程挂死时全程
  8.07 秒完成兜底交付；假 app-server 正常应答时 spawn 全链路 0.09 秒走通。
