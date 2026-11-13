# Windows 引擎可行性：iOS 17+/27 改定位的落地路线

> 任务：task-5 / 调研人 win-engine｜日期：2026-11-13｜结论已对齐 Lead 的 Windows 版实现
> 版本基线：**pymobiledevice3 11.23.0**（仓库 `.venv` 内实测源码）、**go-ios v1.3.2**（release 包 + 源码实测）
> 范围：本文件只做决策与证据；代码改动在 `locpilot/engine/`（Lead 负责），本任务只新增本文件与 `windows/probe/`。
> 验证边界：开发机是 macOS，**Windows 行为无法在本机执行**；所有未经真机 Windows 验证的条目都标「待 Windows 验证」，并给出验证命令。

---

## 0. 结论（先看这个）

### 0.1 一句话

**Windows 默认引擎仍是 pymobiledevice3，走无管理员的进程内 userspace 隧道；go-ios v1.3.2 只作降级引擎。** 内核隧道（tunneld）需要管理员，只在 iOS 17.0–17.3 或排障时使用。

### 0.2 用户要的最终回答表

| 问题 | 回答 |
|---|---|
| iOS 17+（含 iOS 27）在 Windows 上改定位走哪条路 | **pymobiledevice3 11.23.0 + 进程内 worker + iOS 17.4+ 原生支持的核心路径**：`usbmux(127.0.0.1:27015)` → lockdown → `UserspaceRsdTunnel`（纯 Python 用户态网络栈，无 kernel TUN）→ `auto_mount`（iOS 27 = Cryptex1 DDI，经 RSD/cryptexd）→ DVT `LocationSimulation` |
| 需要什么权限 | **不需要管理员**（默认路径与 go-ios 降级路径都不需要）。只有内核隧道 `pymobiledevice3 remote tunneld` / `lockdown start-tunnel` 需要「以管理员身份运行」；iOS 17.0–17.3 在 Windows 上被迫走这条（官方支持矩阵明说） |
| 失败时 App 该显示什么 | 见 §6「失败文案映射表」：每种失败一个中文标题 + 一句人话 + 一个主按钮（重试/打开帮助/以管理员重启 App/切兼容引擎）；英文原文只放折叠的技术细节 |

### 0.3 决策表

| 项 | 决定 | 依据 |
|---|---|---|
| Windows 默认引擎 | `pymobiledevice3`（进程内 worker，`LOCPILOT_PMD3_INPROC=1` 或 frozen+Windows 自动启用） | 官方支持矩阵 Windows+iOS 17.4+ = Supported (no root)；现有引擎抽象与 worker 协议可直接复用；持久 DTX 会话适合 1Hz 回放 |
| 降级引擎 | `go-ios 1.3.2` 的 `ios tunnel start --userspace` + `ios setlocation` | 单个 exe、MIT、--userspace 不需要管理员；但一个坐标一个进程且阻塞到 SIGINT，只适合单点/≤1Hz |
| 不采用的方案 | go-ios 内核隧道（要 wintun.dll + 管理员）；WSL2 作为主路径；把 go-ios 当默认 | 见 §2.2 / §3 |
| 是否要求 `pymobiledevice3` 命令行在 PATH | **不要求**。Windows 打包版没有 CLI 回退，只依赖 import 进来的 Python 包 | `PyMobileDevice3Engine.availability()` 进程内分支只检查 `import pymobiledevice3` |
| App 需要做什么 | ① 打包内置 pymobiledevice3 ② 冻结环境启用进程内 worker ③ 首次运行引导（iTunes/信任/开发者模式/DDI）④ 按 §6 映射错误 ⑤ 用 §7 探针验收 | 见 §4 / §5 |

---

## 1. 默认路线：pymobiledevice3 在 Windows 上（一手证据）

### 1.1 官方支持矩阵：Windows + iOS 17.4+ 本来就被支持，且无 root

pymobiledevice3 官方文档「iOS 17+ tunnels」的 Support Notes 表格（<https://doronz88.github.io/pymobiledevice3/latest/guides/ios17-tunnels/>）：

| Host OS | iOS 17.0-17.3 | iOS 17.4+ |
| --- | --- | --- |
| macOS | Supported (no root, native tunnel) | Supported (no root) |
| **Windows** | **Needs tunneld (Administrator) + a third-party driver** | **Supported (no root)** |
| Linux | Needs tunneld (root) | Supported (no root) |

同一页写明默认行为：「On iOS 17.4 and later this works the same on macOS, Windows and Linux」，即命令会自动建立 **in-process userspace tunnel**，no root / no admin。用户的 iPhone 18 Pro 是 iOS 27，落在 iOS 17.4+，所以 Windows 默认路径成立。

对应实现（本机 `.venv` 的 11.23.0 源码）：

- 自动选择隧道的默认偏好是 userspace：`.venv/lib/python3.9/site-packages/pymobiledevice3/cli/cli_common.py:421` `default_transport_preference()` → `return "userspace"`（同文件 :442）；只有它抛 `UserspaceTunnelUnavailableError`（iOS 17.0–17.3 没有 CoreDeviceProxy）才降到管理员 `tunneld`（同文件 :597 / :612）。
- userspace 隧道不需要 root：`.venv/.../pymobiledevice3/remote/userspace_tunnel.py` 用纯 Python 的 PyTCP 栈替代 kernel TUN；文档字符串明确「no root/admin either, on every host」。
- 代码对 Windows 有专门适配：`remote/userspace_tunnel.py:220`「Unix uses an AF_UNIX SOCK_DGRAM socketpair; **Windows (no such socketpair)** uses a connected …」；`remote/userspace_tunnel.py:444` relay 在「AF_UNIX is unavailable (e.g. **Windows**)」时退回 loopback TCP；`pmd_pytcp/lib/io_backend.py:54` 注明「works on **proactor** loops」覆盖 Windows。

### 1.2 Windows 的 usbmux 依赖：Apple Mobile Device Service（不是「Python 自带驱动」）

Windows 上 usbmuxd 是 Apple 的服务，pymobiledevice3 通过 **loopback TCP 127.0.0.1:27015** 连它：

- 源码：`.venv/.../pymobiledevice3/usbmux.py:39` `ITUNES_HOST = ("127.0.0.1", 27015)`；`osu/win_util.py:79` `usbmux_address` 返回该地址。
- 官方文档：「On Windows usbmuxd 的 socket is loopback TCP (127.0.0.1:27015) instead of a unix socket」——<https://doronz88.github.io/pymobiledevice3/latest/guides/network-stacks/>。
- 服务由 **iTunes 经典安装包** 提供；Microsoft Store 的 Apple Devices 服务能走 USB，但**不发现 Wi-Fi 设备**（pmd3 issue #1968，官方安装文档也写了）：<https://doronz88.github.io/pymobiledevice3/latest/installation/>、<https://github.com/doronz88/pymobiledevice3/issues/1968>。
- 源码还会区分两种安装形态：`osu/win_util.py:25` 读注册表 `SYSTEM\CurrentControlSet\Services\Apple Mobile Device Service` 的 ImagePath，路径含 `windowsapps`（:31）就判定为 Store 版并标记 `discovers_over_wifi=False`。

