# LocPilot Windows UI（`windows/ui/`）

Windows 版的原生窗口界面：pywebview（WebView2）加载本地后端页面 `http://127.0.0.1:<port>/`，
页面与后端**同源**，只通过既有的 HTTP/SSE API 通信。交互语义与 macOS 版对齐：全屏高德地图、
点击落针改定位、右上控件簇、左下坐标读数。

## 文件结构

```
windows/ui/
├── index.html            入口（后端静态服务在 / 直接返回它）
├── styles.css            视觉与自适应布局（玻璃材质、暗/亮外观、减弱动效）
├── js/
│   ├── geo.js            WGS-84 ↔ GCJ-02（与 locpilot/core/coord.py 同构）
│   ├── model.js          快照 -> 界面状态（纯函数：状态色/读数/底图/弹框）
│   ├── api.js            /api 客户端（HTTP + SSE，全部相对路径）
│   ├── pin.js            落针状态机 PinAnimator + DOM 渲染 PinView
│   └── app.js            主程序：Leaflet 地图、交互、渲染、SSE
├── vendor/
│   ├── leaflet.js        Leaflet 1.9.4 本地副本（来自 web/vendor/，离线必需）
│   ├── leaflet.css
│   └── leaflet.LICENSE
├── tests/                node 单元断言 + 浏览器端到端验收（运行期不加载）
└── README.md
```

## 集成契约（外壳/打包必须遵守）

1. **入口固定为 `index.html`**，由后端 `LocPilotServer` 的静态服务在 `/` 提供；
   Windows 版在构造 Server 之前把 `locpilot.server.WEB_DIR` 指到本目录
   （开发模式：`windows/app.py` 的 `apply_web_dir()`；打包：`windows/ui` 作为
   PyInstaller datas 目标名 `web`，frozen 后即 `_MEIPASS/web`）。
2. **全部静态资源用相对路径**（`./vendor/leaflet.js`、`./js/app.js`…），
   不使用 `/` 开头的绝对路径、不写死端口、**不引用任何 CDN**。
3. **API 一律相对路径 `/api/...`**：页面与后端同源，无 CORS、无端口发现。
4. **无构建步骤**：纯静态文件，不需要 node_modules / 打包器；node 只用于跑测试。
5. 窗口加载 URL 即后端根地址，页面启动后先 `GET /api/status` 再订阅 `GET /api/events`（SSE）。

## 交互语义（对齐 macOS 版）

- **点击地图 → 落针**：目标圆环浮现 → 从 80px 高处落下 → 落地冲击 + 白色波纹 →
  `processing` 停留 280ms（"系统确认"）→ 调 `POST /api/teleport`（WGS-84）→
  成功：针体 0.30s 红→绿 + 白色对勾绘制 + 绿色波纹；失败：白色感叹号淡入 + 针尖摇晃 + 红色波纹，
  并在顶部提示失败原因。成功/失败由业务结果驱动，不写死在动画里。
- **右上控件簇**（分组同 macOS 版）：
  - 手机图标：设备状态 —— 绿=已连接、黄=未连接、红=后端不可达；点击展开弹框显示
    设备名/「正在连接…」与连接/断开动作；
  - 定位箭头 + 恢复真实定位：跟随定位（镜头随定位变化自动移动）、`POST /api/clear`；
  - ＋ / －：缩放。
- **左下角**：坐标读数（WGS-84，6 位小数）与「© 高德地图」归属标注**同一行**；
  **无定位时读数完全不出现**（没有 `—` 之类占位符）。
- **跟随定位**：开启后定位变化才带镜头（用户手动平移不会被抢）；无定位时按键置灰并提示。
- 窗口可任意缩放：控件尺寸/间距随视口 `clamp`，布局自适应（最小 640×420 实测不溢出、无滚动条）。

## 坐标系

- 后端与 API **只认 WGS-84**（设备真值）。显示层按底图 `crs` 做转换：
  高德底图 = GCJ-02，OSM = WGS-84；点击底图时先反解回 WGS-84 再下发。
