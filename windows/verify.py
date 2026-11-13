"""Windows 版端到端冒烟：后端 + 静态 UI，跨平台无需 GUI。

用法（仓库根）：
    .venv/bin/python windows/verify.py            # macOS 本地验证
    python windows\verify.py                       # Windows 上验证

覆盖：进程内启动后端 → /api/health → /api/status → mock 连接 → teleport →
      /api/status 位置一致 → 根页面由 windows/ui 提供 → /api/clear → 干净退出。
退出码 0 = 全绿。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

results = []          # (name, ok, detail)


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s %s%s" % ("OK  " if ok else "FAIL", name, ("  " + detail) if detail else ""))
    return ok


def _read(response):
    return json.loads(response.read().decode() or "{}")


def post(url, payload):
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.getcode(), _read(response)
    except urllib.error.HTTPError as exc:                     # 回显后端错误体，便于定位
        return exc.code, {"error": exc.read().decode(errors="replace")[:400]}


def get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.getcode(), _read(response)
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode(errors="replace")[:400]}


def main() -> int:
    import app as shell          # windows/app.py

    os.environ.setdefault("LOCPILOT_ENGINE", "mock")
    # 用独立临时状态目录：既避免污染真实状态，也让"沙箱内跑测试"不会因
    # 写不了 ~/Library/Application Support 而误报 500。
    temp_home = None
    if not os.environ.get("LOCPILOT_HOME"):
        import tempfile

        temp_home = tempfile.TemporaryDirectory(prefix="locpilot-verify-")
        os.environ["LOCPILOT_HOME"] = temp_home.name
    shell.prepare_env()
    check("静态目录解析", shell.ui_dir().exists(), str(shell.ui_dir()))

    server = session = None
    try:
        server, session = shell.start_backend()
        check("后端健康检查", shell.wait_health(server.port), "port=%d" % server.port)

        base = "http://127.0.0.1:%d" % server.port
        code, status = get_json(base + "/api/status")
        check("GET /api/status", code == 200 and "engine" in status)

        code, info = post(base + "/api/connect", {"engine": "mock"})
        check("POST /api/connect (mock)", code == 200 and info.get("engine") == "mock",
              "" if code == 200 else str(info.get("error", ""))[:200])

        lat, lon = 25.0339, 121.5645
        code, moved = post(base + "/api/teleport", {"lat": lat, "lon": lon})
        position = (moved.get("position") or {})
        ok_pos = abs(position.get("lat", 0) - lat) < 1e-6 and abs(position.get("lon", 0) - lon) < 1e-6
        check("POST /api/teleport 回包位置一致", code == 200 and ok_pos,
              "" if code == 200 else str(moved.get("error", ""))[:200])

        _, status2 = get_json(base + "/api/status")
        pos2 = status2.get("position") or {}
        check("GET /api/status 位置落库", abs(pos2.get("lat", 0) - lat) < 1e-6)

        with urllib.request.urlopen(base + "/", timeout=10) as response:
            body = response.read()
        index = shell.ui_dir() / "index.html"
        fingerprint = index.read_bytes().split(b"<", 1)[-1][:48] if index.exists() else b""
        check("根页面由 UI 目录提供", response.getcode() == 200 and (not fingerprint or fingerprint in body))

        code, _ = post(base + "/api/clear", {})
        check("POST /api/clear", code == 200)
    finally:
        if temp_home is not None:
            try:
                temp_home.cleanup()
            except Exception:
                pass
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

    failed = [name for name, ok, _ in results if not ok]
    print()
    if failed:
        print("失败 %d 项：%s" % (len(failed), ", ".join(failed)))
        return 1
    print("全部通过：%d 项" % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
