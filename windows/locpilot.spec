# -*- mode: python ; coding: utf-8 -*-
"""LocPilot Windows 版 PyInstaller 打包配置（默认 onefile，可选 onedir）。

入口脚本是 windows/app.py（pywebview 原生窗口 + 进程内后端）。不建议手工拼
pyinstaller 参数，请用 windows/build.ps1：它负责虚拟环境、依赖安装、版本号、
图标生成、打包与冻结包自检。

    windows\\build.ps1                  # onefile -> windows\\dist\\LocPilot.exe
    windows\\build.ps1 -OneDir          # onedir  -> windows\\dist\\LocPilot\\LocPilot.exe
    windows\\build.ps1 -NoSelfTest      # 跳过构建后的 --selftest

build.ps1 的等价命令（仓库根，venv 已就绪）：

    windows\\.venv\\Scripts\\python.exe -m PyInstaller --noconfirm ^
        --distpath windows\\dist --workpath windows\\.build\\pyinstaller ^
        windows\\locpilot.spec

环境变量（通常由 build.ps1 设置，也可手工覆盖）：

    LOCPILOT_BUILD_VERSION      exe 文件属性版本号；缺省从 locpilot/__init__.py 读取
    LOCPILOT_WIN_ONEDIR=1       改用 onedir 模式（调试 / 规避杀软误报 onefile）
    LOCPILOT_WIN_NO_ICON=1      跳过图标生成（Pillow 不可用时也会自动跳过）
    LOCPILOT_WIN_CONSOLE=1      调试用：保留控制台（默认无控制台窗口）
    LOCPILOT_HIDDEN_IMPORTS     额外 hiddenimports，逗号分隔（排障用）

资源映射（打包后 _MEIPASS 内的布局，必须与下面两处契约一致）：
    windows/ui/                    -> web/                后端静态站点根
                                                           （server.WEB_DIR；app.ui_dir() 冻结时也取 <root>/web）
    locpilot/engine/pmd3_worker.py -> locpilot/engine/    非冻结 / 子进程兜底时由外部 python 执行
    locpilot.ico（生成或已有）      -> windows/assets/     窗口与 exe 图标（app.py 自动探测）
    LICENSE / NOTICE.md            -> 包根目录
"""

import os
import re
import subprocess
import sys
from pathlib import Path

APP_NAME = "LocPilot"
WINDOWS_DIR = Path(SPECPATH).resolve()          # windows/
ROOT = WINDOWS_DIR.parent                       # 仓库根
APP_SCRIPT = WINDOWS_DIR / "app.py"
REQUIREMENTS = WINDOWS_DIR / "requirements.txt"
UI_DIR = WINDOWS_DIR / "ui"
WEB_FALLBACK = ROOT / "web"
WORK_DIR = Path(workpath) / "win-build"         # 构建期临时产物（随 workpath 一起被 -Clean 清掉）
ONEDIR = os.environ.get("LOCPILOT_WIN_ONEDIR", "").strip().lower() in ("1", "true", "yes", "on")
CONSOLE = os.environ.get("LOCPILOT_WIN_CONSOLE", "").strip() == "1"


def log(message):
    print("[spec] " + message)


def read_version():
    """从 locpilot/__init__.py 读版本号：spec 不 import locpilot（避免依赖运行期路径）。"""
    candidates = [ROOT / "locpilot" / "__init__.py"]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", text)
        if match:
            return match.group(1).strip()
    log("WARNING: 未能从 locpilot/__init__.py 解析版本号，回退 0.0.0")
    return "0.0.0"


def describe_runtime():
    """打印解释器 / 依赖版本，排障时一眼能看出构建环境。"""
    log("Python: %s (%d-bit) @ %s" % (sys.version.split()[0], 64 if sys.maxsize > 2 ** 32 else 32, sys.executable))
    for package in ("PyInstaller", "webview", "pymobiledevice3", "PIL"):
        try:
            module = __import__(package)
            version = getattr(module, "__version__", None) or getattr(module, "VERSION", None) or "?"
        except Exception as exc:                 # 依赖缺失不在这里失败，由 build.ps1 提前给出可读错误
            version = "缺失(%s)" % type(exc).__name__
        log("依赖 %s: %s" % (package, version))