**结论**：Windows 上「能看到 iPhone」= Apple Mobile Device Service 在跑 + 数据线/信任到位。这既是 pmd3 的前提，也是 go-ios 的前提（go-ios 同样连 `tcp://127.0.0.1:27015`，见 `ios/usbmuxconnection.go:52`）。

### 1.3 什么时候才需要管理员（别把权限要求说大）

只有「内核隧道」需要管理员，默认 userspace 路径不需要：

- `pymobiledevice3 remote tunneld`：`cli/remote.py:417` `if not OSUTILS.is_admin: raise AccessDeniedError()`。
- `pymobiledevice3 lockdown start-tunnel`：`cli/lockdown.py:206` 装饰器 `@sudo_required`。
- Windows 上 `is_admin` 用 `win32security.CheckTokenMembership`（`osu/win_util.py:61`）；报错文案是 `This command requires admin privileges. Consider retrying with "run-as administrator".`（`osu/win_util.py:91`，`__main__.py:448` 打印）。
- 官方 troubleshooting 也写明：「For developer commands, prefer the default no-root userspace tunnel instead — it requires no privileges at all.」<https://doronz88.github.io/pymobiledevice3/latest/guides/troubleshooting/>

> Windows 没有 sudo：文档明确「run the command from a terminal opened with **Run as administrator**」。

### 1.4 仓库实现：为什么必须进程内 worker（Lead 已实现）

**问题**：`windows/app.py` 是「pywebview + 进程内 Python 后端」，用 PyInstaller 冻结。PyInstaller 冻结后 `sys.executable` 就是 `LocPilot.exe` 自己，而原 `locpilot/engine/pmd3.py` 用 `[python, pmd3_worker.py]` 起子进程；冻结版照此执行会把 App 自己当解释器再启动一次 → worker 路径在 Windows 打包版必然坏。

**同时**：Windows 打包版没有 `pymobiledevice3` 命令行在 PATH 上，`PyMobileDevice3Engine` 的「无 worker 时退回单发 CLI」也走不通（`_cli_set` 依赖 `binary()` 找到可执行文件）。**所以进程内 worker 是唯一可用路径。**

**已实现方案**（`locpilot/engine/pmd3.py` + `pmd3_worker.py`，Lead 负责；截至本文件编写时为未提交状态）：

1. `inproc_enabled()`：`LOCPILOT_PMD3_INPROC=1` 或 (`sys.frozen` 且 `os.name == "nt"`) 时启用。
2. `_InProcWorker`：对外接口与 `_WorkerProc` 一致（`start/request/alive/stop/stderr_tail`），内部复用 `pmd3_worker.handle(bridge, session, payload)`，**worker 协议一字不差**。
3. `availability()`：进程内模式只要求 `import pymobiledevice3` 成功，不要求命令行。
4. `list_devices()`：进程内走 `usbmux.list_devices()`（11.23.0 上是 async），映射成 `DeviceInfo`；设备名/iOS 版本在 `open()` 后由 lockdown 补全（best-effort）。
5. `describe()` 增加 `worker: "inproc"|"subprocess"` 与 `inproc_enabled`，日志可区分。
6. macOS 路径完全不变（仍子进程 worker + CLI 挂载）。

### 1.5 进程内模式的三条实现注意（写进验收，不当 bug 看）

> 这三条是「实现选择」：换了运行形态就必须配套，不是历史缺陷。

**① 挂载顺序：先建 RSD，再 mount（Cryptex1 约束）**

iOS 27 的 DDI 是 Cryptex1，`auto_mount()` 在 27+ 只能通过 cryptexd over RSD 安装：`.venv/.../services/mobile_image_mounter.py:340` `CRYPTEX_IMAGE_MIN_VERSION = Version("27.0")`；同文件 :661–663 在纯 lockdown 且版本 ≥ 27 时直接 `raise RSDRequiredError`。所以进程内不能沿用「先挂 DDI 再建隧道」的旧顺序，`Session.open()` 已改为：iOS 17+ → 先 `_rsd_provider()` 拿 RSD → `_auto_mount(provider)` → 再开 DVT。`_auto_mount` 在进程内走库调用 `mobile_image_mounter.auto_mount(provider)`（不再 `sys.executable -m pymobiledevice3`），`provider=None` 时返回跳过说明、不抛错（当前两条分支都会传 provider，`None` 分支只是防御性兜底；挂载始终 best-effort，与旧行为一致）。

**② 隧道句柄：Session 持有并在 close 时 `aclose()`**

`UserspaceRsdTunnel` 的库文档写明「Embedders should use `UserspaceRsdTunnel` directly — it is a closeable handle」，且 **one tunnel per process**（PyTCP 是进程级单例）。原来的 CLI 便利函数 `establish_userspace_rsd()` 把隧道放模块全局 `_cli_tunnel`、只在进程退出时关；子进程 worker 每次 close 杀进程掩盖了这个问题，进程内复用就会撞 one-tunnel-per-process。实现已改为进程内 `UserspaceRsdTunnel(serial=...)` → `aopen()` → 句柄存 `Session._tunnel`，`Session.close()` 里 `await self._tunnel.aclose()`。

**③ Windows 进程内用 Selector 事件循环**

`Bridge` 在进程内 + Windows 时先 `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())` 再 `new_event_loop()`。理由：Proactor 不支持 `add_reader`（aioquic/QUIC 的 Wi-Fi RemotePairing 路径需要），而改成库调用后进程内不再需要 `create_subprocess`（Proactor 的唯一优势），所以 selector 安全。pmd3 CLI 自己在 Windows 上也是这么设的（`.venv/.../pymobiledevice3/__main__.py:98`）。

### 1.6 iOS 27 的 DDI 与缓存路径

- 官方 troubleshooting：「From iOS 27 the image is the Cryptex1 DDI, installed over `cryptexd` just like `pymobiledevice3 cryptex auto-install`」；缓存目录 Windows 是 `%USERPROFILE%\.pymobiledevice3`。<https://doronz88.github.io/pymobiledevice3/latest/guides/troubleshooting/>
- 首次挂载需要联网下载（Apple CDN）；失败不影响「枚举/配对」，但会影响 DVT 相关开发者服务，App 要能提示「开发者镜像未就绪」。
- 配对记录（lockdown）在 Windows 由 Apple 服务维护（`%ALLUSERSPROFILE%\Apple\Lockdown`，源码 `osu/win_util.py:96`）；开发者模式的开启可用 `pymobiledevice3 amfi enable-developer-mode`（走 lockdown，不需要 Mac）。

