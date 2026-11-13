# LocPilot Windows 版：安装、开发与打包

本目录是 LocPilot 的 Windows 端外壳与打包配置：pywebview 原生窗口 + 进程内 Python 后端
（复用仓库 locpilot 包），用 PyInstaller 产出单文件 LocPilot.exe。

> **验证状态（如实）**：本仓库的构建机是 macOS（arm64，无 Windows 主机、无 Xcode/pwsh 预装）。
> PowerShell 脚本已用 PowerShell 7.4.6 做**语法解析、-WhatIf 全流程预检**；locpilot.spec 已在
> macOS 上用真实 PyInstaller 6.22.3 跑通一次**交叉打包流程**（见 §6.3）。
> **所有 Windows 实机行为（exe 构建、双击运行、WebView2、真机连接）都还没有在 Windows 上验证**，
> §6.1 的每一条都标注了「待 Windows 验证」，请按顺序执行并记录原始输出。

---

## 0. 目录与职责

| 文件 | 作用 |
|---|---|
| windows/app.py | 外壳入口：准备环境 → 进程内起后端 → 等 /api/health → pywebview 开窗口；支持 --selftest 无窗口自检、--port 调试 |
| windows/build.ps1 | 一键构建：Python 检查 → 幂等 venv → 幂等装依赖 → PyInstaller → 产物校验 → 冻结包自检 |
| windows/locpilot.spec | PyInstaller 配置：收集 locpilot / windows/ui / pymobiledevice3 / pywebview / 图标 / 版本资源 |
| windows/run-dev.ps1 | 开发模式：不打包，装依赖后直接 python windows/app.py |
| windows/requirements.txt | 运行期依赖（pywebview、pymobiledevice3）；构建时另装 pyinstaller、pillow |
| windows/verify.py | 源码级端到端冒烟（mock 引擎，8 项断言）；跨平台，开发机可直接跑 |
| windows/ui/ | Windows 版 Web UI（高德底图 + GCJ-02 纠偏）；打包到 exe 内的 `web/` 目录（tests/ 不参与运行期，一并入包无影响） |
| windows/probe/ | Windows 主机/真机/降级引擎探针；详见 windows/probe/README.md 与 docs/windows-engine.md |

---

## 1. 系统要求

| 项目 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10 1809+ / Windows 11，x64 | **Windows on ARM 未验证**；32 位不支持 |
| WebView2 Runtime | 必须有 | Windows 11 预装；Windows 10 若没有，`Get-ChildItem` 检测后安装 Evergreen Runtime（见 §3.1） |
| Python（仅**构建/开发**需要） | 3.9 ~ 3.13，**64 位** | 推荐 3.11/3.12；安装时勾选 Add python.exe to PATH。最终用户跑 exe 不需要 Python |
| 磁盘 | 首次构建约 1.5 GB 可用 | windows\.venv 约 300-500 MB，PyInstaller 工作目录与产物数百 MB |
| 网络 | 首次构建需 PyPI；真机首次连接需下载开发者镜像（DDI） | pip 慢可用 -IndexUrl 换镜像 |
| 真机（可选） | Apple Mobile Device Service（**iTunes 经典版**提供） | Microsoft Store 版 Apple Devices 能走 USB，但不支持 Wi-Fi 发现设备（pymobiledevice3 #1968 / docs/windows-engine.md §1.2） |
| 管理员权限 | **默认路径不需要** | 只有 iOS 17.0-17.3 的内核隧道降级才要管理员（不推荐）；iOS 17.4+ / iOS 27 走无管理员 userspace 隧道 |

---

## 2. 用户：直接运行 LocPilot.exe

1. 拿到构建产物 windows\dist\LocPilot.exe（或发布包），可放在任意目录（**不要**放进需要管理员权限才能写的目录）。
2. 双击运行。首次会被 SmartScreen 拦截（exe 未做代码签名）：
   点「更多信息」→「仍要运行」；企业环境如被策略禁止，需要管理员签名或加白名单。
