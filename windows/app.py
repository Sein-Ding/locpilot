"""LocPilot Windows 外壳：pywebview 原生窗口 + 进程内 Python 后端。

为什么是"进程内"后端
--------------------
PyInstaller 打包后 `sys.executable` 是 LocPilot.exe 自己，不能再用
`python -m locpilot serve` 起子进程；而 Windwos 上额外附带一个后端 exe 又要
两套签名/两套崩溃处理。后端本身就是一个 ThreadingHTTPServer，直接在宿主进程里
起线程最稳：崩溃与退出都在同一个进程生命周期内。

窗口加载 http://127.0.0.1:<port>/ ，页面由后端静态服务提供（<root>/web）。
UI 与后端之间只走既有的 HTTP/SSE API，与 macOS 版共用同一套后端。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_NAME = "LocPilot"
WINDOW_SIZE = (1180, 760)
MIN_SIZE = (900, 620)
HEALTH_TIMEOUT = 20.0


def resource_root() -> Path:
    """仓库根目录；PyInstaller 打包后是解包目录（_MEIPASS）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def app_data_dir() -> Path:
    """每用户状态目录（Windows 用 %LOCALAPPDATA%，其它平台用 Application Support）。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    return Path.home() / "Library" / "Application Support" / APP_NAME


def redirect_stdio_when_frozen() -> Path:
    """PyInstaller `--windowed` 打包后没有控制台，sys.stdout/stderr 是 None，
    任何 print() 都会抛 AttributeError 直接崩掉（Windows 上表现为双击没反应）。
    把标准流重定向进日志文件：既不崩，也留下可以发给支持排查的记录。
    """
    if not getattr(sys, "frozen", False):
        return Path()
    home = app_data_dir()
    home.mkdir(parents=True, exist_ok=True)
    log_path = home / "locpilot.log"
    try:
        stream = open(str(log_path), "a", encoding="utf-8", buffering=1)
    except OSError:
        return Path()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return log_path


def prepare_env() -> Path:
    """准备状态目录与 import 路径，必须在 import locpilot 之前调用。

    优先使用已有的 LOCPILOT_HOME（--selftest 会指向临时目录，避免污染真实状态）。
    """
    home = Path(os.environ.get("LOCPILOT_HOME") or str(app_data_dir()))
    home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LOCPILOT_HOME", str(home))
    # 冻结后没有可用的 pymobiledevice3 命令行：worker 必须跑在进程内。
    # （引擎侧 inproc_enabled() 在 frozen+nt 时也会自动开，这里显式设置便于排查日志。）
    os.environ.setdefault("LOCPILOT_PMD3_INPROC", "1")
    root = resource_root()
    for entry in (str(root), str(root / "windows")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return home


def ui_dir() -> Path:
    """Windows UI 的实际目录。

    打包后 PyInstaller 把 windows/ui 解到 _MEIPASS/web，后端默认就指向那里；
    开发模式下后端默认仍指向仓库旧的 web/，所以要在构造 Server 之前把
    模块级 WEB_DIR 改到 windows/ui，否则 pywebview 加载的是旧网页
    （--selftest 还会误判 ui=ok）。
    """
    root = resource_root()
    if getattr(sys, "frozen", False):
        return root / "web"
    candidate = root / "windows" / "ui"
    if (candidate / "index.html").exists():
        return candidate
    return root / "web"   # 开发早期 windows/ui 还没产出时的兜底


def apply_web_dir(directory: Path) -> None:
    """把后端静态目录指向 Windows UI（只改模块属性，不修改 locpilot 包）。"""
    try:
        import locpilot.server as server_module
        server_module.WEB_DIR = directory
    except Exception as exc:                      # 不因静态目录问题阻止启动
        print("warn: 无法设置 WEB_DIR: %s" % exc, file=sys.stderr)


def start_backend():
    """进程内启动后端，返回 (server, session)。端口 0 = 由系统分配。"""
    apply_web_dir(ui_dir())
    from locpilot import config
    from locpilot.core.session import Session
    from locpilot.core.store import Store
    from locpilot.server import LocPilotServer

    settings = config.resolve_settings()
    store = Store(
        path=config.ensure_data_dir() / "state.json",
        history_limit=int(settings.get("history_limit", 100)),
    )
    session = Session(settings=settings, store=store, start_ticker=True)
    server = LocPilotServer(session, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, name="locpilot-http", daemon=True).start()
    return server, session


def http_get(url: str, timeout: float = 2.0):
    request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.getcode(), response.read()


def wait_health(port: int, timeout: float = HEALTH_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    url = "http://127.0.0.1:%d/api/health" % port
    while time.time() < deadline:
        try:
            code, body = http_get(url)
            if code == 200 and b"true" in body:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.15)
    return False


def write_error_page(home: Path, message: str) -> str:
    """后端起不来时给用户看得懂的页面，而不是白屏。"""
    page = home / "startup-error.html"
    html = """<!doctype html><meta charset="utf-8">
