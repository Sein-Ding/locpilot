#!/usr/bin/env bash
# =============================================================================
# LocPilot 原生 macOS 版构建脚本：SwiftPM(release) → 可双击的 LocPilot.app
#
#   bash macos/build.sh              # 构建 macos/build/LocPilot.app
#   bash macos/build.sh --check      # 只做 SwiftPM 预检（构建 LocPilotKit），不组装 App
#   bash macos/build.sh --selftest   # 构建后跑 App 二进制的无界面自检（--selftest）
#   bash macos/build.sh --smoke      # 构建 → open 启动 → 进程存活 + 后端 /api/health + 崩溃报告守卫
#   bash macos/build.sh --run        # 构建后 open 启动
#   bash macos/build.sh --no-build   # 跳过编译，用现有产物直接跑 --selftest / --smoke / --run
#
# 本机环境（只有 Command Line Tools，无 Xcode）：
#   * 没有 xcodebuild / XCTest / SwiftUI 宏 → 用 swift build 出可执行文件后手工组装 .app；
#   * DSH 沙箱下 SwiftPM 的嵌套沙箱不可用 → --disable-sandbox；
#   * 模块缓存不能写家目录与 /var/folders → 全部重定向到工程内 macos/.build/。
#
# 产物：macos/build/LocPilot.app（ad-hoc 签名，可直接双击；对外分发需自行公证）。
# =============================================================================
set -euo pipefail

MACOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$MACOS_DIR/.." && pwd)"
BUILD_DIR="$MACOS_DIR/build"
APP="$BUILD_DIR/LocPilot.app"
CACHE="$BUILD_DIR/.cache"
SCRATCH="$MACOS_DIR/.build"          # SwiftPM scratch 与各类缓存，全部留在工程内
CONFIG="release"
PRODUCT="LocPilot"
BUNDLE_ID="com.locpilot.desktop"
MIN_MACOS="14.0"                     # 与 Package.swift 的 platforms: [.macOS(.v14)] 一致

RUN=0; SMOKE=0; SELFTEST=0; CHECK=0; NOBUILD=0

usage() { sed -n '3,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

for arg in "$@"; do
  case "$arg" in
    --run) RUN=1 ;;
    --smoke) SMOKE=1 ;;
    --selftest) SELFTEST=1 ;;
    --check) CHECK=1 ;;
    --no-build) NOBUILD=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# SwiftPM
# ---------------------------------------------------------------------------
swiftpm() {
  # 缓存重定向 + 关掉嵌套沙箱（DSH 沙箱下必须）
  export CLANG_MODULE_CACHE_PATH="$SCRATCH/clang-cache"
  export SWIFTPM_MODULECACHE_OVERRIDE="$SCRATCH/swiftpm-modulecache"
  mkdir -p "$CLANG_MODULE_CACHE_PATH" "$SWIFTPM_MODULECACHE_OVERRIDE"
  swift build \
    --package-path "$MACOS_DIR" \
    -c "$CONFIG" \
    --scratch-path "$SCRATCH" \
    --cache-path "$SCRATCH/cache" \
    --config-path "$SCRATCH/config" \
    --security-path "$SCRATCH/security" \
    --disable-sandbox \
    "$@"
}

bin_dir() {
  swift build --package-path "$MACOS_DIR" -c "$CONFIG" \
    --scratch-path "$SCRATCH" --cache-path "$SCRATCH/cache" \
    --config-path "$SCRATCH/config" --security-path "$SCRATCH/security" \
    --disable-sandbox --show-bin-path
}

if [ "$CHECK" = "1" ]; then
  echo "==> SwiftPM 预检：-c $CONFIG --product LocPilotKit"
  swiftpm --product LocPilotKit
  echo "✓ SwiftPM 预检通过（scratch: ${SCRATCH}）"
  exit 0
fi

mkdir -p "$BUILD_DIR" "$CACHE"

if [ "$NOBUILD" = "0" ]; then
  echo "==> SwiftPM 构建：-c $CONFIG --product $PRODUCT"
  swiftpm --product "$PRODUCT"
else
  echo "==> 跳过构建（--no-build）"
fi

BIN="$(bin_dir)/$PRODUCT"
if [ ! -x "$BIN" ]; then
  echo "✗ 构建产物不存在：$BIN" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 组装 .app
# ---------------------------------------------------------------------------
echo "==> 组装 $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$BIN" "$APP/Contents/MacOS/LocPilot"
chmod +x "$APP/Contents/MacOS/LocPilot"

