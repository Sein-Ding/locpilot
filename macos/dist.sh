#!/usr/bin/env bash
# 分发打包：把 LocPilot 打成可拷给其它 Apple Silicon Mac 的 DMG。
#
#   bash macos/dist.sh            # 构建 + 清洗 + 出 DMG + 校验和
#   bash macos/dist.sh --no-build # 用现有 .app（调试用）
#
# 与开发构建的区别：
#   1. 移除 Info.plist 里的 LPRepositoryPath —— 那是本机绝对路径，既泄露用户名，
#      也让分发包在别人机器上找不到仓库。移除后 App 会用包内 Resources/backend 兜底。
#   2. 重新 ad-hoc 签名（改过 Info.plist 必须重签，否则签名失效）。
#   3. 产出带 Applications 软链与首次打开说明的 DMG。
set -euo pipefail

MACOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$MACOS_DIR/.." && pwd)"
APP="$MACOS_DIR/build/LocPilot.app"
DIST="$MACOS_DIR/build/dist"
STAGE="$DIST/dmg-root"
SKIP_BUILD=0
[ "${1:-}" = "--no-build" ] && SKIP_BUILD=1

if [ "$SKIP_BUILD" = "0" ]; then
  echo "==> 构建（复用开发构建脚本）"
  bash "$MACOS_DIR/build.sh"
fi

# 版本号必须在构建之后再读：首次构建前 .app 不存在，PlistBuddy 会把
# "File Doesn't Exist, Will Create: …" 打到 stdout，混进产物文件名里。
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist" 2>/dev/null | head -1)"
case "$VERSION" in
  [0-9]*) ;;
  *) VERSION="1.0.0" ;;
esac
DMG="$DIST/LocPilot-${VERSION}-arm64.dmg"

echo "==> 清洗分发产物"
# 1) 去掉本机绝对路径
/usr/libexec/PlistBuddy -c 'Delete :LPRepositoryPath' "$APP/Contents/Info.plist" 2>/dev/null || true
if /usr/libexec/PlistBuddy -c 'Print :LPRepositoryPath' "$APP/Contents/Info.plist" >/dev/null 2>&1; then
  echo "    ✗ LPRepositoryPath 仍存在" >&2; exit 1
fi
echo "    ✓ 已移除 LPRepositoryPath（不再依赖本机仓库路径）"

# 2) 清掉可能带上的扩展属性（下载后不影响 Gatekeeper 判断，但发行包应干净）
xattr -cr "$APP" 2>/dev/null || true

# 3) 改过 Info.plist 必须重新签名
codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1
codesign --verify --deep --strict "$APP" >/dev/null 2>&1 && echo "    ✓ ad-hoc 签名校验通过" || { echo "    ✗ 签名校验失败" >&2; exit 1; }

echo "==> 分发前自检"
ARCH="$(lipo -info "$APP/Contents/MacOS/LocPilot" | awk '{print $NF}')"
[ "$ARCH" = "arm64" ] && echo "    ✓ 架构 arm64（Apple Silicon 原生）" || { echo "    ✗ 架构异常: $ARCH" >&2; exit 1; }
[ -d "$APP/Contents/Resources/backend/locpilot" ] && echo "    ✓ 包内已带后端源码（无仓库环境兜底）" || { echo "    ✗ 缺少包内后端" >&2; exit 1; }
if grep -rIl "/Users/seinding" "$APP/Contents" 2>/dev/null | head -3 | grep -q .; then
  echo "    ✗ 包内仍含本机用户名路径：" >&2
  grep -rIl "/Users/seinding" "$APP/Contents" 2>/dev/null | head -5 >&2
  exit 1
fi
echo "    ✓ 包内不含本机用户名路径"

echo "==> 组装 DMG"
rm -rf "$STAGE"; mkdir -p "$STAGE"
ditto "$APP" "$STAGE/LocPilot.app"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/安装说明.txt" <<'NOTE'
LocPilot —— iOS 虚拟定位工具（Apple Silicon 原生）

【安装】
把 LocPilot.app 拖到右侧的 Applications 文件夹。

【首次打开】
本版本为 ad-hoc 签名（未做 Apple 公证），首次打开请：
  · 右键点击 LocPilot.app → 打开 → 在弹窗里再点"打开"；
  · 或执行一次：xattr -dr com.apple.quarantine /Applications/LocPilot.app

【安装定位引擎（改真机定位必需）】
App 首次运行只会拉起内置后端；要驱动真机，需要 pymobiledevice3：
  菜单栏「引擎 → 安装 / 修复定位引擎…」，按提示安装（约 40MB，无需 sudo）。