def collect_package(package, datas, binaries, hiddenimports):
    """collect_all 等价于 PyInstaller 的 --collect-all：子模块 + 数据文件 + 动态库。

    引擎（pymobiledevice3）和 pywebview 都大量使用函数内延迟 import / 数据文件，
    只靠静态分析容易漏（Windows 上表现为运行期 ImportError 或窗口空白）。
    """
    try:
        from PyInstaller.utils.hooks import collect_all
        package_datas, package_binaries, package_hiddenimports = collect_all(package)
    except Exception as exc:
        log("WARNING: collect_all(%s) 失败，继续靠静态分析：%s" % (package, exc))
        return
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)
    log(
        "collect_all(%s): datas=%d binaries=%d hiddenimports=%d"
        % (package, len(package_datas), len(package_binaries), len(package_hiddenimports))
    )


def resolve_ui_source():
    """windows/ui 优先；缺失时回退仓库 web/，两者都没有就明确失败。"""
    if (UI_DIR / "index.html").is_file():
        return UI_DIR
    if (WEB_FALLBACK / "index.html").is_file():
        log("WARNING: windows/ui/index.html 不存在，回退打包仓库 web/ —— 这不是 Windows 版 UI，仅供早期联调")
        return WEB_FALLBACK
    raise SystemExit(
        "[spec] 找不到 UI 目录：%s 或 %s 都不存在。Windows 版需要 windows/ui/index.html。"
        % (UI_DIR, WEB_FALLBACK)
    )


def build_icon():
    """生成 .ico：优先 windows/assets/locpilot.ico；否则用 macos/tools/make-icon.py + Pillow 生成。"""
    if os.environ.get("LOCPILOT_WIN_NO_ICON", "").strip() == "1":
        log("图标: 已按 LOCPILOT_WIN_NO_ICON=1 跳过")
        return None

    source = WINDOWS_DIR / "assets" / "locpilot.ico"
    if source.is_file():
        log("图标: 使用 %s" % source)
        return source

    maker = ROOT / "macos" / "tools" / "make-icon.py"
    if not maker.is_file():
        log("WARNING: 没有 windows/assets/locpilot.ico，也找不到 %s；exe 将使用默认图标" % maker)
        return None

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    png = WORK_DIR / "locpilot-1024.png"
    target = WORK_DIR / "locpilot.ico"
    try:
        result = subprocess.run(
            [sys.executable, str(maker), str(png)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
        )
    except Exception as exc:
        log("WARNING: 运行 make-icon.py 失败（%s）；exe 将使用默认图标" % exc)
        return None
    if result.returncode != 0 or not png.is_file():
        detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()[-300:]
        log("WARNING: make-icon.py 生成 PNG 失败（exit %s）：%s" % (result.returncode, detail))
        return None

    try:
        from PIL import Image           # 仅构建期需要，不打进 exe
    except ImportError:
        log("WARNING: 未安装 Pillow，无法把 PNG 转成 .ico（pip install pillow 后重试）；exe 将使用默认图标")
        return None
    try:
        image = Image.open(str(png))
        image.save(
            str(target),
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
    except Exception as exc:
        log("WARNING: PNG 转 .ico 失败（%s）；exe 将使用默认图标" % exc)
        return None
    log("图标: 已生成 %s" % target)
    return target


def build_version_file(version):
    """生成 Windows 版本资源文本（PyInstaller version= 参数用），保证文件属性里能看到版本。"""
    numbers = [int(part) for part in re.findall(r"\d+", version)[:4]]
    while len(numbers) < 4:
        numbers.append(0)
    filevers = "(%d, %d, %d, %d)" % tuple(numbers)
    escaped = version.replace("\\", "\\\\").replace("'", "\\'")
    lines = [
        "# UTF-8",
        "VSVersionInfo(",
        "  ffi=FixedFileInfo(",
        "      filevers=" + filevers + ",",
        "      prodvers=" + filevers + ",",
        "      mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)",
        "  ),",
        "  kids=[",
        "    StringFileInfo([",
        "      StringTable('040904B0', [",
        "        StringStruct('CompanyName', 'LocPilot'),",
        "        StringStruct('FileDescription', 'LocPilot - iOS 虚拟定位工具'),",
        "        StringStruct('FileVersion', '" + escaped + "'),",
        "        StringStruct('InternalName', 'LocPilot'),",
        "        StringStruct('LegalCopyright', 'LocPilot contributors'),",
        "        StringStruct('OriginalFilename', 'LocPilot.exe'),",
        "        StringStruct('ProductName', 'LocPilot'),",
        "        StringStruct('ProductVersion', '" + escaped + "'),",
        "      ])",
        "    ]),",
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])",
        "  ]",
        ")",
    ]
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    path = WORK_DIR / "version_info.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("版本资源: %s" % path)
    return path


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------
if not APP_SCRIPT.is_file():
    raise SystemExit("[spec] 找不到入口脚本：%s" % APP_SCRIPT)
