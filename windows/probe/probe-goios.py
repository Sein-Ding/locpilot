#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LocPilot Windows 探针 #3：go-ios 降级路径（Windows userspace 隧道，无管理员）。

定位：go-ios 只是**降级引擎**——pmd3 进程内 worker 不可用（例如冻结包没打进依赖）时才用。
go-ios 在 Windows 上的特点（源码核实，见 docs/windows-engine.md）：
  * ios tunnel start --userspace 不需要管理员，也不需要 wintun.dll；
  * 但 iOS 17+ 的 ios setlocation 是「一个坐标一个进程，进程阻塞到收到 Ctrl-C/Ctrl-Break」，
    不适合高频回放；清空定位 = 让 setlocation 进程退出（而不是 ios resetlocation）。
  * agent 的隧道信息 HTTP 端口实际默认 60105（帮助文本写 28100 是过期文档），必须显式固定。

用法（仓库根目录，Windows；先下载 go-ios-win.zip 解压出 ios.exe）：

    # 只检查 ios.exe 能否运行
    python windows\probe\probe-goios.py --self-check --goios C:\tools\go-ios\ios.exe

    # 只验证 agent + 隧道（不改定位）
    python windows\probe\probe-goios.py --agent-only --goios C:\tools\go-ios\ios.exe

    # 完整：起 agent -> 设坐标 -> 保持 -> Ctrl-Break 退出 setlocation -> 停 agent
    python windows\probe\probe-goios.py --goios C:\tools\go-ios\ios.exe --lat 25.0339 --lon 121.5645 --hold 8

退出码
------
0 = 全绿
1 = 某一步失败（看 FAIL 的 hint）
2 = 前置条件不满足（找不到 ios.exe / 没有设备）

注意：本脚本会在结束时尽力清空模拟定位并停掉自己启动的 agent；agent 日志写在临时文件里，
退出时删除（除非失败，失败时打印末尾 20 行再删）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

PROBE_VERSION = 1
IS_WINDOWS = sys.platform == "win32"
CREATE_NEW_PROCESS_GROUP = 0x00000200  # 只有 Windows 有
CREATE_NO_WINDOW = 0x08000000
RESULTS: List[Dict[str, Any]] = []


def record(name: str, status: str, detail: str = "", hint: str = "", ms: Optional[float] = None) -> None:
    item: Dict[str, Any] = {"name": name, "status": status, "detail": detail, "hint": hint}
    if ms is not None:
        item["ms"] = round(ms, 1)
    RESULTS.append(item)


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000.0


def _http_status(url: str, timeout: float = 1.0) -> Optional[int]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.getcode())
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return None


def _tcp(host: str, port: int, timeout: float = 0.6) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _run(argv: List[str], timeout: float = 30.0) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(argv, **kwargs)
        return {
            "argv": argv,
            "rc": proc.returncode,
            "out": (proc.stdout or b"").decode("utf-8", "replace"),
            "err": (proc.stderr or b"").decode("utf-8", "replace"),
        }
    except FileNotFoundError as exc:
        return {"argv": argv, "rc": 127, "out": "", "err": "命令不存在: %s" % (exc,)}
    except subprocess.TimeoutExpired:
        return {"argv": argv, "rc": 124, "out": "", "err": "超时 %.0fs" % timeout}


def _json_lines(text: str) -> List[Any]:
    """go-ios 的 stdout 是 JSON，但 stderr 会混 WARN；逐行解析容错。"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _first_json(text: str) -> Optional[Any]:
    """go-ios 有些命令直接输出一个 JSON 对象/数组（可能带换行）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start_candidates = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    for start in sorted(start_candidates):
        for end in range(len(text), start, -1):
            if text[end - 1] in "}]":
                try:
                    return json.loads(text[start:end])
                except ValueError:
                    continue
    return None


# --- go-ios 路径 ----------------------------------------------------------
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
    candidates += [os.path.join(here, "..", "goios", "ios.exe"), os.path.join(here, "..", "goios", "ios")]
    for candidate in candidates:
        path = os.path.abspath(os.path.expanduser(candidate))
        if os.path.isfile(path):
            return path
    return None


def goios_version(exe: str) -> Optional[str]:
    result = _run([exe, "version"], timeout=30)
    for payload in _json_lines(result["out"]):
        if isinstance(payload, dict) and payload.get("version"):
            return str(payload["version"])
    if result["rc"] != 0:
        return None
    return None


def _agent_url(port: int, path: str) -> str:
    return "http://127.0.0.1:%d%s" % (port, path)