没有引擎时仍可用 mock 虚拟设备体验界面。

【使用】
1. 用 USB 连接 iPhone，手机上点"信任此电脑"，并开启开发者模式；
2. 点右上角手机图标连接设备；
3. 在地图上点任意位置 —— 大头针落下后定位即改到那里；
4. 点右上角"恢复真实定位"回到真实 GPS。

【要求】macOS 14 或更高；Apple Silicon（M 系列）Mac。
【许可】GPL-3.0，第三方组件见包内 NOTICE.md。
NOTE

# DMG 优先；若环境不允许创建磁盘镜像（沙箱/无权限），自动回落 ZIP。
# 两条路都能拿到同样的 .app，ZIP 解压后拖进 Applications 即可。
rm -f "$DMG" "$DIST/LocPilot-${VERSION}-arm64.zip"
ARTIFACT=""
if hdiutil create -volname "LocPilot" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null 2>&1; then
  ARTIFACT="$DMG"
  echo "    ✓ 已生成 DMG：$DMG"
else
  # 回落：直接打包 .app 本体 —— 压缩包顶层就是 LocPilot.app，解压后拖进 Applications 即可。
  # 用 --norsrc --noextattr 去掉 AppleDouble/资源叉噪声，签名靠包内 _CodeSignature 仍然有效
  # （下面的“分发级验证”会解压后复核签名与自检，签名一旦失效这里就会报错）。
  ZIP="$DIST/LocPilot-${VERSION}-arm64.zip"
  rm -f "$ZIP"
  ditto -c -k --norsrc --noextattr --keepParent "$APP" "$ZIP"
  # 安装说明作为独立文件放在压缩包旁边，不塞进 zip 里，避免多一层目录
  cp "$STAGE/安装说明.txt" "$DIST/安装说明.txt"
  ARTIFACT="$ZIP"
  echo "    ! 无法创建磁盘镜像（沙箱限制），已回落为 ZIP：$ZIP"
fi

echo
echo "==> 校验和"
shasum -a 256 "$ARTIFACT" | tee "$ARTIFACT.sha256"
echo
echo "==> 产物"
ls -lh "$ARTIFACT" | awk '{print "    " $9 "  " $5}'
echo
echo "==> 分发级验证（解压到临时目录，模拟别人的 Mac）"
VERIFY_DIR="$(mktemp -d)"
if [[ "$ARTIFACT" == *.zip ]]; then
  ditto -x -k "$ARTIFACT" "$VERIFY_DIR"
else
  MP="$(hdiutil attach "$ARTIFACT" -nobrowse -readonly | tail -1 | awk '{print $3}')"
  ditto "$MP/LocPilot.app" "$VERIFY_DIR/LocPilot.app"
  hdiutil detach "$MP" >/dev/null 2>&1 || true
fi
VERIFY_APP="$(find "$VERIFY_DIR" -maxdepth 2 -name 'LocPilot.app' | head -1)"
if [ -z "$VERIFY_APP" ]; then echo "    ✗ 解压后找不到 LocPilot.app" >&2; rm -rf "$VERIFY_DIR"; exit 1; fi
codesign --verify --strict "$VERIFY_APP" >/dev/null 2>&1 && echo "    ✓ 解压后签名仍然有效" || { echo "    ✗ 解压后签名失效" >&2; exit 1; }
# 注意：这里**不**打 quarantine 标记。真实下载的副本会被系统加上 quarantine 并由 Gatekeeper 拦一次，
# 那是预期行为（见 安装说明.txt 的"首次打开"）。自动校验要验的是"干净解压能否跑起来"。
"$VERIFY_APP/Contents/MacOS/LocPilot" --selftest > "$VERIFY_DIR/selftest.json" 2>"$VERIFY_DIR/selftest.err" &
SPID=$!
for _ in $(seq 1 60); do sleep 1; kill -0 $SPID 2>/dev/null || break; done
kill -9 $SPID 2>/dev/null || true
if grep -q '"healthy"[[:space:]]*:[[:space:]]*true' "$VERIFY_DIR/selftest.json" 2>/dev/null; then
  echo "    ✓ 独立副本可自检通过（后端健康）"
  grep -o '"engines"[^,]*' "$VERIFY_DIR/selftest.json" | head -1 | sed 's/^/      /'
else
  echo "    ✗ 独立副本自检失败：" >&2; tail -3 "$VERIFY_DIR/selftest.json" "$VERIFY_DIR/selftest.err" 2>/dev/null >&2
fi
pkill -9 -f "locpilot serve" 2>/dev/null || true
rm -rf "$VERIFY_DIR"
