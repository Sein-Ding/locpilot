# LocPilot Windows 探针（windows/probe）

三条**只读/可回退**的探针，用来在 Windows 上验证「iOS 17+（含 iOS 27）改定位」的真实链路。
调研结论与失败文案见 [docs/windows-engine.md](../../docs/windows-engine.md)。

> 适用范围：Windows 10/11 + 官方 Apple 驱动（iTunes 经典版或 Apple Devices）。
> 真机验收必须在 **Windows + iPhone** 上跑；本仓库开发机是 macOS，已标注「待 Windows 验证」的项不能靠 macOS 结果代替。

## 0. 三条探针各管什么

| 脚本 | 回答的问题 | 需要设备 | 需要管理员 | 会改定位 |
|---|---|---|---|---|
| `probe-windows.py` | 「这台 Windows 具备改定位的前提吗」：Apple 移动设备服务、usbmux 端口、pmd3/go-ios、Python、管理员、端口占用 | 否 | 否 | 否 |
| `probe-device.py` | 「默认引擎（pymobiledevice3 进程内 userspace 隧道）能不能真的改定位」：枚举→配对→隧道→DDI→set→clear，逐步计时 | 是 | 否 | 是（结束自动清空） |
| `probe-goios.py` | 「降级引擎 go-ios 能不能用」：ios.exe→agent→隧道→setlocation→Ctrl-Break 清空 | 是（--self-check 除外） | 否 | 是（结束自动清空） |

## 1. 在 Windows 上跑之前

1. 安装 **iTunes 经典版**（<https://www.apple.com/itunes/download/win64>）—— 它提供 *Apple Mobile Device Service*。
   不推荐 Microsoft Store 的 Apple Devices：USB 能用，但**不支持 Wi-Fi 发现**（pmd3 issue #1968）。
2. iPhone：USB 数据线（能传数据）、解锁、首次弹「要信任此电脑吗？」点信任。
3. iPhone 设置 → 隐私与安全性 → **开发者模式** 打开（会要求重启）。
4. Python 运行时里要有 `pymobiledevice3>=11.23.0`（Windows App 打包时已内置；开发机可 `pip install "pymobiledevice3>=11.23.0"`）。
5. go-ios 只在需要降级时用：下载 <https://github.com/danielpaulus/go-ios/releases> 的 `go-ios-win.zip`，解压出 `ios.exe`
   （v1.3.2 的 zip 只有这一个文件，sha256 `939c6bca…`；**不需要** wintun.dll —— 那是内核隧道模式才要的，LocPilot 不用）。

## 2. 跑法（PowerShell / cmd，仓库根目录）

```powershell
# 2.1 主机就绪（不接手机也能跑）
python windows\probe\probe-windows.py
python windows\probe\probe-windows.py --json          # 给脚本/CI 用

# 2.2 默认引擎端到端：先在窗口里改成你手机附近坐标，跑完在 iPhone 地图上确认，脚本会自动清空
python windows\probe\probe-device.py --self-check                       # 只验证运行时/模块/枚举
python windows\probe\probe-device.py --lat 25.0339 --lon 121.5645 --hold 8
python windows\probe\probe-device.py --clear-only                       # 只清空

# 2.3 降级引擎（改成 ios.exe 实际路径）
python windows\probe\probe-goios.py --self-check --goios C:\tools\go-ios\ios.exe
python windows\probe\probe-goios.py --agent-only --goios C:\tools\go-ios\ios.exe
python windows\probe\probe-goios.py --lat 25.0339 --lon 121.5645 --hold 8 --goios C:\tools\go-ios\ios.exe
```

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 该探针覆盖的步骤全绿 |
| 1 | 设备/隧道/定位步骤失败（看每个 `[FAIL]` 下面的 `->` 处置提示） |
| 2 | 前置条件不满足：不是 Windows、没设备、pmd3 不存在、找不到 ios.exe |

### 输出怎么读

每行形如：

```text
[PASS] userspace 隧道（无管理员）（812ms）
       RSD fd12:34::1:59203（in_process=True）
[FAIL] 开发者镜像 DDI
       RSDRequiredError: ...
       -> 首次安装 Cryptex1 DDI 需要联网下载；确认 iPhone 已解锁。
```

- `[PASS]` = 该步在**本机**验证通过；
- `[WARN]` = 不影响继续，但建议处理；
- `[FAIL]` 且退出码 2 = 先按 hint 修前置条件，再重跑；
- `[SKIP]` = 当前模式跳过（例如 macOS 上跳过 Apple 服务检查）。

## 3. 三条探针对应的验收项（Windows 必跑）

| # | 验收项 | 命令 | 通过判据 |
|---|---|---|---|
| 1 | Apple 服务 + usbmux 就绪 | `probe-windows.py` | 除「平台/设备枚举」外无 FAIL |
| 2 | userspace 隧道**无管理员**可用 | `probe-device.py --lat ... --hold 5` | 「userspace 隧道（无管理员）」PASS |
| 3 | iOS 27 Cryptex1 DDI 可挂载（走 RSD） | 同上 | 「开发者镜像 DDI」PASS |
| 4 | 定位 set/clear | 同上 | 「设置模拟定位」「清空模拟定位」PASS，且手机地图目视确认 |
| 5 | 事件循环无回归 | 上面加 `--loop selector` 与 `--loop default` 各跑一次 | 两者 set 都 PASS |
| 6 | 重连不撞 one-tunnel-per-process | App 里断开/连接 3 次，或连续跑 3 次 probe-device | 每次都 PASS，无 "a userspace tunnel is already active" |
| 7 | 拔线恢复 | 保持定位时拔线再插回 | App 显示断开→自动恢复；无崩溃 |
| 8 | 降级引擎可用 | `probe-goios.py --lat ... --hold 5` | agent/set/clear 全 PASS；Ctrl-Break 后手机定位恢复 |

## 4. 不会留垃圾

- 不写注册表、不改服务、不装东西；`probe-windows.py` 纯只读。
- `probe-device.py` 用固定坐标测试后自动 `clear`；异常中断时按输出提示再跑 `--clear-only`。
- `probe-goios.py` 的 agent 日志写系统临时目录，退出时删除；自己启动的 agent 用 `/shutdown` 停掉（`--keep-agent` 可保留）。
  go-ios 的配对身份默认放 `%LOCALAPPDATA%\LocPilot\goios-pair`（`--pair-record-path` 可改）。
- 没有 `.bak`、不往仓库写文件。

## 5. 常见判断

| 现象 | 最可能原因 | 先做什么 |
|---|---|---|
| `probe-windows.py` 报 usbmux 27015 连接被拒 | Apple Mobile Device Service 没启动 | 服务里启动它；不行就重装 iTunes 经典版 |
| 服务 PASS 但枚举 0 台 | 线/锁屏/没点信任/开发者模式没开 | 换线、解锁、点信任 |
| 隧道 FAIL 且报 iOS 17.0–17.3 | 该版本 Windows 无管理员路径不可用 | 升级 iOS 到 17.4+，或管理员跑 `pymobiledevice3 remote tunneld` |
| DDI FAIL | 首次下载 Cryptex1 DDI 需要联网/被墙 | 检查网络后重试；加大 `--mount-timeout` |
| go-ios setlocation 提前退出 | agent 未就绪 / DDI 未挂载 / 开发者模式未开 | 先跑 `--agent-only` 与 `ios tunnel ls` 定位 |
