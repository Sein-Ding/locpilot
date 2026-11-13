#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LocPilot Windows 探针 #1：主机就绪检查（不需要连接 iPhone，不需要管理员）。

用法（在仓库根目录，Windows 上）：

    python windows\probe\probe-windows.py
    python windows\probe\probe-windows.py --json
    python windows\probe\probe-windows.py --goios C:\tools\go-ios\ios.exe

检查内容
--------
1. 平台 / 解释器（是否 Windows、是否 64 位、是否 PyInstaller 冻结）
2. 管理员权限（默认 userspace 隧道不需要；只有内核隧道 tunneld 降级才需要）
3. Apple Mobile Device Service 状态（pmd3 与 go-ios 都依赖它）
4. 该服务来自经典 iTunes 还是 Microsoft Store "Apple Devices"（后者不支持 Wi-Fi 发现）
5. usbmux 端口 127.0.0.1:27015 是否可连
6. pymobiledevice3 / pmd-pytcp / pytun-pmd3 是否可用（默认引擎）
7. go-ios（ios.exe）能否运行并输出版本（降级引擎）
8. 端口 60105（go-ios agent）/ 49151（pmd3 tunneld）是否已有服务在跑
9. 用 pymobiledevice3 的 usbmux 枚举一次设备（能连线时才有内容）

退出码
------
0 = 关键项全部通过（可以继续跑 probe-device.py）
1 = 至少一项 FAIL（按 hint 修）
2 = 当前不是 Windows（其余检查仅供参考，不代表 Windows 上可用）

本脚本只读：不安装任何东西、不修改服务、不写文件。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

# macOS 开发机用 LibreSSL 跑 pmd3 时会刷 urllib3 警告；Windows 的 OpenSSL 不会。只忽略这一条。
import warnings

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

IS_WINDOWS = sys.platform == "win32"
IS_FROZEN = bool(getattr(sys, "frozen", False))
PROBE_VERSION = 1

RESULTS: List[Dict[str, Any]] = []


def record(name: str, status: str, detail: str = "", hint: str = "") -> None:
    """status: PASS / WARN / FAIL / SKIP。"""
    RESULTS.append({"name": name, "status": status, "detail": detail, "hint": hint})


def _run(argv: List[str], timeout: float = 20.0) -> Dict[str, Any]:
    """跑一个短命令并带回显；Windows 上不弹黑框。"""
    kwargs: Dict[str, Any] = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(argv, **kwargs)
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        return {"argv": argv, "rc": proc.returncode, "out": out, "err": err}
    except FileNotFoundError as exc:
        return {"argv": argv, "rc": 127, "out": "", "err": "命令不存在: %s" % (exc,)}
    except subprocess.TimeoutExpired:
        return {"argv": argv, "rc": 124, "out": "", "err": "超时 %.0fs" % timeout}
    except OSError as exc:
        return {"argv": argv, "rc": 126, "out": "", "err": str(exc)}


def _tcp_open(host: str, port: int, timeout: float = 0.8) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _http_status(url: str, timeout: float = 1.0) -> Optional[int]:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.getcode())
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return None


# --- 1. 平台与解释器 -------------------------------------------------------
def check_platform() -> None:
    detail = "%s / %s / Python %s" % (platform.platform(), platform.machine(), platform.python_version())
    if IS_WINDOWS:
        record("平台", "PASS", detail)
    else:
        record(
            "平台",
            "FAIL",
            detail,
            "本探针的目标平台是 Windows；当前机器只能验证脚本逻辑，不能验证 Windows 行为。",
        )
    bits = 64 if sys.maxsize > 2 ** 32 else 32
    record("解释器位数", "PASS" if bits == 64 else "WARN", "%d-bit；frozen=%s" % (bits, IS_FROZEN))
    if IS_FROZEN:
        record(
            "解释器形态",
            "WARN",
            "当前是 PyInstaller 冻结进程（sys.executable=%s）" % sys.executable,
            "冻结进程不能再用 sys.executable 起 pmd3_worker.py 子进程；引擎必须走进程内适配，见 docs/windows-engine.md。",
        )


# --- 2. 管理员 ------------------------------------------------------------
def check_admin() -> None:
    is_admin: Optional[bool] = None
    if IS_WINDOWS:
        try:
            import ctypes

            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_admin = None
    else:
        try:
            is_admin = os.geteuid() == 0
        except AttributeError:
            is_admin = None
    if is_admin is None:
        record("管理员权限", "SKIP", "无法判断")
        return
    record(
        "管理员权限",
        "WARN" if is_admin else "PASS",
        "是（当前终端已提权）" if is_admin else "否（默认路径不需要）",
        "除非要跑内核隧道 tunneld 降级，userspace 隧道不需要管理员。",
    )