<title>LocPilot 启动失败</title>
<style>
  body{margin:0;background:#1c1c1e;color:#f2f2f7;font:14px/1.6 -apple-system,"Segoe UI",sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh}
  .card{max-width:560px;padding:28px 32px;background:#2c2c2e;border-radius:14px}
  h1{font-size:17px;margin:0 0 12px}
  .hint{color:#98989f;margin-top:14px}
  code{background:#3a3a3c;padding:2px 6px;border-radius:5px;font-size:12px}
</style>
<div class="card">
  <h1>LocPilot 后端没有启动起来</h1>
  <p>%s</p>
  <p class="hint">排查：确认已安装 Python 依赖（<code>pip install -r windows/requirements.txt</code>），
  或查看状态目录里的日志：<code>%s</code></p>
</div>
""" % (message, str(home))
    page.write_text(html, encoding="utf-8")
    return page.as_uri()


def acquire_single_instance(home: Path):
    """单实例锁：用户双击两次时不要开出两个后端/两个窗口。

    Windows 用命名 mutex（Local\\ = 当前登录会话，不需要管理员权限）；
    其他平台用文件锁（本地开发验证用）。拿不到锁返回 None。
    """
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\LocPilotSingleInstance")
        if not handle:
            return None
        if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return None
        return handle
    import fcntl
    handle = open(str(home / "instance.lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def run_gui(url: str, icon: Path) -> int:
    import webview

    webview.create_window(
        APP_NAME,
        url,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
        background_color="#1c1c1e",
        text_select=False,
    )
    kwargs = {}
    if icon.exists():
        kwargs["icon"] = str(icon)
    if sys.platform == "win32":
        kwargs["gui"] = "edgechromium"   # 用系统 WebView2，不额外打包浏览器内核
    webview.start(**kwargs)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="LocPilot Windows 外壳")
    parser.add_argument("--selftest", action="store_true", help="只启动后端做自检，不开窗口")
    parser.add_argument("--port", type=int, default=0, help="调试用：固定后端端口（0=自动）")
    args = parser.parse_args(argv)

    redirect_stdio_when_frozen()

    # 自检默认用临时状态目录：selftest 不该写用户的 state.json / 历史
    temp_home = None
    if args.selftest and not os.environ.get("LOCPILOT_HOME"):
        import tempfile

        temp_home = tempfile.TemporaryDirectory(prefix="locpilot-selftest-")
        os.environ["LOCPILOT_HOME"] = temp_home.name

    home = prepare_env()

    instance = None
    if not args.selftest:                       # 自检要能与正在运行的 App 并存
        instance = acquire_single_instance(home)
        if instance is None:
            print("已有 LocPilot 在运行，本次启动直接退出。")
            return 0

    server = None
    session = None
    try:
        server, session = start_backend()
        healthy = wait_health(server.port)
        ui_code, ui_body = (0, b"")
        try:
            ui_code, ui_body = http_get(server.url, timeout=5)
        except Exception:
            pass

        # 防"假绿"：不只看 / 返回了 HTML，还要确认服务的就是解析出的那个 index.html
        # （否则 dev 模式加载了仓库旧 web/ 也会被判成 ui=ok）。
        ui_ok = ui_code == 200 and b"<" in ui_body

        # 静态资源也要能取到（冻结包里 datas 映射错的话，首页能开但脚本/瓦片全 404）
        asset_results = {}
        for asset in ("/js/app.js", "/vendor/leaflet.js", "/styles.css"):
            try:
                code, body = http_get(server.url.rstrip("/") + asset, timeout=5)
                asset_results[asset] = code == 200 and len(body) > 0
            except Exception:
                asset_results[asset] = False
        if ui_ok and not all(asset_results.values()):
            ui_ok = False
            print("warn: 静态资源不可用: %s" % asset_results, file=sys.stderr)
        index_file = ui_dir() / "index.html"
        if ui_ok and index_file.exists():
            raw = index_file.read_bytes()
            start = raw.find(b"<")
            fingerprint = raw[start:start + 48] if start >= 0 else b""
            if fingerprint and fingerprint not in ui_body[:8192]:
                ui_ok = False
                print("warn: 静态指纹不匹配，服务可能不是 %s" % index_file, file=sys.stderr)

        if args.selftest:
            report = {
                "backend": "ok" if healthy else "failed",
                "ui": "ok" if ui_ok else "failed",
                "port": server.port,
                "home": str(home),
                "web_dir": str(ui_dir()),
                "assets": "ok" if all(asset_results.values()) else "failed",
                "python": sys.version.split()[0],
                "frozen": bool(getattr(sys, "frozen", False)),
            }
            for key, value in report.items():
                print("%s=%s" % (key, value))
            return 0 if (healthy and report["ui"] == "ok") else 1

        if not healthy:
            url = write_error_page(home, "等待 20 秒仍没有响应，后端可能缺少依赖或端口被占用。")
        elif not ui_ok:
            url = write_error_page(home, "后端已就绪，但静态 UI 没有找到（<root>/web 缺失）。")
        else:
            url = server.url
        icon = resource_root() / "windows" / "assets" / "locpilot.ico"
        return run_gui(url, icon)
    finally:
        if session is not None:
            try:
                session.shutdown()
            except Exception:
                pass
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
        if instance is not None:
            try:
                if sys.platform != "win32":
                    instance.close()
            except Exception:
                pass
        if temp_home is not None:
            try:
                temp_home.cleanup()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