def _wait_agent(port: int, timeout: float, proc: Optional[subprocess.Popen] = None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        if _http_status(_agent_url(port, "/health"), timeout=0.5) == 200:
            return True
        time.sleep(0.25)
    return False


def _wait_ready(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _http_status(_agent_url(port, "/ready"), timeout=0.5) == 200:
            return True
        time.sleep(0.25)
    return False


def start_agent(exe: str, port: int, pair_dir: str, log_path: str) -> Optional[subprocess.Popen]:
    try:
        os.makedirs(pair_dir, exist_ok=True)
    except OSError as exc:
        record("创建配对目录", "FAIL", "%s: %s" % (pair_dir, exc), "用 --pair-record-path 指向可写目录（例如 %LOCALAPPDATA%\\LocPilot\\goios-pair）。")
        return None
    cmd = [
        exe,
        "tunnel",
        "start",
        "--userspace",
        "--pair-record-path",
        pair_dir,
        "--tunnel-info-port",
        str(port),
    ]
    kwargs: Dict[str, Any] = dict(stdout=open(log_path, "ab"), stderr=subprocess.STDOUT)
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        record("启动 agent", "FAIL", "%s: %s" % (cmd, exc), "确认 ios.exe 可执行；Windows 上不要用 mac/linux 包。")
        return None


def stop_agent(exe: str, port: int, proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        urllib.request.urlopen(_agent_url(port, "/shutdown"), timeout=2.0).read()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
        return
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# --- 主流程 ---------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    exe = find_goios(args.goios)
    if not exe:
        record("查找 ios.exe", "FAIL", "未找到", "下载 https://github.com/danielpaulus/go-ios/releases 的 go-ios-win.zip，解压得到 ios.exe，用 --goios 指定。")
        return 2
    record("查找 ios.exe", "PASS", exe)
    version = goios_version(exe)
    if not version:
        record("ios version", "FAIL", "运行 version 失败", "确认是 PE32+ x86-64 的 Windows 包，且未被杀软拦截。")
        return 2
    record("ios version", "PASS", "go-ios %s（%s）" % (version, sys.platform))

    if args.self_check:
        running = _http_status(_agent_url(args.tunnel_info_port, "/health"), timeout=0.5)
        record(
            "agent 状态",
            "PASS" if running == 200 else "SKIP",
            "已在监听 %d" % args.tunnel_info_port if running == 200 else "未运行（--self-check 不启动）",
        )
        record("自检模式", "SKIP", "只验证二进制可运行；真机/隧道行为待 Windows 验证")
        return 0

    log_file = tempfile.NamedTemporaryFile(prefix="locpilot-goios-agent-", suffix=".log", delete=False)
    log_path = log_file.name
    log_file.close()
    agent = None
    started_agent = False
    exit_code = 0
    try:
        # --- agent --------------------------------------------------------
        t = time.monotonic()
        if _http_status(_agent_url(args.tunnel_info_port, "/health"), timeout=0.5) == 200:
            record("go-ios agent", "PASS", "已在运行（端口 %d）" % args.tunnel_info_port, "", _ms(t))
        else:
            agent = start_agent(exe, args.tunnel_info_port, args.pair_record_path, log_path)
            if agent is None:
                return 2
            started_agent = True
            if not _wait_agent(args.tunnel_info_port, args.agent_timeout, agent):
                tail = ""
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                        tail = "".join(fh.readlines()[-10:])[-600:]
                except OSError:
                    pass
                record(
                    "go-ios agent",
                    "FAIL",
                    "启动失败或超时 %.0fs：%s" % (args.agent_timeout, tail or "无日志"),
                    "确认 --pair-record-path 指向可写目录；端口 %d 未被占用；杀软没有拦截 ios.exe。" % args.tunnel_info_port,
                    _ms(t),
                )
                return 2
            record("go-ios agent", "PASS", "--userspace 模式已启动（端口 %d，无管理员）" % args.tunnel_info_port, "", _ms(t))

        if not _wait_ready(args.tunnel_info_port, args.agent_timeout):
            record(
                "agent 首次隧道扫描",
                "WARN",
                "/ready 未在 %.0fs 内变绿" % args.agent_timeout,
                "没插设备时 /ready 也可能迟迟不绿；插入并解锁 iPhone 后重试。",
            )
        else:
            record("agent 首次隧道扫描", "PASS", "/ready = 200")

        # --- 隧道列表 -----------------------------------------------------
        t = time.monotonic()
        ls = _run([exe, "tunnel", "ls", "--tunnel-info-port", str(args.tunnel_info_port)], timeout=30)
        tunnels = _first_json(ls["out"])
        if isinstance(tunnels, list) and tunnels:
            detail = "; ".join(
                "udid=%s userspace=%s port=%s" % (item.get("Udid"), item.get("UserspaceTUN"), item.get("UserspaceTUNPort"))
                for item in tunnels
                if isinstance(item, dict)
            )
            record("tunnel ls", "PASS", detail or str(tunnels)[:300], "", _ms(t))
        else:
            record(
                "tunnel ls",
                "WARN",
                "没有隧道（%s）" % ((ls["err"] or ls["out"]).strip()[:200] or "空列表"),
                "iOS 17+ 必须先有隧道才能 setlocation；插上并解锁 iPhone，等 agent 扫描一轮。",
                _ms(t),
            )

        # --- 设备 ---------------------------------------------------------
        t = time.monotonic()
        listing = _run([exe, "list", "--details", "--tunnel-info-port", str(args.tunnel_info_port)], timeout=30)
        payload = _first_json(listing["out"])
        devices = payload.get("deviceList") if isinstance(payload, dict) else None
        if not devices:
            record(
                "设备列表",
                "FAIL",
                "0 台设备（%s）" % ((listing["err"] or listing["out"]).strip()[:200] or "空"),
                "数据线要能传数据、iPhone 解锁、弹窗点信任；Windows 上还要求 Apple Mobile Device Service 在跑。",
                _ms(t),
            )
            return 2
        device = devices[0]
        udid = device.get("Udid") or device.get("SerialNumber")
        record(
            "设备列表",
            "PASS",
            "%s / %s / iOS %s" % (device.get("ProductName") or "?", device.get("ProductType") or "?", device.get("ProductVersion") or "?"),
            "",
            _ms(t),
        )
        if args.udid:
            matched = [d for d in devices if d.get("Udid") == args.udid]
            if not matched:
                record("选择设备", "FAIL", "找不到 UDID=%s" % args.udid)
                return 2
            device = matched[0]
            udid = device.get("Udid")
        record("选择设备", "PASS", "UDID=%s" % udid)

        if args.agent_only:
            record("agent-only 模式", "SKIP", "不设坐标、不改定位")
            return 0

        major = None
        try:
            major = int(str(device.get("ProductVersion") or "").split(".", 1)[0])
        except ValueError:
            major = None
        if major is not None and major < 17:
            record("iOS 版本判定", "WARN", "iOS %s：老路径，setlocation 立即返回，清空用 resetlocation" % device.get("ProductVersion"))
        else:
            record("iOS 版本判定", "PASS", "iOS %s：setlocation 阻塞到 Ctrl-C/Ctrl-Break，清空=让进程退出" % device.get("ProductVersion"))

        # --- 设置定位 -----------------------------------------------------
        if args.lat is None or args.lon is None:
            record("设置定位", "SKIP", "没有传 --lat/--lon")
            return 0

        set_cmd = [
            exe,
            "setlocation",
            "--lat=%.6f" % args.lat,
            "--lon=%.6f" % args.lon,
            "--udid=%s" % udid,
            "--tunnel-info-port=%d" % args.tunnel_info_port,
        ]
        kwargs: Dict[str, Any] = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if IS_WINDOWS:
            kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        t = time.monotonic()
        set_proc = subprocess.Popen(set_cmd, **kwargs)
        time.sleep(min(3.0, max(1.0, args.hold)))
        rc = set_proc.poll()
        if rc is not None:
            out, err = set_proc.communicate(timeout=5)
            if major is not None and major < 17 and rc == 0:
                record("设置定位", "PASS", "老路径已返回（rc=0）", "", _ms(t))
            else:
                record(
                    "设置定位",
                    "FAIL",
                    "进程提前退出 rc=%s：%s" % (rc, ((err or out or b"").decode("utf-8", "replace")).strip()[:400]),
                    "iOS 17+ 应保持进程存活；提前退出多半是隧道没就绪/开发者模式未开/DDI 未挂载。",
                    _ms(t),
                )
                return 1
        else:
            record("设置定位", "PASS", "进程存活，模拟定位保持中（iOS 17+ 语义）", "在 iPhone 自带地图上目视确认。", _ms(t))

        if args.hold > 0:
            time.sleep(max(0.0, args.hold - 1.0))

        # --- 清空 ---------------------------------------------------------
        t = time.monotonic()
        cleared = False
        if major is not None and major < 17:
            reset = _run([exe, "resetlocation", "--udid=%s" % udid], timeout=30)
            cleared = reset["rc"] == 0
            record("清空定位", "PASS" if cleared else "FAIL", "resetlocation rc=%s" % reset["rc"], "" if cleared else (reset["err"] or reset["out"])[:300], _ms(t))
        else:
            signal_sent = False
            if IS_WINDOWS:
                try:
                    set_proc.send_signal(signal.CTRL_BREAK_EVENT)  # 需要 CREATE_NEW_PROCESS_GROUP（已设）
                    signal_sent = True
                except Exception as exc:
                    record("清空定位（发送 Ctrl-Break）", "WARN", "发送失败: %s" % exc)
            else:
                try:
                    set_proc.send_signal(signal.SIGINT)
                    signal_sent = True
                except Exception as exc:
                    record("清空定位（发送 SIGINT）", "WARN", "发送失败: %s" % exc)
            try:
                set_proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pass
            if set_proc.poll() is None:
                try:
                    set_proc.kill()
                except Exception:
                    pass
                record(
                    "清空定位",
                    "WARN",
                    "进程未在 8s 内退出，已强制结束（%s）" % ("Ctrl-Break 没送达" if not signal_sent else "设备可能在收尾"),
                    "强制结束不等于 Ctrl-C：设备端可能残留模拟定位，重启 iPhone 可恢复；这也是 go-ios 只作降级引擎的原因。",
                    _ms(t),
                )
                exit_code = 1
            else:
                record("清空定位", "PASS", "setlocation 已退出（go-ios 收到 Ctrl-Break/SIGINT 后会 revert）", "", _ms(t))
        return exit_code
    finally:
        if started_agent and not args.keep_agent:
            try:
                stop_agent(exe, args.tunnel_info_port, agent)
                record("停止 agent", "PASS", "已发送 /shutdown 并等待退出")
            except Exception as exc:
                record("停止 agent", "WARN", "停止失败: %s（任务管理器里结束 ios.exe）" % (exc,))
        try:
            os.unlink(log_path)
        except OSError:
            pass
        record("总耗时", "SKIP", "%.1fs" % (time.monotonic() - started))


def print_report(as_json: bool, exit_code: int) -> None:
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for item in RESULTS:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    if as_json:
        print(
            json.dumps(
                {
                    "probe": "windows-goios",
                    "probe_version": PROBE_VERSION,
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "platform": sys.platform,
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
    print("LocPilot Windows 探针 #3：go-ios 降级路径（userspace 隧道，无管理员）")
    print("=" * 78)
    for item in RESULTS:
        ms = "（%.0fms）" % item["ms"] if "ms" in item else ""
        print("[%s] %s%s" % (item["status"], item["name"], ms))
        if item["detail"]:
            print("       %s" % item["detail"])
        if item["hint"] and item["status"] in ("WARN", "FAIL"):
            print("       -> %s" % item["hint"])
    print("-" * 78)
    print("汇总: PASS=%d WARN=%d FAIL=%d SKIP=%d" % (counts["PASS"], counts["WARN"], counts["FAIL"], counts["SKIP"]))
    print("结论: %s（退出码 %d）" % ("全绿" if exit_code == 0 else "有阻塞项", exit_code))
    print("提醒：go-ios 只作降级；默认引擎是 pymobiledevice3 进程内 worker。")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LocPilot Windows go-ios 降级路径探针")
    parser.add_argument("--goios", default=None, help="ios.exe 路径（默认自动搜索）")
    parser.add_argument("--self-check", action="store_true", help="只验证 ios.exe 可运行且输出版本")
    parser.add_argument("--agent-only", action="store_true", help="只起 agent + 列隧道/设备，不改定位")
    parser.add_argument("--udid", default=None, help="目标设备 UDID")
    parser.add_argument("--lat", type=float, default=None, help="模拟纬度")
    parser.add_argument("--lon", type=float, default=None, help="模拟经度")
    parser.add_argument("--hold", type=float, default=5.0, help="设置后保持秒数（默认 5）")
    parser.add_argument("--tunnel-info-port", type=int, default=60105, help="go-ios agent 隧道信息端口（默认 60105）")
    parser.add_argument(
        "--pair-record-path",
        default=None,
        help="go-ios 配对身份目录（默认 %%LOCALAPPDATA%%\\LocPilot\\goios-pair）",
    )
    parser.add_argument("--agent-timeout", type=float, default=30.0, help="agent 启动/就绪超时秒（默认 30）")
    parser.add_argument("--keep-agent", action="store_true", help="结束时保留 agent（默认停掉自己启动的）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    if args.udid is None:
        args.udid = os.environ.get("LOCPILOT_UDID") or None
    if args.lat is None and args.lon is not None or args.lon is None and args.lat is not None:
        parser.error("--lat 与 --lon 必须同时给")
    if not args.pair_record_path:
        if os.environ.get("LOCPILOT_HOME"):
            base = os.environ["LOCPILOT_HOME"]
        elif IS_WINDOWS:
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "LocPilot")
        else:
            base = os.path.join(os.path.expanduser("~"), ".locpilot")
        args.pair_record_path = os.path.join(base, "goios-pair")

    code = 1
    try:
        code = run(args)
    except KeyboardInterrupt:
        print("\n用户中断；请检查是否残留 ios.exe / 模拟定位。", file=sys.stderr)
        code = 130
    except Exception as exc:  # 探针自己不能裸崩，否则用户只看到 traceback
        record("探针异常", "FAIL", "%s: %s" % (type(exc).__name__, exc), "把完整输出贴回；这是探针脚本问题，不是引擎结论。")
        code = 1
    print_report(args.json, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