# --- 3/4. Apple Mobile Device Service ------------------------------------
_AMDS = "Apple Mobile Device Service"


def check_amds() -> None:
    if not IS_WINDOWS:
        record("Apple Mobile Device Service", "SKIP", "非 Windows")
        return
    query = _run(["sc.exe", "query", _AMDS])
    if query["rc"] != 0:
        record(
            "Apple Mobile Device Service",
            "FAIL",
            "服务不存在或查询失败: %s" % (query["err"].strip()[:180] or query["out"].strip()[:180] or "sc.exe rc=%d" % query["rc"]),
            "安装 Apple 官方 iTunes 经典版（https://www.apple.com/itunes/download/win64），它提供该服务；"
            "Microsoft Store 的 Apple Devices 也能连 USB，但不支持 Wi-Fi 发现。",
        )
        return
    state = None
    for line in query["out"].splitlines():
        if "STATE" in line.upper():
            parts = line.replace(":", " ").split()
            numbers = [p for p in parts if p.isdigit()]
            if numbers:
                state = int(numbers[0])
            break
    running = state == 4
    record(
        "Apple Mobile Device Service",
        "PASS" if running else "FAIL",
        "STATE=%s（%s）" % (state, "RUNNING" if running else "未运行 / 未知"),
        "" if running else "在「服务」中启动 Apple Mobile Device Service，或重装 iTunes。",
    )

    config = _run(["sc.exe", "qc", _AMDS])
    if config["rc"] == 0:
        path = ""
        for line in config["out"].splitlines():
            if "BINARY_PATH_NAME" in line.upper():
                path = line.split(":", 1)[-1].strip().strip('"')
                break
        lowered = path.lower()
        if "windowsapps" in lowered:
            record(
                "Apple 服务安装形态",
                "WARN",
                "Microsoft Store 版 Apple Devices: %s" % path,
                "该服务不支持 Wi-Fi 发现（pmd3#1968）；需要无线连接请改装经典 iTunes。",
            )
        elif path:
            record("Apple 服务安装形态", "PASS", "经典安装包: %s" % path)


# --- 5. usbmux 端口 -------------------------------------------------------
def check_usbmux_port() -> None:
    if not IS_WINDOWS:
        record("usbmux TCP 127.0.0.1:27015", "SKIP", "非 Windows（Windows 上才是 TCP 27015）")
        return
    open_now = _tcp_open("127.0.0.1", 27015)
    record(
        "usbmux TCP 127.0.0.1:27015",
        "PASS" if open_now else "FAIL",
        "可连接" if open_now else "连接被拒绝",
        "" if open_now else "这是 Windows 上 usbmuxd（Apple Mobile Device Service）的固定端口；服务没跑时 pmd3 / go-ios 都枚举不到设备。",
    )


# --- 6. pymobiledevice3 ---------------------------------------------------
def _module_version() -> Optional[str]:
    try:
        from importlib.metadata import version

        return version("pymobiledevice3")
    except Exception:
        return None


def _module_importable(name: str) -> Optional[bool]:
    try:
        import importlib.util

        return importlib.util.find_spec(name) is not None
    except Exception:
        return None


def check_pmd3() -> None:
    version = _module_version()
    if version is None:
        record(
            "pymobiledevice3",
            "FAIL",
            "当前解释器里未安装",
            "在打包用的运行时里 pip install 'pymobiledevice3>=11.23.0'；Windows 默认引擎依赖它（GPL-3.0，项目本身 GPL-3.0）。",
        )
        return
    record("pymobiledevice3", "PASS", "版本 %s（当前解释器）" % version)
    for name, purpose in (("pmd_pytcp", "userspace 隧道网络栈（无管理员路径必需）"), ("pytun_pmd3", "内核隧道 / Wintun（仅管理员降级路径需要）")):
        found = _module_importable(name)
        record(name, "PASS" if found else "WARN", "%s：%s" % (purpose, "已安装" if found else "未安装"))


# --- 7. go-ios ------------------------------------------------------------
def find_goios(explicit: Optional[str]) -> Optional[str]:
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("LOCPILOT_GOIOS")
    if env:
        candidates.append(env)
    for name in ("ios.exe", "ios"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "goios", "ios.exe"))
    candidates.append(os.path.join(here, "..", "goios", "ios"))
    for candidate in candidates:
        path = os.path.abspath(os.path.expanduser(candidate))
        if os.path.isfile(path):
            return path
    return None