- `js/geo.js` 与 `locpilot/core/coord.py` 完全同构：同一组克拉索夫斯基椭球常数、
  同一组三次多项式扰动、同样的境外矩形（经度 72.004~137.8347、纬度 0.8293~55.8271，境外原样返回）、
  同样的 8 次不动点迭代。测试用 Python 生成的**金标坐标**逐点比对（1e-8 度内一致）。
- 上海偏移实测 481.2 米，反解误差 < 1e-9 度（约 0.1 毫米）。

## 离线可用

- Leaflet 1.9.4 是仓库内本地副本（`vendor/`，MIT，License 随附）；字体只用系统字体；
  没有任何需要联网的第三方 CDN。
- **唯一需要网络的资源是高德瓦片**（直连 `webrd0x.is.autonavi.com`，属于地图影像本身）。
  瓦片不可达时：地图保留浅色底、控件/连接/落针/坐标读数/恢复真实定位全部照常可用。
  端到端验收里用 CDP 阻断 `*://*.autonavi.com/*` 做了实测（见下）。

## 运行与验收

在仓库根目录执行：

```bash
# 1) 单元断言（零第三方依赖，node >= 20）
node windows/ui/tests/run-tests.mjs

# 2) 静态服务 + curl 断言（相对路径、无外链、引用文件齐全、MIME 正确）
python3 -m http.server 8931 --directory windows/ui
curl -s -o /dev/null -w '%{http_code} %%{content_type}\n' http://127.0.0.1:8931/
#   然后逐个 curl ./styles.css、./js/*.js、./vendor/* 并断言 200

# 3) 浏览器端到端（真实后端 mock 引擎 + 无头 Chrome，同源拓扑）
node windows/ui/tests/verify-ui.mjs
#   可选：--shot=<目录> 每步截图，用于人工视觉验收（临时目录，验完删除）

# 4) Windows 外壳自检（Lead/win-pack 提供）：后端 + 本 UI 指纹校验
.venv/bin/python windows/app.py --selftest     # ui=ok, web_dir=…/windows/ui
.venv/bin/python windows/verify.py             # 全部通过：8 项
```

端到端断言覆盖：本地 Leaflet、高德底图 + GCJ-02、点击落针阶段序
`dropping→landed→processing→success/error`、成功绿勾/失败红针与提示、设备状态
连接/断开/弹框、跟随定位、恢复真实定位、缩放、窗口 1440×900 / 900×620 / 640×420
自适应、瓦片断网仍可用、无外部请求（无 CDN）、无未捕获 JS 异常。

调试参数（仅调试/验收用，正常外壳不带）：`?engine=mock` 指定连接引擎、
`?lat=&lon=&zoom=` 指定初始视野、`?autoconnect=1` 启动即连接。
自动化入口：`window.__locpilot`（`ready / state / pin / geo / map() / actions`）。

## 已知限制 / 待 Windows 验证

本机是 macOS，以下与 Windows 宿主相关的行为**无法在本机验证**，Windows 上需复核：

1. **pywebview + WebView2 真实窗口**：无头 Chrome 已覆盖页面行为，但原生窗口创建、
   最小尺寸、DPI 缩放、窗口拖动/最大化，需要在 Windows 上跑
   `.venv\Scripts\python windows\app.py`（或 `dist\LocPilot.exe`）实测。
2. **WebView2 版本**：用到 `backdrop-filter`、Web Animations API、ES2017+；
   Edge WebView2 evergreen（建议 ≥ 110）均支持。若极端环境缺 `element.animate`，
   `pin.js` 会跳过着色/位移动画直接落到最终状态（功能不丢）。
3. **高德瓦片在网络环境中的可达性**：Windows 侧若有代理/防火墙策略，瓦片可能加载不出；
   UI 其余部分不受影响（离线路径已由断网用例覆盖）。
4. **HiDPI**：布局用 CSS 像素 + 相对单位，理论适配 125%/150% 缩放；需在 Windows 上截图确认。

## 备注

- 本目录只写 `windows/ui/`；未修改 `locpilot/`、`macos/`、`README/CHANGELOG`。
- `tests/` 会被整目录收进 exe 但不参与运行；如在意体积，win-pack 可在 spec 里排除 `tests`。
```