# ---- Info.plist（LPRepositoryPath 指向仓库根：开发时直接用仓库里的 Python 源码）----
cp "$MACOS_DIR/Resources/Info.plist" "$APP/Contents/Info.plist"
PLIST="$APP/Contents/Info.plist"
plist_set() { # plist_set <key> <value>
  plutil -replace "$1" -string "$2" "$PLIST" 2>/dev/null \
    || plutil -insert "$1" -string "$2" "$PLIST"
}
plist_set CFBundleExecutable "LocPilot"
plist_set CFBundleIdentifier "$BUNDLE_ID"
plist_set LSMinimumSystemVersion "$MIN_MACOS"
plist_set LPRepositoryPath "$ROOT"
plutil -lint "$PLIST" >/dev/null

# ---- 图标（Pillow 优先，make-icon.py 自带纯标准库回退；都失败则复用缓存）----
ICON_ICNS="$CACHE/AppIcon.icns"
ICON_PNG="$CACHE/AppIcon-1024.png"
ICONSET="$CACHE/AppIcon.iconset"
make_icon() {
  local py=""
  for cand in "$ROOT/.venv/bin/python" "/opt/homebrew/bin/python3" "/usr/local/bin/python3" "$(command -v python3 || true)"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then py="$cand"; break; fi
  done
  [ -n "$py" ] || return 1
  "$py" "$MACOS_DIR/tools/make-icon.py" "$ICON_PNG" >/dev/null 2>&1 || return 1
  [ -f "$ICON_PNG" ] || return 1
  rm -rf "$ICONSET"; mkdir -p "$ICONSET"
  for spec in "16 16x16" "32 16x16@2x" "32 32x32" "64 32x32@2x" "128 128x128" "256 128x128@2x" "256 256x256" "512 256x256@2x" "512 512x512" "1024 512x512@2x"; do
    # shellcheck disable=SC2086
    set -- $spec
    sips -z "$1" "$1" "$ICON_PNG" --out "$ICONSET/icon_$2.png" >/dev/null 2>&1 || return 1
  done
  rm -f "$ICON_ICNS"
  iconutil -c icns "$ICONSET" -o "$ICON_ICNS" >/dev/null 2>&1 || return 1
  [ -f "$ICON_ICNS" ]
}
if make_icon; then
  echo "    图标已生成：AppIcon.icns"
elif [ -f "$ICON_ICNS" ]; then
  echo "    图标生成失败，复用缓存：$ICON_ICNS"
else
  echo "    ⚠ 未能生成 AppIcon.icns（App 仍可运行，只是 Finder 里没有图标）"
fi
if [ -f "$ICON_ICNS" ]; then cp "$ICON_ICNS" "$APP/Contents/Resources/AppIcon.icns"; fi

# ---- 内置后端（给没有仓库的用户兜底；开发时优先用 LPRepositoryPath 指向的仓库源码）----
echo "==> 打包内置后端"
mkdir -p "$APP/Contents/Resources/backend"
ditto "$ROOT/locpilot" "$APP/Contents/Resources/backend/locpilot"
if [ -d "$ROOT/web" ]; then ditto "$ROOT/web" "$APP/Contents/Resources/backend/web"; fi
for extra in README.md NOTICE.md LICENSE; do
  if [ -f "$ROOT/$extra" ]; then cp "$ROOT/$extra" "$APP/Contents/Resources/backend/$extra"; fi
done
find "$APP/Contents/Resources/backend" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$APP/Contents/Resources/backend" -name '*.pyc' -delete 2>/dev/null || true

# ---- ad-hoc 签名 ----
if codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1; then
  echo "==> ad-hoc 签名完成"
else
  echo "    ⚠ 签名失败：仍可本地运行（首次打开可能需要右键 → 打开）"
fi
codesign --verify "$APP" 2>&1 | tail -1 || true

# ---- 包结构自检（验收项 3）----
echo "==> 包结构自检"
plist_get() { plutil -extract "$1" raw -o - "$PLIST" 2>/dev/null || true; }
fail=0
check_eq() { # check_eq <label> <actual> <expected>
  if [ "$2" = "$3" ]; then echo "    ✓ $1 = $2"; else echo "    ✗ $1 = '$2'（期望 '$3'）" >&2; fail=1; fi
}
check_nonempty() { # check_nonempty <label> <actual>
  if [ -n "$2" ]; then echo "    ✓ $1 = $2"; else echo "    ✗ $1 缺失" >&2; fail=1; fi
}
check_eq "CFBundleIdentifier" "$(plist_get CFBundleIdentifier)" "$BUNDLE_ID"
check_nonempty "LSMinimumSystemVersion" "$(plist_get LSMinimumSystemVersion)"
check_eq "NSAppTransportSecurity.NSAllowsLocalNetworking" "$(plist_get NSAppTransportSecurity.NSAllowsLocalNetworking)" "true"
check_eq "LPRepositoryPath" "$(plist_get LPRepositoryPath)" "$ROOT"
if [ -f "$(plist_get LPRepositoryPath)/locpilot/__init__.py" ]; then
  echo "    ✓ 仓库源码可见：$(plist_get LPRepositoryPath)/locpilot/__init__.py"