if not (ROOT / "locpilot" / "__init__.py").is_file():
    raise SystemExit("[spec] 找不到 locpilot 包：%s" % (ROOT / "locpilot"))

VERSION = os.environ.get("LOCPILOT_BUILD_VERSION", "").strip() or read_version()
ui_source = resolve_ui_source()
ui_file_count = sum(1 for path in ui_source.rglob("*") if path.is_file())

describe_runtime()
log("模式: %s / %s" % ("onedir" if ONEDIR else "onefile", "console(调试)" if CONSOLE else "windowed(无控制台)"))
log("版本: %s" % VERSION)
log("UI: %s -> web/（%d 个文件）" % (ui_source, ui_file_count))
if REQUIREMENTS.is_file():
    log("运行时依赖清单: %s" % REQUIREMENTS)

datas = [(str(ui_source), "web")]
binaries = []
hiddenimports = [
    # pywebview 在 Windows 上通过 winforms + EdgeChromium(WebView2) 工作；
    # clr 由 pythonnet 提供，命中的 hook 会把 Python.Runtime.dll 一起收进来。
    "clr",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    # 引擎进程内模式直接 import 这些服务；显式列出避免不同 PyInstaller 版本分析差异。
    "pymobiledevice3.usbmux",
    "pymobiledevice3.lockdown",
    "pymobiledevice3.services.dvt.instruments.dvt_provider",
    "pymobiledevice3.services.dvt.instruments.location_simulation",
    "pymobiledevice3.services.simulate_location",
    "pymobiledevice3.remote.remote_service_discovery",
    "pymobiledevice3.remote.userspace_tunnel",
    "pymobiledevice3.tunneld.api",
]

extra_hidden = [
    item.strip()
    for item in os.environ.get("LOCPILOT_HIDDEN_IMPORTS", "").replace(";", ",").split(",")
    if item.strip()
]
if extra_hidden:
    log("额外 hiddenimports: %s" % ", ".join(extra_hidden))
    hiddenimports.extend(extra_hidden)

collect_package("webview", datas, binaries, hiddenimports)
collect_package("pymobiledevice3", datas, binaries, hiddenimports)

# 子进程兜底的 worker 脚本必须作为真实文件存在：冻结后
# pmd3.py 的 __file__ 是 <_MEIPASS>/locpilot/engine/pmd3.py，
# WORKER_SCRIPT 会解析到同目录下的 pmd3_worker.py。
worker_script = ROOT / "locpilot" / "engine" / "pmd3_worker.py"
if worker_script.is_file():
    datas.append((str(worker_script), "locpilot/engine"))
    log("子进程 worker 脚本: %s" % worker_script)
else:
    log("WARNING: 找不到 %s（进程内模式不依赖它，但命令行/子进程兜底会用到）" % worker_script)

for name in ("LICENSE", "NOTICE.md"):
    extra = ROOT / name
    if extra.is_file():
        datas.append((str(extra), "."))

icon_path = build_icon()
if icon_path is not None:
    datas.append((str(icon_path), "windows/assets"))

version_file = build_version_file(VERSION)

# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------
analysis = Analysis(
    [str(APP_SCRIPT)],
    pathex=[str(ROOT), str(WINDOWS_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe_options = dict(
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
if sys.platform == "win32":
    if icon_path is not None:
        exe_options["icon"] = str(icon_path)
    if version_file is not None:
        exe_options["version"] = str(version_file)
else:
    log("非 Windows 构建：跳过 exe 图标/版本资源参数（本机只做交叉分析与打包流程验证）")

if ONEDIR:
    exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, **exe_options)
    coll = COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=APP_NAME,
    )
    log("输出: %s" % (Path(DISTPATH) / APP_NAME))
else:
    exe = EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, **exe_options)
    log("输出: %s" % (Path(DISTPATH) / (APP_NAME + (".exe" if sys.platform == "win32" else ""))))