3. 窗口出现后：
   - 右上角设备按钮：连接 / 断开 iPhone；
   - 地图上点击任意位置 = 把手机定位改到该点（显示层负责 GCJ-02 纠偏，后端只存 WGS-84）；
   - 恢复真实定位：控件簇里的「恢复真实定位」。
4. 状态目录：`%LOCALAPPDATA%\LocPilot`

   | 文件 | 内容 |
   |---|---|
   | state.json | 历史记录 / 收藏 |
   | settings.json | 用户偏好（引擎、底图等） |
   | locpilot.log | 运行日志（**窗口版没有控制台，排查先看这个文件**） |
   | startup-error.html | 后端起不来时窗口里显示的排障页 |

5. 单实例：命名互斥量（`Local\LocPilotSingleInstance`），双击两次只会开一个窗口，第二次静默退出。

卸载：删除 LocPilot.exe 与 `%LOCALAPPDATA%\LocPilot`；如装过真机引擎，再删 `%USERPROFILE%\.pymobiledevice3`。

---

## 3. 真机连接前置（Windows）

按顺序检查；详细证据与错误码见 [docs/windows-engine.md](../docs/windows-engine.md)。

### 3.1 WebView2 Runtime
```powershell
# 有输出（版本号）就是已安装；没有输出则需安装
Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction SilentlyContinue | Select-Object pv
```
安装地址：<https://developer.microsoft.com/microsoft-edge/webview2/>（Evergreen Runtime，不需要打包浏览器内核）。

### 3.2 Apple Mobile Device Service
```powershell
Get-Service 'Apple Mobile Device Service' -ErrorAction SilentlyContinue | Format-List Name, Status, StartType, BinaryPathName
Start-Service 'Apple Mobile Device Service'   # 未运行时启动
```
没有这个服务 = 没装 iTunes 经典版（或安装不完整）。从 Apple 官网下载 Windows 版 iTunes 安装包（不要只装 Microsoft Store 的 Apple Devices，它没有 Wi-Fi 发现能力）。

### 3.3 设备与信任
- 用**能传数据**的数据线连接 iPhone（不是纯充电线）；
- 解锁屏幕；
- 手机上弹「要信任此电脑吗？」点「信任」，并输入锁屏密码。

### 3.4 开发者模式（iOS 16+）
iPhone：设置 → 隐私与安全性 → 开发者模式 → 打开（会要求重启并输入锁屏密码）。
也可以在电脑上执行（手机保持连接、解锁；App 内置的是引擎库、不提供命令行，这里需要机器上另装有 Python + pymobiledevice3，仅排障用）：
```powershell
python -m pymobiledevice3 amfi enable-developer-mode
```

### 3.5 开发者镜像（DDI）
首次连接时自动从 Apple 下载并挂载（iOS 27 是 Cryptex1 DDI），需要联网、可能需要解锁手机。
缓存位置：`%USERPROFILE%\.pymobiledevice3`。失败时窗口会给出「开发者镜像未就绪」提示，重试即可。

### 3.6 权限：默认不需要管理员
| 场景 | 是否需要管理员 |
|---|---|
| iOS 17.4+（含 iOS 27）USB 连接 + 改定位 | **不需要**（userspace 隧道，纯 Python 网络栈） |
| iOS 17.0 - 17.3 | 需要：`pymobiledevice3 remote tunneld`（管理员）+ 第三方驱动，属于降级路径 |
| 内核隧道排障（`lockdown start-tunnel`） | 需要 |

### 3.7 可选：go-ios 降级引擎
默认引擎（内置 pymobiledevice3）不可用时才需要。下载 <https://github.com/danielpaulus/go-ios/releases/tag/v1.3.2> 的 `go-ios-win.zip`，
解压得到 ios.exe，放到固定目录并通过环境变量告知 LocPilot：
```powershell
setx LOCPILOT_GOIOS "C:\Tools\go-ios\ios.exe"
```
重新打开 LocPilot 后，引擎下拉框会出现 go-ios。注意：它一个坐标起一个进程，只适合单点/低频；清空定位可能不可靠（详见 docs/windows-engine.md §2）。