else
  echo "    ✗ LPRepositoryPath 下找不到 locpilot/__init__.py" >&2; fail=1
fi
if [ -x "$APP/Contents/MacOS/LocPilot" ]; then echo "    ✓ Contents/MacOS/LocPilot 可执行"; else echo "    ✗ 缺少可执行文件" >&2; fail=1; fi
if [ -f "$APP/Contents/Resources/AppIcon.icns" ]; then echo "    ✓ Contents/Resources/AppIcon.icns"; else echo "    ✗ 缺少 AppIcon.icns" >&2; fail=1; fi
if [ -d "$APP/Contents/Resources/backend/locpilot" ]; then echo "    ✓ Contents/Resources/backend/locpilot"; else echo "    ✗ 缺少内置后端" >&2; fail=1; fi
[ "$fail" = "0" ] || { echo "✗ 包结构自检失败" >&2; exit 1; }

echo "==> 完成：$APP"
du -sh "$APP" | awk '{print "    体积: " $1}'

# ---------------------------------------------------------------------------
# 无界面自检（App 二进制 --selftest）
# mock 引擎 + 关自动连接：验收脚本绝不能碰用户真机
# ---------------------------------------------------------------------------
if [ "$SELFTEST" = "1" ]; then
  echo "==> 无界面自检：LocPilot --selftest（LOCPILOT_ENGINE=mock LOCPILOT_AUTOCONNECT=0）"
  LOCPILOT_ENGINE=mock LOCPILOT_AUTOCONNECT=0 "$APP/Contents/MacOS/LocPilot" --selftest
  echo "✓ 自检通过"
fi