### 1.7 打包前提（Windows 版）

| 项 | 要求 | 说明 |
|---|---|---|
| Python | 3.9+（本机 3.9.6 已跑通 pmd3 11.23.0；Windows 推荐 3.11/3.12） | pmd3 `Requires-Python >=3.9` |
| 依赖 | `pymobiledevice3>=11.23.0` | `windows/requirements.txt` 需加；pywin32/av 由环境标记自动装（`METADATA`：`pywin32; platform_system == "Windows"`） |
| PyInstaller | `--collect-all pymobiledevice3` + hidden imports | pmd3 依赖多（construct/qh3/pytun_pmd3/pmd_pytcp…），漏收集会在 `availability()` 直接返回 false |
| 许可 | 项目 GPL-3.0 与 pmd3 GPL-3.0-or-later 兼容 | 打包分发时保留许可与 NOTICE（当前仓库 LICENSE 已是 GPL-3.0） |
| 缓存/状态 | `%USERPROFILE%\.pymobiledevice3`（pmd3）、`%LOCALAPPDATA%\LocPilot`（App） | App 已用 `LOCALAPPDATA` 做每用户状态目录（`windows/app.py:37`） |

---

## 2. 降级路线：go-ios v1.3.2 在 Windows 上

### 2.1 二进制与可用性

- 官方 release 有 `go-ios-win.zip`（v1.3.2，<https://github.com/danielpaulus/go-ios/releases/tag/v1.3.2>）。本机实测下载：**8,824,605 bytes，sha256 `939c6bcaafed183a92afb9f79cc11b1f935fa6389bfc94d3902e3f52c4dff3fe`，zip 内只有一个 `ios.exe`（23,691,264 bytes，PE32+ x86-64）**。
- **userspace 模式不需要管理员**：源码 `cmd_tunnel.go:44-50` 只有 `start && !--userspace` 才 `tunnel.CheckPermissions()`；Windows 的权限探测是打开 `\\.\PHYSICALDRIVE0`（`ios/tunnel/tunnel_windows.go:24-28`）。同文件 :114 的错误文案直接写「Run from an administrator shell, or use `ios tunnel start --userspace` which does not need admin rights」。
- **内核模式**才要管理员 + wintun.dll：README 原文「For iOS 17+ devices you need to run `sudo ios tunnel start` … To make this work on Windows, download the latest wintun.dll … copy it to `C:/Windows/system32`」；代码在 `ios/tunnel/wintun_windows.go` + `setupTunnelInterface`（:114 附近）。**我们不采用内核模式。**
- `--userspace` 的实现是 gvisor netstack（`ios/tunnel/userspace_tunnel.go`，无 windows 构建标签），客户端通过本地 TCP 代理端口转 RSD（`ios/connect.go:349-352` 的 `ConnectUserSpaceTunnel`），因此跨平台可用。

### 2.2 为什么只作降级（硬限制）

1. **一个坐标一个进程，且阻塞到 SIGINT**：`main.go:1373-1387` `startLocationSimulation()` 设完坐标后 `<-c` 等 `os.Interrupt`，退出时 `defer stopLocationSimulation()` 才 revert。「先起新进程再杀旧进程」会把刚设的坐标清掉，必须严格「先停旧的、再起新的」；1Hz 路线回放每个 tick 都有进程启动 + DTX 握手开销（macOS 实测进程启动 ≈150ms，Windows 未测）。
2. **清空定位只能靠让进程退出**：`resetlocation`/`setlocationgpx` 在 v1.3.2 没有 RSD 分支（`cmd_device_location.go` 直接走老 lockdown 服务），iOS 17+ 不可靠；官方 e2e 也是给 `setlocation` 发 SIGINT。Windows 上 Python 只能对 `CREATE_NEW_PROCESS_GROUP` 子进程发 `CTRL_BREAK_EVENT`；**从无控制台的窗口进程（冻结 App）发送不保证成功**，失败只能强杀，可能残留模拟定位。
3. **上游没有 Windows 真机 CI**：`.github/workflows/real-device.yml` 的真机矩阵只有 `macOS` 与 `Linux`（第 25-33 行），Windows 只做「binary 能跑」的 smoke（`release.yml`/`release-canary.yml` 的 windows-latest）。所以 Windows + 真机 + userspace 隧道这条组合**没有上游覆盖**，只能我们自己验。
4. **agent 要额外管理**：`ios` 子命令在 iOS 17+ 需要先跑 `ios tunnel start --userspace` 常驻 agent，客户端靠 HTTP 隧道信息发现（默认端口 `ios/connect.go:388` = **60105**；但 `main.go:197` 帮助文本仍写 28100，是过期文档，集成时必须显式传 `--tunnel-info-port`）。

### 2.3 若真要集成 go-ios，必须按这个姿势