---

## 4. 开发模式（不打包）

前置：装好 64 位 Python 3.9+，在**仓库根目录**打开 PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # 仅当前窗口生效，避免改系统策略

# 1) 首次：创建 windows\.venv 并安装依赖，然后打开窗口（mock 引擎，不碰真机）
.\windows\run-dev.ps1 -Mock

# 2) 真机开发（默认 auto 引擎，进程内 pymobiledevice3）
.\windows\run-dev.ps1

# 3) 只做无窗口自检（不开 GUI，验证后端 + 静态 UI）
.\windows\run-dev.ps1 -SelfTest

# 4) 只看会执行什么，不修改任何文件
.\windows\run-dev.ps1 -WhatIf -Mock
```

run-dev.ps1 参数：

| 参数 | 作用 |
|---|---|
| -Python <路径/命令> | 指定解释器；缺省依次尝试 py -3、python、python3 |
| -VenvDir <目录> | 虚拟环境目录，缺省 windows\.venv（与 build.ps1 共用） |
| -Mock | 设置 LOCPILOT_ENGINE=mock，使用虚拟设备演示/联调 |
| -SkipInstall | 不联网，要求依赖已就绪 |
| -UpgradeDeps | 强制 pip --upgrade |
| -IndexUrl <url> | pip 镜像（例如 https://pypi.tuna.tsinghua.edu.cn/simple） |
| -SelfTest | 等价 python windows\app.py --selftest（不开窗口） |
| -Port <端口> | 固定后端端口（调试用；默认 0 = 系统分配） |

说明：
- 开发模式下 windows\app.py 会把后端静态目录指向 `windows\ui`（不是仓库旧 web/），改 UI 代码后重启脚本即可生效；
- 引擎运行在 App 进程内（LOCPILOT_PMD3_INPROC=1），与打包版行为一致；
- 状态目录同样是 `%LOCALAPPDATA%\LocPilot`，勿与打包版同时运行（单实例锁会拦第二个窗口）。

---

## 5. 构建 LocPilot.exe

### 5.1 一条命令
```powershell
cd locpilot
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\windows\build.ps1
```
产物：`windows\dist\LocPilot.exe`（onefile，无控制台窗口）。若想放到仓库根的 dist\，
用 `-OutputDir ..\dist`（注意该目录需要自行加入 .gitignore）。

### 5.2 常用组合
```powershell
.\windows\build.ps1 -WhatIf                 # 只打印预检与计划，零写入
.\windows\build.ps1 -Clean                  # 清理 windows\.build 与旧产物后重建
.\windows\build.ps1 -Verify                 # 构建前额外跑 windows\verify.py（8 项源码级冒烟）
.\windows\build.ps1 -NoSelfTest             # 跳过构建后的 exe --selftest（不推荐）
.\windows\build.ps1 -OneDir                 # 产出 windows\dist\LocPilot\LocPilot.exe（排障/启动更快）
.\windows\build.ps1 -SkipInstall            # 不联网（依赖需已就绪）
.\windows\build.ps1 -UpgradeDeps            # 强制升级依赖
.\windows\build.ps1 -IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple
.\windows\build.ps1 -NoIcon                 # 不生成图标
```

build.ps1 参数：

| 参数 | 作用 |
|---|---|
| -Python / -VenvDir / -OutputDir | 自定义解释器、venv、产物目录（缺省 windows\.venv、windows\dist） |
| -OneDir | onedir 产物（调试方便，体积更大、启动更快） |
| -Clean | 删除 windows\.build 与旧产物，PyInstaller 加 --clean |
| -SkipInstall / -UpgradeDeps / -IndexUrl | 依赖安装控制（离线、强制升级、镜像） |
| -Verify | 构建前跑源码级端到端冒烟 |
| -NoSelfTest | 跳过冻结包自检 |
| -NoIcon | 跳过图标生成 |
| -HiddenImports a,b | 额外 hiddenimports，排障用 |
| -WhatIf | 只做只读预检并打印计划（不创建 venv、不联网、不打包） |

### 5.3 幂等与失败语义
- 重复执行安全：venv 存在则复用；依赖齐备则**完全不联网**；PyInstaller 会覆盖旧产物。
- 任何一步失败（含 pip、PyInstaller、产物缺失、--selftest 非 0）都会以**非零退出码**结束，并打印可读原因。
- 失败时保留 windows\.build\pyinstaller 下的构建日志，便于排查；成功后可加 -Clean 释放空间。

### 5.4 构建过程做了什么
1. 校验 Python（>=3.9、64 位）与源码文件；
2. 幂等创建 windows\.venv；
3. 幂等安装 windows\requirements.txt + pyinstaller>=6.3 + pillow>=9；
4. 解析版本号（locpilot\__init__.py），生成图标（windows\assets\locpilot.ico 不存在时用 macos\tools\make-icon.py + Pillow 生成）与 Windows 版本资源；
5. 调用 PyInstaller（windows\locpilot.spec）打包；
6. 校验产物大小，并默认执行 `LocPilot.exe --selftest` 断言 backend=ok / ui=ok / frozen=True。

### 5.5 打包资源映射（与后端的契约）
| 源 | 打包后（_MEIPASS 内） | 消费方 |
|---|---|---|
| windows/ui/ | web/ | locpilot/server.py 的静态站点根；app.py 冻结时 ui_dir() 指向它 |
| locpilot/ 包 | PYZ（Python 模块） | 进程内后端 |
| pymobiledevice3、pywebview | PYZ + 数据文件/DLL（collect_all） | 引擎进程内 worker；WebView2 平台模块 |
| locpilot/engine/pmd3_worker.py | locpilot/engine/（**真实文件**） | 非冻结/子进程兜底时由外部 python 执行 |
| 生成的 locpilot.ico | windows/assets/locpilot.ico | app.py 探测窗口图标 |
| LICENSE / NOTICE.md | 包根目录 | GPL 合规 |

> 不要改 windows/ui 到 web 的映射：locpilot/server.py 的 WEB_DIR 与 windows/app.py 的折叠逻辑都依赖
> `<_MEIPASS>/web`；改成别的目录会导致窗口白屏（--selftest 会失败报 ui=failed）。

### 5.6 构建目录
| 路径 | 内容 | 是否入库 |
|---|---|---|
| windows\.venv\ | 构建/开发用虚拟环境（含 PyInstaller、pywebview、pymobiledevice3） | 否 |
| windows\.build\ | PyInstaller 工作目录、中间文件、图标/版本资源 | 否 |
| windows\dist\ | 最终产物 LocPilot.exe（-OneDir 时是 LocPilot\ 目录） | 否 |
| windows\assets\locpilot.ico | 可选：自定义图标（存在时优先使用） | 可选入库存 |

---

## 6. 验收

### 6.1 Windows 上必须执行的验收清单（**全部待 Windows 验证**）

| # | 命令（仓库根目录，PowerShell） | 预期结果 | 验证点 |
|---|---|---|---|
| W1 | `pwsh -NoProfile -File windows\build.ps1 -WhatIf` | exit 0；打印「WhatIf 检查完成」，列出 Python/venv/依赖/打包命令/产物路径 | 脚本语法、预检、Windows 路径解析 |
| W2 | `.\windows\build.ps1` | exit 0；末尾「构建成功」+ 产物路径 + SHA256 + 自检通过行 | 幂等构建、pip、PyInstaller、冻结包自检 |
| W3 | `.\windows\dist\LocPilot.exe --selftest; $LASTEXITCODE` | 退出码 0；日志（§6.2）出现 backend=ok / ui=ok / frozen=True | exe 内后端 + windows/ui 静态资源 + WebView2 无关部分 |
| W4 | `.\windows\dist\LocPilot.exe` | 出现 LocPilot 窗口；地图瓦片加载；无崩溃、无白屏 | WebView2、pywebview、windows/ui 实际渲染 |
| W5 | W4 用 `--port 8899` 启动后，另开窗口执行：`(Invoke-WebRequest http://127.0.0.1:8899/).StatusCode`；`(Invoke-WebRequest http://127.0.0.1:8899/js/app.js).StatusCode`；`(Invoke-WebRequest http://127.0.0.1:8899/vendor/leaflet.js).StatusCode`；`Invoke-RestMethod http://127.0.0.1:8899/api/health` | 四个请求依次为 200 / 200 / 200 / ok；根页面 HTML 含 `./js/app.js` | 后端 HTTP、静态资源 MIME/200、API |
| W6 | `.\windows\run-dev.ps1 -SelfTest` | exit 0，输出 backend=ok / ui=ok | 开发模式（源码 + windows/ui） |
| W7 | `.\windows\run-dev.ps1 -Mock` | 窗口出现；点地图能改虚拟设备定位；状态/日志正常 | mock 全链路 |
| W8 | `python windows\verify.py`（用 windows\.venv\Scripts\python.exe） | 8 项全部 OK，exit 0 | 后端 API 端到端（health/status/connect/teleport/UI/clear） |
| W9 | `python windows\probe\probe-windows.py` | exit 0（无设备也应 PASS 主机项；有阻塞看报告） | Apple 服务、usbmux 27015、WebView2、pmd3 依赖 |
| W10 | 连接真机后 `python windows\probe\probe-device.py --lat 25.0339 --lon 121.5645 --hold 8` | 全 PASS，iPhone 地图位置变化 | 真机 iOS 17.4+/27 userspace 隧道、DDI、DVT |
| W11 | 上述真机操作后再跑 W3 | 仍 exit 0，日志 worker: inproc | 冻结包进程内 worker |

### 6.2 冻结包自检输出在哪里看
窗口版（console=False）没有控制台，--selftest 的输出被重定向到日志文件：
```powershell
& .\windows\dist\LocPilot.exe --selftest
$LASTEXITCODE
Get-Content "$env:LOCALAPPDATA\LocPilot\locpilot.log" -Tail 10
```
预期（示例）：
```
backend=ok
ui=ok
port=51234
home=C:\Users\<you>\AppData\Local\Temp\locpilot-selftest-xxxxxxxx
web_dir=C:\Users\<you>\AppData\Local\Temp\_MEIxxxxxx\web
python=3.12.10
frozen=True
```
两点说明：
- home 是 **--selftest 专用临时状态目录**，不会写你的 state.json / 历史记录；
- web_dir 指向 **Temp 下的 _MEI 目录**（onefile 解包位置），这是正常的；-OneDir 时是 dist\LocPilot\_internal\web。

### 6.3 本机（macOS）已完成的验证（原始命令见交付报告）
| 项 | 方法 | 结果 |
|---|---|---|
| build.ps1 语法 | PowerShell 7.4.6 `[Parser]::ParseFile` | 0 errors |
| build.ps1 全流程预检 | `pwsh -NoProfile -File windows/build.ps1 -WhatIf` | exit 0，输出完整计划（本机非 Windows 会先给 WARN） |
| run-dev.ps1 语法 + 预检 | 同上 | 0 errors / exit 0 |
| 两个 .ps1 静态分析 | PSScriptAnalyzer 1.25.0（Warning,Error） | 0 findings |
| build.ps1 完整流程演练 | macOS 上把路径/平台判定换成 mac 等价的补丁副本，跑真实 venv + PyInstaller + --selftest | exit 0：产物 30.5 MB、SHA256、selftest exit 0（backend=ok/ui=ok/frozen=True） |
| run-dev.ps1 -SelfTest 演练 | 同上补丁副本 | exit 0：backend=ok / ui=ok / web_dir=.../windows/ui |
| locpilot.spec Python 3.9 语法 | ast.parse(feature_version=(3,9)) | 通过 |
| locpilot.spec 实际打包 | macOS + PyInstaller 6.22.3 交叉跑 spec（onefile windowed） | 见交付报告：产物生成、冻结包 --selftest backend=ok/ui=ok/frozen=True |
| 资源映射 | 检查 PyInstaller 产物 | web/index.html 与 windows/assets/locpilot.ico 已入包 |
| Windows 专属 | exe 图标/版本资源、WebView2、真机 | **待 Windows 验证**（W1-W11） |

---

## 7. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `无法加载文件 ...build.ps1，因为在此系统上禁止运行脚本` | PowerShell 执行策略限制 | `Set-ExecutionPolicy -Scope Process Bypass`（只影响当前窗口），或把文件解除锁定：`Unblock-File .\windows\build.ps1` |
| `找不到可用的 Python 解释器` / 版本过低 | 未装 Python 或 PATH 里没有 | 安装 64 位 Python 3.9+ 并勾选 Add to PATH；或用 `-Python "C:\Python312\python.exe"` |
| `需要 64 位 Python` | 装了 32 位 Python 或 32 位 PowerShell | 装 64 位 Python；在 64 位 PowerShell 里执行 `[Environment]::Is64BitProcess` 应为 True |
| pip 下载失败 / SSL 错误 | 网络/代理/旧系统证书 | 换镜像：`-IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple`；或先手动运行 `python -m pip install --upgrade pip`；公司代理需设置 HTTP_PROXY/HTTPS_PROXY |
| PyInstaller 报 hidden import 缺失 / 打包后引擎不可用 | pmd3 依赖未收集全 | 本 spec 已 `collect_all` pymobiledevice3；若仍缺，用 `.build.ps1 -HiddenImports 模块名` 加白并反馈 |
| 双击 exe 没反应 / 一闪而过 | 窗口版无控制台，异常写进日志 | 看 `%LOCALAPPDATA%\LocPilot\locpilot.log` 与同目录 startup-error.html；确认 --selftest 能过 |
| 窗口白屏 / 一直转圈 | WebView2 缺失；或 UI 没打进包 | 先装 WebView2 Runtime；再跑 `LocPilot.exe --selftest` 看 ui=ok；构建日志里应有 `[spec] UI: ... -> web/` |
| SmartScreen「Windows 已保护你的电脑」 | exe 未签名 | 更多信息 → 仍要运行；正式分发需代码签名证书 |
| 杀毒软件报毒/删除 exe | PyInstaller 单文件特征误报 | 加白名单；或改 `-OneDir` 构建减少误报；正式分发建议签名 |
| 后端 20 秒未就绪（错误页） | 端口被占/被安全软件拦截/依赖缺失 | 看 locpilot.log；换 `--port`；确认 windows\.venv 依赖完整（build.ps1 会复检） |
| 第二次双击没反应 | 单实例锁 | 已有窗口在运行（任务栏找一下）；先关掉旧的 |
| 看不到 iPhone | Apple 服务未运行 / 线/信任/开发者模式/DDI | 按 §3 顺序检查；跑 `python windows\probe\probe-windows.py` 定位阻塞项 |
| 连接时报「开发者模式未开启」 | iOS 16+ 需要开发者模式 | 按 §3.4 打开并重启手机 |
| 连接时报「开发者镜像未就绪」 | 首次 DDI 下载失败/未联网 | 保持联网重试；解锁手机；见 §3.5 |
| iOS 17.0-17.3 提示需要管理员 | 该版本没有无管理员 userspace 隧道 | 升级 iOS 到 17.4+，或按 docs/windows-engine.md 用管理员 tunneld 降级 |
| 地图空白但定位可用 | 瓦片服务被墙/公司代理 | 换网络；高德瓦片需要能访问 webrd*.is.autonavi.com |
| 中文日志乱码 | 控制台代码页 | PowerShell 里先 `chcp 65001`，或用 Windows Terminal |
| pip/venv 目录被杀软锁住 | 实时防护扫描 venv | 给仓库目录加白；重跑 `-Clean` |
| 想彻底重来 | — | 关掉 LocPilot → 删 windows\.venv、windows\.build、windows\dist → 重跑 build.ps1（不会留下 .bak） |

---

## 8. 已知限制（如实）

1. **未在 Windows 实机验证**：本机构建机是 macOS，Windows 主机当前不可达；exe 构建、GUI、WebView2、真机连接均待 W1-W11 验收。
2. **未代码签名**：SmartScreen/杀软误报属预期；正式分发需 Authenticode 签名。
3. **onefile 首次启动有解包开销**：约 2-10 秒（取决于磁盘与杀软实时扫描）；-OneDir 可避免。
4. **窗口版无控制台**：stdout/stderr 全部进 `%LOCALAPPDATA%\LocPilot\locpilot.log`。
5. **真机依赖 Apple Mobile Device Service**：来自 iTunes 经典版；只装 Microsoft Store 的 Apple Devices 可走 USB 但无 Wi-Fi 发现。
6. **首次 DDI 需要联网**：iOS 27 是 Cryptex1 DDI，经 cryptexd 安装；离线环境首次连接会失败。
7. **默认无管理员**：仅 iOS 17.0-17.3 内核隧道降级需要管理员；不提供自动提权（uac_admin 未开启）。
8. **go-ios 降级引擎不随包分发**：需要用户自行下载 ios.exe（MIT，体积 23 MB）；默认路径不依赖它。
9. **Windows on ARM / 32 位未适配**；Python 必须 64 位。
10. **构建机需要 Python 与网络**：最终用户不需要，但构建/开发首次要联网装 PyPI 依赖。
11. **单文件体积较大**（约 30-60 MB）：内含 Python 运行时、pymobiledevice3、WebView2 平台模块。

---

## 9. 维护者笔记

- **加依赖**：改 windows/requirements.txt（运行期）；只在构建期用的工具依赖直接加进 build.ps1 的 pip 参数。
- **spec 里为什么用 collect_all**：pymobiledevice3 大量函数内延迟 import、pywebview 运行期动态加载平台模块，静态分析会漏；漏了的表现是运行期 ImportError / 窗口空白。
- **版本号**：build.ps1 从 locpilot\__init__.py 读取并通过 LOCPILOT_BUILD_VERSION 传给 spec，写进 exe 文件属性（右键 → 属性 → 详细信息）。
- **图标**：优先 windows\assets\locpilot.ico；没有时用 macos\tools\make-icon.py 画 PNG 再转 ICO（需要 Pillow）。生成物在 windows\.build 内，不污染源码。
- **自检契约**：windows\app.py --selftest 必须输出 backend=ok / ui=ok / frozen=True 并 exit 0；build.ps1 依赖这个契约判定成功（窗口版输出在 locpilot.log）。
- **改 UI 契约**：windows/ui 必须全部相对路径引用资源、API 走相对 `/api/...`（同源），由后端静态服务器提供；不要写死端口或使用 file://。
- **不要**把 windows/ui 改名或挪走：locpilot/server.py 的 WEB_DIR 契约在 _MEIPASS/web。

---

## 10. 参考

- 引擎选型与 Windows 可行性（证据、错误码表、探针）：[docs/windows-engine.md](../docs/windows-engine.md)
- Windows 探针用法：windows/probe/README.md
- macOS 构建：macos/build.sh（对照理解打包/冒烟结构）