def check_goios(explicit: Optional[str]) -> Optional[str]:
    path = find_goios(explicit)
    if not path:
        record(
            "go-ios",
            "WARN",
            "未找到 ios.exe（降级引擎，可后装）",
            "从 https://github.com/danielpaulus/go-ios/releases 下载 go-ios-win.zip，解压出 ios.exe 后用 --goios 指定。",
        )
        return None
    version = _run([path, "version"], timeout=30)
    parsed = None
    for line in (version["out"] or "").splitlines():
        line = line.strip()
        if line.startswith("{") and '"version"' in line:
            try:
                parsed = json.loads(line).get("version")
            except ValueError:
                pass
    if version["rc"] == 0 and parsed:
        record("go-ios", "PASS", "版本 %s（%s）" % (parsed, path))
        return path
    record(
        "go-ios",
        "WARN",
        "ios.exe 存在但 version 失败: rc=%s %s" % (version["rc"], (version["err"] or version["out"]).strip()[:200]),
        "确认下载的是 go-ios-win.zip（PE32+ x86-64），而不是 mac/linux 包。",
    )
    return path


# --- 8. 端口占用 ----------------------------------------------------------
def check_ports() -> None:
    for port, name, hint in (
        (60105, "go-ios agent", "go-ios agent 已在跑（正常；--userspace 模式无需管理员）"),
        (49151, "pmd3 tunneld", "tunneld 已在跑（正常；管理员进程，非默认路径）"),
    ):
        status = _http_status("http://127.0.0.1:%d/" % port)
        record(
            "端口 %d（%s）" % (port, name),
            "PASS" if status is not None else "SKIP",
            ("HTTP %s" % status) if status is not None else "未监听（未启动即可，不影响默认路径）",
            hint if status is not None else "",
        )


# --- 9. 设备枚举 ----------------------------------------------------------
def check_devices() -> None:
    if _module_version() is None:
        record("设备枚举（usbmux list）", "SKIP", "pymobiledevice3 未安装")
        return
    started = time.time()
    try:
        import asyncio

        from pymobiledevice3.usbmux import list_devices

        devices = asyncio.run(list_devices())
        elapsed = (time.time() - started) * 1000.0
        if devices:
            detail = "发现 %d 台: %s（%.0fms）" % (
                len(devices),
                ", ".join("%s[%s]" % (d.serial, d.connection_type) for d in devices),
                elapsed,
            )
            record("设备枚举（usbmux list）", "PASS", detail)
        else:
            record(
                "设备枚举（usbmux list）",
                "WARN",
                "0 台设备（%.0fms）" % elapsed,
                "需要改定位时：数据线要能传数据、手机解锁、首次弹窗点「信任」。设备连接不是主机就绪检查的必需项。",
            )
    except Exception as exc:
        record(
            "设备枚举（usbmux list）",
            "FAIL",
            "%s: %s" % (type(exc).__name__, str(exc)[:200]),
            "多半是 Apple Mobile Device Service 没起来，或 pmd3 运行时损坏；先跑 pymobiledevice3 doctor。",
        )


# --- 输出 -----------------------------------------------------------------
def print_report(as_json: bool, exit_code: int) -> None:
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for item in RESULTS:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    if as_json:
        print(
            json.dumps(
                {
                    "probe": "windows-host",
                    "probe_version": PROBE_VERSION,
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "ok": exit_code == 0,
                    "exit_code": exit_code,
                    "summary": counts,
                    "results": RESULTS,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print("=" * 78)
    print("LocPilot Windows 探针 #1：主机就绪检查（只读，不需要设备）")
    print("=" * 78)
    for item in RESULTS:
        print("[%s] %s" % (item["status"], item["name"]))
        if item["detail"]:
            print("       %s" % item["detail"])
        if item["hint"] and item["status"] in ("WARN", "FAIL"):
            print("       -> %s" % item["hint"])
    print("-" * 78)
    print("汇总: PASS=%d WARN=%d FAIL=%d SKIP=%d" % (counts["PASS"], counts["WARN"], counts["FAIL"], counts["SKIP"]))
    print("结论: %s（退出码 %d）" % ("主机就绪" if exit_code == 0 else "有阻塞项", exit_code))


def decide_exit() -> int:
    fails = [item for item in RESULTS if item["status"] == "FAIL"]
    if not IS_WINDOWS:
        return 2
    critical_fail = [item for item in fails if item["name"] in ("平台", "Apple Mobile Device Service", "usbmux TCP 127.0.0.1:27015", "pymobiledevice3")]
    return 1 if critical_fail else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LocPilot Windows 主机就绪检查（只读）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--goios", default=None, help="ios.exe 路径（默认自动搜索 PATH / windows/goios/）")
    args = parser.parse_args(argv)

    check_platform()
    check_admin()
    check_amds()
    check_usbmux_port()
    check_pmd3()
    check_goios(args.goios)
    check_ports()
    check_devices()

    code = decide_exit()
    print_report(args.json, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