| 项 | 做法 | 证据/原因 |
|---|---|---|
| 启动 agent | 显式 `ios.exe tunnel start --userspace --pair-record-path <可写目录> --tunnel-info-port 60105` | 不要依赖 `ENABLE_GO_IOS_AGENT=user` 自动拉起：`cli_setup.go:84` 对所有命令生效，agent 子进程继承环境变量后可能递归拉 agent（竞态）；显式启动可控 |
| 配对身份目录 | 显式传 `--pair-record-path`（如 `%LOCALAPPDATA%\LocPilot\goios-pair`） | 默认是当前工作目录 `.`（`cmd_tunnel.go` 帮助：「If nothing is specified, the current dir is used」）；App 的 CWD 可能在 Program Files，不可写 |
| 端口 | 客户端命令统一带 `--tunnel-info-port 60105` | 帮助文本默认值与实现不一致（28100 vs 60105），不显式传会连不上 agent |
| 设备信息 | `ios list --details`（返回 `{"deviceList":[{"Udid","ProductName","ProductType","ProductVersion","ConnectionType"}]}`，`main.go:1446` `detailsEntry`） | 裸 `ios list` 只给 UDID 字符串数组，拿不到 iOS 版本 |
| 设置坐标 | `ios setlocation --lat= --lon= --udid= --tunnel-info-port=60105`，起在 **新进程组**，存活≥3s 且 stderr 无 ERROR 才算成功 | iOS 17+ 该命令阻塞到 SIGINT，进程提前退出就是失败 |
| 清空 | 给 setlocation 发 SIGINT（POSIX）/**CTRL_BREAK_EVENT**（Windows，需 CREATE_NEW_PROCESS_GROUP）；再不行只能强杀并提示用户重启手机 | 没有可用的 iOS 17+ `resetlocation` |
| 频率 | 只支持单点传送 / ≤1Hz；摇杆和高频回放不要走它 | 每坐标新进程 + DTX 握手 |

---

## 3. 两路对比（只列会影响决策的维度）

| 维度 | pymobiledevice3 11.23.0（默认） | go-ios v1.3.2（降级） |
|---|---|---|
| iOS 17.4+ / iOS 27 定位 | 支持（无 root userspace 隧道 + DVT，官方支持矩阵） | 支持（userspace agent + DTX），但上游无 Windows 真机 CI |
| 管理员 | **不需要**（内核 tunneld 才需要） | userspace 不需要；内核模式需要 |
| 额外安装 | Python 运行时 + pmd3 依赖（打进 App） | 单个 `ios.exe`（23.7MB），零依赖 |
| 常驻会话 / 高频 | **有**（进程内 worker，一次 RSD+DTX，之后每坐标 <10ms 级） | 无（一个坐标一个新进程，阻塞到 SIGINT，≈150ms 启动 + 握手） |
| 清空定位 | `LocationSimulation.clear()`（同一 DTX 会话内） | 让 setlocation 进程退出（Ctrl-C/Ctrl-Break），Windows 下不保证送达 |
| 失败可诊断性 | 有异常类与中文可映射的错误（见 §6） | stderr JSON + WARN 噪音，需按退出码/关键词判断，存在静默降级风险 |
| 许可 | GPL-3.0-or-later（项目本身 GPL-3.0，兼容） | MIT（无义务，可分发） |
| 分发体积 | Python 运行时 + 依赖（pmd3 包本体 ~4.6MB，整 venv 100MB+） | 23.7MB zip 解压后 23.7MB（仅 exe） |
| 上游 Windows 覆盖 | 官方文档明确支持 Windows 17.4+；CI/文档活跃 | 只有 Windows 构建 smoke，无真机 CI |
| 结论 | **默认引擎** | **降级引擎**（pmd3 不可用时） |

---

## 4. 决策与降级策略（App 里的状态机）

### 4.1 连接流程（自上而下，先默认后降级）

```text
启动 / 点「连接真机」
  │
  ├─ 1. pymobiledevice3 进程内 worker 可用吗？
  │      availability(): import pymobiledevice3 成功 → 可用
  │      └─ 否：引擎缺失文案（ENGINE_MISSING）→ 有 ios.exe 则提示切兼容引擎，否则提示重装
  │
  ├─ 2. 枚举设备（usbmux.list_devices）
  │      ├─ 抛连接错误 → AMDS_DOWN（Apple 移动设备服务未运行）
  │      └─ 0 台 → NO_DEVICE（线/解锁/信任）；不去猜、不静默切 mock（沿用现有 allow_mock=False 原则）
  │
  ├─ 3. open(): lockdown → RSD 隧道 → DDI → DVT
  │      ├─ 开发者模式未开 → DEV_MODE_OFF（可自动执行 amfi enable-developer-mode 后引导重启）
  │      ├─ DDI 挂载失败 → DDI_NOT_READY（首次需联网；可重试）
  │      ├─ UserspaceTunnelUnavailable / iOS 17.0-17.3 → IOS_TOO_OLD_NOADMIN（引导管理员 tunneld 或升级 iOS）
  │      ├─ 隧道超时/其他 → TUNNEL_FAILED（重试；有 ios.exe 时提供「切兼容引擎」）
  │      └─ 成功 → 已连接，可 teleport / 回放
  │
  ├─ 4. 运行期：设备拔出 / 锁屏 / 隧道断开
  │      pmd3 的 transport watcher 会拆隧道（userspace_tunnel.py「transport closed (device disconnected?)」）
  │      → DEVICE_DISCONNECTED：提示重连，恢复后自动重开（不要在断开状态假装成功）
  │
  └─ 5. 降级引擎（仅在用户点「兼容模式」或 pmd3 引擎缺失时）：
        启 agent（--userspace，显式端口/配对目录）→ tunnel ls → setlocation
        失败按 §6 的 GOIOS_* 文案展示；清空用 Ctrl-Break，失败则提示重启手机
```

### 4.2 引擎选择规则（落到代码）

- Windows 版由 `windows/app.py` 在 `prepare_env()` 里设置 `LOCPILOT_PMD3_INPROC=1`（或在构建时依赖 frozen+nt 自动判据），**不要**改 macOS 的子进程路径。
- go-ios 只在以下条件才建议用户切换：① `availability()` 返回引擎缺失；② 用户显式选「兼容模式」；③ iOS 17.0–17.3 且用户拒绝管理员 tunneld。没有 `ios.exe` 时不要展示「切兼容引擎」按钮。
- **绝不静默降级到 mock**：现有 `auto_select(allow_mock=False)` 的原则在 Windows 同样适用（用户会以为连上了真机）。

### 4.3 App 集成要点清单（Windows）

| # | 事项 | 位置 | 验收 |
|---|---|---|---|
| 1 | 依赖加 `pymobiledevice3>=11.23.0`，PyInstaller `--collect-all pymobiledevice3` | `windows/requirements.txt`、打包脚本 | 冻结包里 `import pymobiledevice3` 成功；`availability()` 返回 true |
| 2 | 冻结/Windows 启用进程内 worker（`LOCPILOT_PMD3_INPROC=1`） | `windows/app.py` prepare_env | `describe()` 输出 `worker: "inproc"`，连接真机可 set/clear |
| 3 | 进程内「先 RSD 后 mount」、Session 持隧道句柄、Selector 事件循环 | `locpilot/engine/pmd3_worker.py`（Lead） | 隧道/DDI/重连/拔线 4 项探针通过 |
| 4 | 首次运行引导：iTunes/信任/开发者模式/DDI | UI 连接面板 | 四个状态各有独立文案与下一步按钮（§6） |
| 5 | 错误映射：底层异常 → 错误码 → 中文文案 | 后端 API + UI | §6 每行都有对应展示；英文原文只进折叠区 |
| 6 | 日志：`%LOCALAPPDATA%\LocPilot\locpilot.log`，错误页给路径 | `windows/app.py` 已重定向 stdio | 用户能一键复制诊断信息 |
| 7 | 不要求 `pymobiledevice3` CLI 在 PATH | 引擎 availability 分支 | PATH 里没有 CLI 也能连真机 |

---

## 5. 权限与前置清单（给用户看的「检查清单」）

| 项 | 要求 | 谁来做 | 失败时 App 文案 |
|---|---|---|---|
| Apple 移动设备服务 | 已安装并在运行（iTunes 经典版） | 用户安装，App 检测 | AMDS_DOWN |
| usbmux 端口 | `127.0.0.1:27015` 可连 | 服务启动即满足 | AMDS_DOWN |
| 数据线 | 能传数据（非纯充电线） | 用户 | NO_DEVICE |
| iPhone 解锁 + 信任 | 首次弹窗点「信任」 | 用户 | TRUST_REQUIRED / NO_DEVICE |
| 开发者模式 | 设置 → 隐私与安全性 → 开发者模式（开完重启） | 用户；App 可一键执行 `amfi enable-developer-mode` 后引导重启 | DEV_MODE_OFF |
| 开发者镜像 DDI | iOS 27 = Cryptex1，经 RSD 安装，首次联网下载 | pmd3 自动挂载；App 可重试 | DDI_NOT_READY |
| 管理员 | **默认路径不需要** | — | 仅内核 tunneld 降级时 ADMIN_REQUIRED |
| 网络 | 首次 DDI 下载、地图瓦片 | 用户网络 | DDI_NOT_READY / 地图空但定位可用 |

---

## 6. 失败文案映射表（可直接照抄进 UI）

### 6.1 展示规范

- **主文案中文、一句话说清「发生了什么 + 我该做什么」**；英文异常原文只放「技术细节」折叠区。
- 每个错误一个稳定错误码（后端 API 返回，UI 按码出文案），便于用户截图反馈。
- 红色横幅只用于阻塞项（连不上/改不了）；WARN 用黄色提示条（例如开发模式未开但用户点了连接前先提示）。
- 主按钮最多两个：主操作（重试/打开帮助/以管理员重启）和次操作（切换引擎/稍后）。
- 日志路径固定：`%LOCALAPPDATA%\LocPilot\locpilot.log`；「复制诊断信息」= 错误码 + 设备型号/iOS 版本 + 日志末尾 50 行。

### 6.2 默认引擎（pymobiledevice3）

| 错误码 | 触发（一手错误类/原文） | App 标题 | 正文（可直接用） | 主按钮 | 次按钮 |
|---|---|---|---|---|---|
| `ENGINE_MISSING` | `availability()` 进程内分支失败：`进程内 worker 不可用（pymobiledevice3 未打包）: ...` | 定位引擎不可用 | 安装包缺少定位引擎组件（pymobiledevice3），无法连接真机。请重新安装 LocPilot Windows 版。 | 打开下载页 | 用演示模式 |
| `AMDS_DOWN` | `ConnectionFailedToUsbmuxdError`；WinError 10061 / `连接尝试失败`；CLI 文案 `Failed to connect to usbmuxd socket. Make sure it is running.`；`usbmux.list_devices` 抛错 | Apple 的设备服务未运行 | Windows 需要 Apple Mobile Device Service 才能看到 iPhone。请安装 Apple 官方 iTunes（经典版），并在「服务」中启动 Apple Mobile Device Service。 | 重试 | 打开帮助 |
| `NO_DEVICE` | `DeviceNotFoundError`：`Device not found: usbmux has no device matching udid ...`；`NoDeviceConnectedError`；枚举为空 | 没有发现 iPhone | 请用能传数据的数据线连接 iPhone，解锁屏幕；首次连接时在手机上点「信任」。 | 重试 | 打开帮助 |
| `TRUST_REQUIRED` | `PasswordRequiredError` / `NotPairedError` / 设备锁屏相关 | iPhone 需要解锁并信任本机 | 解锁 iPhone，并在「要信任此电脑吗？」弹窗点「信任」；若没有弹窗，拔插数据线重试。 | 重试 | 打开帮助 |
| `DEV_MODE_OFF` | 建议先主动查 `lockdown.get_developer_mode_status()`（源码 `lockdown.py:534`，读 `com.apple.security.mac.amfi/DeveloperModeStatus`）为 False；否则 `InvalidServiceError`（`No such service: com.apple.instruments.dtservicehub`）/ `DeveloperModeIsNotEnabledError` | 需要开启「开发者模式」 | 在 iPhone：设置 → 隐私与安全性 → 开发者模式 → 打开（会要求重启并输入锁屏密码）。开启后回到电脑点重试。 | 我已在手机上开启，重试 | 如何开启（引导） |
| `DDI_NOT_READY` | `DeveloperDiskImageNotFoundError` / `RSDRequiredError`（iOS 27 纯 lockdown 挂载）；`mount` 字段为 `auto-mount 失败: ...`；或 `InvalidServiceError` 且开发者模式已开（排除 DEV_MODE_OFF 后归到 DDI） | 开发者镜像未就绪 | 首次连接需要联网下载并安装开发者镜像（iOS 27 为 Cryptex1）。请保持网络畅通后重试；也可能需要先解锁 iPhone。 | 重新挂载（重试连接） | 查看日志 |
| `IOS_TOO_OLD_NOADMIN` | `UserspaceTunnelUnavailableError`（原文 `no-root userspace tunnel unavailable: the device has no CoreDeviceProxy service (needs iOS 17.4+) ...`）；或文档矩阵 iOS 17.0–17.3 | 该 iOS 版本在 Windows 上需要管理员隧道 | iOS 17.0–17.3 无法使用无管理员的定位隧道。请升级 iPhone 到 iOS 17.4+，或用管理员身份运行 tunneld（首次还需要第三方驱动）。 | 如何以管理员启动 | 稍后 |
| `ADMIN_REQUIRED` | `AccessDeniedError`；原文 `This command requires admin privileges. Consider retrying with "run-as administrator".` | 需要管理员权限 | 当前操作需要以管理员身份运行。普通改定位不需要管理员，这个提示通常只在启用「内核隧道」降级时出现。 | 以管理员身份重启 App | 取消 |
| `TUNNEL_FAILED` | `RuntimeError`：`iOS 17+ 需要 RSD 隧道，且自动建立失败(...)`；隧道建立超时 | 无法建立定位隧道 | 请重新插拔数据线、解锁 iPhone 后重试；若反复失败，可尝试「兼容模式」（go-ios）或查看日志。 | 重试 | 切换兼容引擎（有 ios.exe 时） |
| `DEVICE_DISCONNECTED` | 运行期 `userspace tunnel transport closed (device disconnected?)` / EOF / 读写错误 | iPhone 已断开 | 连接中断（拔线、锁屏或系统休眠），已停止模拟定位。请重新连接 iPhone；连接恢复后会自动重新建立会话。 | 重新连接 | 打开帮助 |

### 6.3 降级引擎（go-ios）

| 错误码 | 触发（一手信号） | App 标题 | 正文 | 主按钮 | 次按钮 |
|---|---|---|---|---|---|
| `GOIOS_MISSING` | 找不到 `ios.exe` | 兼容引擎未安装 | 未找到 go-ios 的 ios.exe。请下载 go-ios-win.zip 并解压，或改用默认引擎。 | 打开下载页 | 用默认引擎 |
| `GOIOS_AGENT_DOWN` | stderr WARN `go-ios agent is not running ...`；`ios tunnel ls` 退出码 1 且 `dial tcp 127.0.0.1:60105: connect: connection refused` | 兼容引擎的后台服务未启动 | 兼容引擎需要先启动后台隧道服务。 | 启动兼容引擎 | 用默认引擎重试 |
| `GOIOS_NO_DEVICE` | stderr `Device not found: ` + `no iOS devices are attached to this host` | 没有发现 iPhone | 请连接并解锁 iPhone、点信任；Windows 还需 Apple Mobile Device Service 在运行。 | 重试 | 打开帮助 |
| `GOIOS_ADMIN_REQUIRED` | `this program needs elevated privileges. Run as administrator.` / `this program needs root privileges. Run with sudo.` / 启动参数误加了非 `--userspace` | 兼容引擎需要管理员 | 兼容引擎被配置成「内核隧道」模式。LocPilot 的兼容模式固定使用 `--userspace`，无需管理员；请重置兼容模式配置后重试。 | 重置兼容模式 | 打开帮助 |
| `GOIOS_TUN_FAILED` | `Error creating TUN device:` / `wintun.dll` 相关（仅内核隧道模式） | 兼容引擎缺少组件 | 仅内核隧道模式需要 wintun.dll（放到 C:\Windows\System32）。LocPilot 不使用该模式，请改用 userspace 模式。 | 改用 userspace 模式 | 打开帮助 |
| `GOIOS_SET_EXITED` | `ios setlocation` 在 3–6s 内提前退出（非 0，或 iOS 17+ 下 rc=0 也不正常）；stderr 含 tunnel/RSD/DDI 关键词 | 兼容引擎设置定位失败 | 兼容引擎在 iOS 17+ 需要后台服务、开发者模式与开发者镜像都就绪。请先确认默认引擎能连接，再切兼容模式。 | 用默认引擎重试 | 查看日志 |
| `GOIOS_CLEAR_UNSAFE` | Ctrl-Break 未送达 → 强杀 setlocation 进程 | 定位可能未恢复 | 兼容引擎未能正常退出，模拟定位可能仍在生效。重启 iPhone 可恢复真实定位；建议改用默认引擎以避免此问题。 | 重启 iPhone 方法 | 重试清空 |

---

## 7. 探针与验收命令（Windows 上跑）

三条探针都在 `windows/probe/`，用法与输出判读见 [windows/probe/README.md](../windows/probe/README.md)。摘要：

| 探针 | 命令（仓库根目录） | 需要设备 | 退出码 |
|---|---|---|---|
| 主机就绪 | `python windows/probe/probe-windows.py`（可加 `--json`） | 否 | 0 就绪 / 1 有阻塞 / 2 非 Windows |
| 默认引擎端到端 | `python windows/probe/probe-device.py --lat 25.0339 --lon 121.5645 --hold 8` | 是 | 0 全绿 / 1 失败 / 2 前置不满足 |
| 降级引擎 | `python windows/probe/probe-goios.py --lat 25.0339 --lon 121.5645 --hold 8 --goios C:/tools/go-ios/ios.exe` | 是 | 0 全绿 / 1 失败 / 2 前置不满足 |

补充参数：`probe-device.py --self-check`（只验证运行时/导入/枚举）、`--clear-only`（只清空）、`--loop selector|default`（A/B 事件循环）；`probe-goios.py --self-check`、`--agent-only`、`--pair-record-path`、`--keep-agent`。

快速人工交叉验证（不依赖探针）：

```bat
python -m pymobiledevice3 doctor --json
python -m pymobiledevice3 usbmux list
python -m pymobiledevice3 developer dvt simulate-location set -- 25.0339 121.5645
python -m pymobiledevice3 developer dvt simulate-location clear
```

> 注：以上 CLI 写法只用于交叉验证；Windows App 运行时**不需要** CLI，走进程内 worker。

---

## 8. 待 Windows 验证清单（本机 macOS 无法验证）

Windows 主机（192.168.0.126）当前不可达，且本机没有 iPhone，所以下面每一项都必须到 Windows + 真机上跑。

| # | 待验证项 | 命令/操作 | 通过判据 |
|---|---|---|---|
| W1 | Apple Mobile Device Service 就绪、usbmux 27015 可连 | `python windows/probe/probe-windows.py` | `Apple Mobile Device Service`、`usbmux TCP 127.0.0.1:27015` 均为 PASS（0 设备也算通过） |
| W2 | userspace 隧道**无管理员**建立（iOS 17.4+） | 普通 PowerShell（非管理员）跑 `probe-device.py --lat ... --hold 5` | `[PASS] userspace 隧道（无管理员）` |
| W3 | iOS 27 Cryptex1 DDI 经 RSD 挂载 | 同上 | `[PASS] 开发者镜像 DDI`（首次需联网，可能较慢） |
| W4 | DVT 定位 set/clear 生效 | 同上；保持期间看 iPhone 自带地图 | `设置模拟定位`/`清空模拟定位` PASS，且地图目视确认；清空后回到真实位置 |
| W5 | 事件循环 A/B 无回归 | 分别加 `--loop default` 与 `--loop selector` | 两种都能 set PASS（实现已固定 selector，另一项用于排除环境差异） |
| W6 | 重连不撞 one-tunnel-per-process | App 里断开/连接 3 次；或连跑 3 次 `probe-device.py` | 每次成功，无 `a userspace tunnel is already active` |
| W7 | 拔线/锁屏后恢复 | 保持定位时拔线再插回 | App 出现 `DEVICE_DISCONNECTED` 文案，重连后自动恢复，不崩溃 |
| W8 | 1Hz 路线回放 60s 不丢点 | App 路线播放 60s（约 60 次 set） | 位置连续推进；结束后 `LocPilot.exe` 无多余进程、CPU 正常 |
| W9 | 冻结包中进程内 worker 生效 | `LocPilot.exe --selftest` 后连接真机；看日志/`/api/engines` | `describe()` 中 `worker: "inproc"`、`inproc_enabled: true`；连接/teleport 成功 |
| W10 | 降级引擎可用（可选） | `probe-goios.py --lat ... --hold 5` | agent/set/clear 全 PASS；Ctrl-Break 后手机地图恢复真实定位 |
| W11 | Wi-Fi 路径（可选） | 拔线后用 `python -m pymobiledevice3 lockdown wifi-connections on`（先 USB 一次）再连 | 经典 iTunes 环境下可发现；Store 版 Apple Devices 预期失败（pmd3 #1968） |
| W12 | 真机型号/iOS 版本记录 | `probe-windows.py --json` + `probe-device.py --self-check --json` | 输出里记录 iPhone 型号与 iOS 27.x，作为交付证据 |

---

## 9. 本机（macOS）已完成验证与原始输出

说明：以下验证的是**探针脚本自身可运行、pmd3 11.23.0 的 API 可用、go-ios 1.3.2 的 CLI 行为**；Windows 特有行为（Apple 服务、27015、Wintun、进程内冻结）仍待 W1–W12。

### 9.1 三条探针语法编译（Python 3.9.6，兼容用户环境的 3.9）

```console
$ cd /Users/seinding/workspace/locpilot
$ export PYTHONPYCACHEPREFIX=/tmp/win-engine/pycache   # 沙箱不能写 ~/Library/Caches
$ python3 -m py_compile windows/probe/probe-windows.py windows/probe/probe-device.py windows/probe/probe-goios.py
$ echo $?
0
```

### 9.2 主机探针（macOS 上预期 exit 2，证明非 Windows 会正确降级报告）

> 输出为节选（省略了部分 PASS/SKIP 行），汇总行与退出码是原始值。

```console
$ .venv/bin/python windows/probe/probe-windows.py ; echo EXIT=$?
[FAIL] 平台  macOS-27.2-arm64-arm-64bit / arm64 / Python 3.9.6
[PASS] pymobiledevice3  版本 11.23.0（当前解释器）
[PASS] pmd_pytcp  userspace 隧道网络栈（无管理员路径必需）：已安装
[PASS] pytun_pmd3  内核隧道 / Wintun（仅管理员降级路径需要）：已安装
[SKIP] Apple Mobile Device Service  非 Windows
[SKIP] usbmux TCP 127.0.0.1:27015  非 Windows（Windows 上才是 TCP 27015）
[WARN] 设备枚举（usbmux list）  0 台设备（114ms）
汇总: PASS=5 WARN=2 FAIL=1 SKIP=4
结论: 有阻塞项（退出码 2）
EXIT=2
```

### 9.3 默认引擎探针自检（证明 pmd3 11.23.0 的 import 链路与设备枚举可用）

> 输出为节选；`EXIT=0` 是原始值。

```console
$ .venv/bin/python windows/probe/probe-device.py --self-check ; echo EXIT=$?
[PASS] pymobiledevice3 可用性  版本 11.23.0（/Users/seinding/workspace/locpilot/.venv/bin/python）
[WARN] 平台  macOS-27.2-arm64-arm-64bit / Python 3.9.6
[PASS] pmd3 模块导入  usbmux / lockdown / UserspaceRsdTunnel / auto_mount / DVT 均可用
[WARN] usbmux 设备枚举（1ms）  0 台设备（--self-check 允许；这只说明脚本/运行时可导入，不能替代 Windows 真机验收）
汇总: PASS=2 WARN=2 FAIL=0 SKIP=0
结论: 全绿（退出码 0）
EXIT=0
```

### 9.4 降级引擎探针：真实 go-ios v1.3.2 二进制（macOS 包，用于验证脚本流程）

> 输出为节选（保留了关键 PASS/WARN/FAIL 行）；两个 `EXIT` 均为原始值。

```console
$ .venv/bin/python windows/probe/probe-goios.py --self-check --goios /tmp/win-engine/goiosmac/ios ; echo EXIT=$?
[PASS] ios version  go-ios 1.3.2（darwin）
结论: 全绿（退出码 0）
EXIT=0

$ .venv/bin/python windows/probe/probe-goios.py --agent-only --goios /tmp/win-engine/goiosmac/ios \
      --pair-record-path /tmp/win-engine/goios-pair --agent-timeout 15 ; echo EXIT=$?
[PASS] go-ios agent（263ms）  --userspace 模式已启动（端口 60105，无管理员）
[PASS] agent 首次隧道扫描  /ready = 200
[WARN] tunnel ls（170ms）  没有隧道（[]）
[FAIL] 设备列表（152ms）  0 台设备（{"deviceList":[]}）
[PASS] 停止 agent  已发送 /shutdown 并等待退出
结论: 有阻塞项（退出码 2）
EXIT=2
```

这组结果证明：脚本的 agent 启动/健康检查/隧道查询/设备解析/关闭清理流程在真实 go-ios 二进制上是对的；**唯独 Windows 上的 userspace 隧道与 setlocation 阻塞语义仍需 W2/W10 验证**。

### 9.5 go-ios v1.3.2 资产校验（本机实测）

```console
$ curl -sSL -o go-ios-win.zip https://github.com/danielpaulus/go-ios/releases/download/v1.3.2/go-ios-win.zip
$ shasum -a 256 go-ios-win.zip
939c6bcaafed183a92afb9f79cc11b1f935fa6389bfc94d3902e3f52c4dff3fe  go-ios-win.zip
$ unzip -l go-ios-win.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
 23691264  08-12-2026 00:40   ios.exe
$ unzip -o -q go-ios-win.zip -d winexe && file winexe/ios.exe
winexe/ios.exe: PE32+ executable (console) x86-64, for MS Windows
```

### 9.6 未验证的边界（如实标注）

- Windows 主机 192.168.0.126 当前 ping 不通，无法远程跑探针；本机也没有 iOS 设备，无法做真机 set/clear。
- 因此「Windows 上无管理员 userspace 隧道」依据是**官方支持矩阵 + 源码路径**，而非本机实测；这是本文件最大的不确定项（见 §10）。
- pmd3 11.23.0 的 `pytun_pmd3` 4 个架构 wintun.dll 已在 venv 中（`pytun_pmd3/wintun/bin/{amd64,x86,arm,arm64}/wintun.dll`），但那是内核隧道降级用，默认路径不加载。

---

## 10. 风险登记

| 风险 | 等级 | 影响 | 缓解 |
|---|---|---|---|
| **Windows 真机未验证**（本机 macOS、Windows 主机不可达） | 高 | 官方文档说 17.4+ 无 root 支持，但我们没在自己的 Windows 上跑通过 | 交付前必须跑 W1–W12；探针已写好，一次跑完即可判定 |
| PyInstaller 打包 pmd3 依赖漏收集 | 中 | 冻结包里 `import pymobiledevice3` 失败 → `ENGINE_MISSING`，真机完全不可用 | `--collect-all pymobiledevice3` + hidden imports；W9 验收；`availability()` 明示原因 |
| 事件循环策略差异（Proactor vs Selector） | 中 | Wi-Fi/QUIC 的 `add_reader` 在 Proactor 上不可用 | 进程内已固定 Selector（W5 A/B 验证）；USB 主路径不受影响 |
| DDI 首次下载失败（网络/Apple CDN） | 中 | 首次连接无法就绪，DVT 服务不可用 | App 重试 + 明确文案；有网络即可；用户可先连一次让缓存落地 |
| iOS 小版本差异（27.x 后续更新） | 中 | DVT/隧道协议可能随 iOS 更新变化（历史上多次） | 引擎抽象可切 go-ios；探针可作为回归脚本；保持 pmd3 升级通道 |
| go-ios 在 Windows 无真机 CI | 中 | 降级路径失效时只能现场发现 | 降级仅在默认引擎不可用时使用；探针覆盖；日志留存（agent 日志） |
| go-ios 清空定位不可靠（Ctrl-Break 不送达） | 中 | 用户手机可能残留模拟定位 | 只在降级用；失败提示重启手机；默认引擎无此问题 |
| 杀软/企业策略拦 python 或 ios.exe | 低 | 引擎进程无法启动 | 错误码区分（ENGINE_MISSING/GOIOS_MISSING）+ 帮助链接 |
| 多设备选择歧义 | 低 | 改错设备定位 | `--udid`/设备选择器必选；现有 `DeviceInfo` 已带 UDID |
| GPL 合规 | 低 | 分发 pmd3 需遵守 GPL-3.0 | 项目本身 GPL-3.0；保留 LICENSE/NOTICE；go-ios MIT 的版权声明随包保留 |

---

## 11. 证据索引（一手来源）

### 11.1 本机 pymobiledevice3 11.23.0 源码（`.venv/lib/python3.9/site-packages/`）

| 结论 | 位置 |
|---|---|
| Windows usbmux 是 TCP 27015 | `pymobiledevice3/usbmux.py:39`、`pymobiledevice3/osu/win_util.py:79` |
| 默认隧道偏好 userspace（无 root） | `pymobiledevice3/cli/cli_common.py:421`、`:442`；降级到 tunneld 在 `:597`、`:612` |
| userspace 隧道的 Windows 适配（socketpair/loopback/Proactor） | `pymobiledevice3/remote/userspace_tunnel.py:220`、`:444`；`pmd_pytcp/lib/io_backend.py:54` |
| 只有内核隧道要管理员 | `pymobiledevice3/cli/remote.py:417`；`pymobiledevice3/cli/lockdown.py:206`（`@sudo_required`） |
| Windows 管理员错误文案 | `pymobiledevice3/osu/win_util.py:91`、`:61`；`pymobiledevice3/__main__.py:448` |
| Windows 事件循环策略 | `pymobiledevice3/__main__.py:98`（`WindowsSelectorEventLoopPolicy`） |
| iOS 17+ 自动重试提示 | `pymobiledevice3/__main__.py:432`（`Trying again over ...`） |
| iOS 17.0-17.3 需特权 tunneld | `pymobiledevice3/__main__.py:111`（`INVALID_SERVICE_MESSAGE` 原文） |
| Store 版 Apple Devices 不发现 Wi-Fi | `pymobiledevice3/osu/win_util.py:25`、`:31`、`:48` |
| iOS 27 = Cryptex1 DDI + 需要 RSD | `pymobiledevice3/services/mobile_image_mounter.py:340`（`Version("27.0")`）、`:661-663`（`RSDRequiredError`） |
| 开发者模式可主动检测 | `pymobiledevice3/lockdown.py:534`（`get_developer_mode_status`，读 `com.apple.security.mac.amfi/DeveloperModeStatus`） |
| `auto_mount` 签名 | `pymobiledevice3/services/mobile_image_mounter.py:696` |
| Wintun 仅在 kernel 隧道用（pytun_pmd3 带 4 架构 dll） | `pytun_pmd3/__init__.py:3`；`pytun_pmd3/wintun/bin/*/wintun.dll` |

### 11.2 官方文档（网络，均已抓取正文核对）

- iOS 17+ tunnels / 支持矩阵：<https://doronz88.github.io/pymobiledevice3/latest/guides/ios17-tunnels/>
- 安装与 Windows 平台说明（iTunes、管理员）：<https://doronz88.github.io/pymobiledevice3/latest/installation/>
- 排障（Windows 服务、iOS 27 Cryptex1、admin 错误原文）：<https://doronz88.github.io/pymobiledevice3/latest/guides/troubleshooting/>
- 网络栈（Windows usbmux TCP 27015、socketpair）：<https://doronz88.github.io/pymobiledevice3/latest/guides/network-stacks/>
- Store 版 Apple Devices 的 Wi-Fi 问题：<https://github.com/doronz88/pymobiledevice3/issues/1968>

### 11.3 go-ios v1.3.2（本机解包源码 + release 资产实测）

| 结论 | 位置 |
|---|---|
| Windows 二进制存在且只有 exe | release `go-ios-win.zip`，sha256 `939c6bca…`，内含 `ios.exe` PE32+ x86-64 |
| `--userspace` 跳过管理员探测 | `cmd_tunnel.go:44-50`；Windows CheckPermissions 在 `ios/tunnel/tunnel_windows.go:24-28` |
| 错误文案明确 userspace 不需要管理员 | `ios/tunnel/tunnel_windows.go:114` |
| 内核模式需要 wintun.dll + 管理员 | `README.md`（For iOS 17+ ... wintun.dll ... C:/Windows/system32）；`ios/tunnel/wintun_windows.go` |
| Windows usbmux 也是 TCP 27015 | `ios/usbmuxconnection.go:52` |
| agent 隧道信息端口实际 60105，帮助文本 28100 过期 | `ios/connect.go:388`；`main.go:197` |
| setlocation 阻塞到 SIGINT | `main.go:1373-1387` |
| `resetlocation`/`setlocationgpx` 无 RSD 分支 | `cmd_device_location.go` |
| agent 自动启动仅实验性（ENABLE_GO_IOS_AGENT） | `cli_setup.go:84`、`:92` |
| 上游真机 CI 无 Windows | `.github/workflows/real-device.yml:25-33`（matrix 仅 macOS/Linux） |
| `ios list --details` 返回 iOS 版本 | `main.go:1446`（`detailsEntry`） |

### 11.4 本仓库相关文件

- `windows/app.py`：进程内后端 + PyInstaller 冻结 + `LOCALAPPDATA` 状态目录（Windows 版外壳）
- `windows/requirements.txt`：需补 `pymobiledevice3>=11.23.0`
- `locpilot/engine/pmd3.py`：`inproc_enabled()` / `_InProcWorker` / 进程内 `_list_devices_inproc`（Lead 实现）
- `locpilot/engine/pmd3_worker.py`：进程内先 RSD 后 mount、Session 持隧道句柄并 `aclose`、Windows Selector 事件循环（Lead 实现）
- `tests/test_pmd3_inproc.py`：进程内 worker 契约测试（Lead 自测 6 项；全量 154 项 Python 测试通过）
- `docs/engine-options.md`：go-ios 换引擎可行性（另一任务，macOS 实测）

---

## 12. 范围与清理说明

- 本任务只新增：`docs/windows-engine.md`、`windows/probe/{probe-windows.py,probe-device.py,probe-goios.py,README.md}`。
- **未修改** `locpilot/`、`macos/`、`README.md`、`CHANGELOG.md`（`locpilot/engine/` 下的进程内 worker 改动由 Lead 完成，见 §1.4/§1.5）。
- 调研临时文件（go-ios 源码/二进制、pmd3 网页文档、pycache）全部在 `/tmp/win-engine/`，任务结束删除；仓库内无 `.bak`、无缓存目录。
- 探针脚本运行不会改系统配置；`probe-goios.py` 自己启动的 agent 会 `/shutdown` 停掉，临时日志退出即删。