# ---------------------------------------------------------------------------
# 启动冒烟：open 启动 → 进程存活 + 后端 /api/health 就绪 + 无新增崩溃报告
# ---------------------------------------------------------------------------
if [ "$SMOKE" = "1" ]; then
  echo "==> 启动冒烟测试（open，等价双击启动）"
  CRASH_DIR="$HOME/Library/Logs/DiagnosticReports"
  crash_list() { ls "$CRASH_DIR" 2>/dev/null | grep -i locpilot | sort || true; }
  count_lines() { printf '%s\n' "$1" | sed '/^$/d' | wc -l | tr -d ' '; }
  app_pids() { pgrep -f "$APP/Contents/MacOS/LocPilot" 2>/dev/null | sort -u || true; }

  CRASH_BEFORE_LIST="$(crash_list)"
  CRASH_BEFORE="$(count_lines "$CRASH_BEFORE_LIST")"
  EXISTING_PIDS="$(app_pids | tr '\n' ' ')"
  APP_PID=""

  smoke_fail() {
    echo "    ✗ $1" >&2
    local child
    for child in $(pgrep -P "$APP_PID" 2>/dev/null || true); do
      ps -o pid=,command= -p "$child" >&2 || true
    done
    # open 启动的进程日志进了系统日志，失败时再用直接启动抓一次 stderr 便于排障
    echo "    ↻ 直接启动 5 秒以捕获日志…" >&2
    LOCPILOT_ENGINE=mock LOCPILOT_AUTOCONNECT=0 "$APP/Contents/MacOS/LocPilot" >"$BUILD_DIR/smoke.log" 2>&1 &
    local dbg_pid=$!
    sleep 5
    kill -9 "$dbg_pid" 2>/dev/null || true
    wait "$dbg_pid" 2>/dev/null || true
    tail -25 "$BUILD_DIR/smoke.log" >&2 || true
    echo "    ✗ App 存活且后端就绪检查失败：$1" >&2
    exit 1
  }

  if ! open -n "$APP" --env LOCPILOT_ENGINE=mock --env LOCPILOT_AUTOCONNECT=0 2>"$BUILD_DIR/open.err"; then
    smoke_fail "open 启动失败：$(cat "$BUILD_DIR/open.err" 2>/dev/null)"
  fi

  # 只跟踪本次新起的实例，避免误伤其它已在运行的实例
  for _ in $(seq 1 80); do
    sleep 0.25
    for pid in $(app_pids); do
      case " $EXISTING_PIDS " in
        *" $pid "*) ;;
        *) APP_PID="$pid"; break 2 ;;
      esac
    done
  done
  [ -n "$APP_PID" ] || smoke_fail "open 之后 20 秒内没有出现新的 LocPilot 进程"
  echo "    新实例 PID ${APP_PID}（open 启动成功）"

  # 后端端口：优先看 App 子进程 argv（python3 -m locpilot serve --port N），回退 runtime.json
  backend_port() {
    local pid cmd port
    for pid in $(pgrep -P "$APP_PID" 2>/dev/null || true); do
      cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
      case "$cmd" in
        *"locpilot serve"*)
          port="$(printf '%s' "$cmd" | sed -n 's/.*--port[= ][[:space:]]*\([0-9][0-9]*\).*/\1/p')"
          [ -n "$port" ] && { printf '%s' "$port"; return 0; }
          ;;
      esac
    done
    port="$(plutil -extract port raw -o - "$HOME/Library/Application Support/LocPilot/runtime.json" 2>/dev/null || true)"
    [ -n "$port" ] && printf '%s' "$port"
  }

  healthy_port() { # healthy_port <port>：/api/health 返回 200 且含 true
    local body
    body="$(curl -sS --max-time 2 "http://127.0.0.1:$1/api/health" 2>/dev/null || true)"
    case "$body" in *true*) printf '%s' "$1"; return 0 ;; esac
    return 1
  }

  START_TS="$(date +%s)"
  PORT=""
  DEADLINE=$((START_TS + 40))
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    kill -0 "$APP_PID" 2>/dev/null || smoke_fail "App 进程在冒烟期间退出（疑似崩溃）"
    CAND="$(backend_port || true)"
    if [ -n "$CAND" ] && healthy_port "$CAND" >/dev/null; then PORT="$CAND"; break; fi
    sleep 0.5
  done
  if [ -z "$PORT" ]; then
    # 兜底：扫描端口区间（同时可能有别的实例在跑，取第一个健康的）
    for p in $(seq 8799 8815); do
      if healthy_port "$p" >/dev/null; then PORT="$p"; break; fi
    done
  fi
  [ -n "$PORT" ] || smoke_fail "40 秒内后端 /api/health 未就绪"

  # 验收：open 启动后存活 ≥ 8 秒（期间持续探活）
  while [ $(( $(date +%s) - START_TS )) -lt 8 ]; do
    kill -0 "$APP_PID" 2>/dev/null || smoke_fail "App 存活不足 8 秒"
    sleep 0.5
  done
  kill -0 "$APP_PID" 2>/dev/null || smoke_fail "App 存活不足 8 秒"
  ALIVE_SECS=$(( $(date +%s) - START_TS ))
  HEALTH_BODY="$(curl -sS --max-time 3 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
  STATUS_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/api/status" 2>/dev/null || true)"
  echo "    ✓ App 存活且后端就绪：PID ${APP_PID}，端口 ${PORT}，存活 ${ALIVE_SECS}s，/api/status HTTP ${STATUS_CODE}"
  echo "      /api/health = $HEALTH_BODY"

  # 清理：先优雅退出 App（后端带 --watch-parent 会自行退出），再兜底
  kill -TERM "$APP_PID" 2>/dev/null || true
  for _ in $(seq 1 16); do kill -0 "$APP_PID" 2>/dev/null || break; sleep 0.25; done
  kill -9 "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
  pkill -f "locpilot serve --port $PORT" 2>/dev/null || true

  # 崩溃报告守卫：启动前后对比 ~/Library/Logs/DiagnosticReports 里的 LocPilot 文件集合
  sleep 1
  CRASH_AFTER_LIST="$(crash_list)"
  CRASH_AFTER="$(count_lines "$CRASH_AFTER_LIST")"
  printf '%s\n' "$CRASH_BEFORE_LIST" | sed '/^$/d' | sort > "$BUILD_DIR/.crash-before"
  printf '%s\n' "$CRASH_AFTER_LIST"  | sed '/^$/d' | sort > "$BUILD_DIR/.crash-after"
  NEW_CRASHES="$(comm -13 "$BUILD_DIR/.crash-before" "$BUILD_DIR/.crash-after" || true)"
  if [ -n "$NEW_CRASHES" ]; then
    echo "    ✗ 本次启动产生了新的崩溃报告（${CRASH_BEFORE} -> ${CRASH_AFTER}）：" >&2
    printf '%s\n' "$NEW_CRASHES" >&2
    echo "    --- 崩溃摘要 ---" >&2
    head -40 "$CRASH_DIR/$(printf '%s' "$NEW_CRASHES" | head -1)" >&2 || true
    echo "    ✗ 无新增崩溃报告检查失败" >&2
    exit 1
  fi
  echo "    ✓ 无新增崩溃报告（累计 $CRASH_AFTER 份历史报告）"
fi

if [ "$RUN" = "1" ]; then
  echo "==> 启动 $APP"
  open "$APP"
fi
