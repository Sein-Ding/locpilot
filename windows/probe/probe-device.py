#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LocPilot Windows 探针 #2：pymobiledevice3 userspace 隧道端到端（默认引擎，无管理员）。

用法（在仓库根目录，Windows 上；需要 pymobiledevice3>=11.23.0）：

    # 只检查环境与设备枚举，不建隧道、不改定位（可在任何平台跑，用来验证脚本本身）
    python windows\probe\probe-device.py --self-check

    # 完整链路：枚举设备 -> 建无 root 隧道 -> 挂载 DDI -> 设坐标 -> 保持 -> 清空
    python windows\probe\probe-device.py --lat 25.0339 --lon 121.5645 --hold 8

    # 只清空（设备当前被模拟定位时）
    python windows\probe\probe-device.py --clear-only

可选：
    --udid <UDID>           多台设备时指定
    --json                  输出机器可读 JSON
    --loop default|selector Windows 事件循环策略（默认 default=Proactor；selector 是 pmd3 CLI 用的策略，
                            排查 Wi-Fi / QUIC 路径时可 A/B）

这个脚本验证的正是 Windows 默认引擎要走的路径：
    usbmux(127.0.0.1:27015) -> lockdown -> UserspaceRsdTunnel(无 root) -> auto_mount(RSD)
    -> DvtProvider -> LocationSimulation.set/clear
每一步耗时都会打印，失败会给出可直接照抄的中文处置提示。

退出码
------
0 = 全绿
1 = 设备或隧道/定位步骤失败（看 FAIL 的 hint）
2 = 前置条件不满足（pmd3 缺失 / 没有设备 / 需要先信任）

注意：脚本结束时会尽力 clear 模拟定位；保持期间请在 iPhone 自带地图上目视确认。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from typing import Any, Dict, List, Optional
import warnings

# macOS 开发机用 LibreSSL 跑 pmd3 时会刷 urllib3 警告；Windows 的 OpenSSL 不会。只忽略这一条。
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

PROBE_VERSION = 1
RESULTS: List[Dict[str, Any]] = []


def record(name: str, status: str, detail: str = "", hint: str = "", ms: Optional[float] = None) -> None:
    item: Dict[str, Any] = {"name": name, "status": status, "detail": detail, "hint": hint}
    if ms is not None:
        item["ms"] = round(ms, 1)
    RESULTS.append(item)


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000.0


# --- 错误 -> 用户可读提示（与 docs/windows-engine.md 的映射表保持一致） ----------
def explain(exc: BaseException) -> str:
    """把 pmd3 异常翻译成 Windows 用户能照做的提示。"""
    text = "%s: %s" % (type(exc).__name__, str(exc))
    lowered = text.lower()
    if "failed to connect to usbmuxd" in lowered or "connectionrefused" in lowered or "connection refused" in lowered:
        return (
            "Apple Mobile Device Service 未运行或 127.0.0.1:27015 不可连。"
            "装 Apple 官方 iTunes 经典版（apple.com/itunes/download/win64），"
            "再到「服务」里启动 Apple Mobile Device Service，然后重试。"
        )
    if "note: " in lowered or "no device" in lowered or "devicenotfound" in lowered or "not connected" in lowered:
        return "未发现 iPhone：用数据线连接、解锁，并在弹「要信任此电脑吗？」时点信任；换一根能传数据的线。"
    if "developer mode" in lowered or "developermode" in lowered:
        return "iPhone 没开开发者模式：设置 → 隐私与安全性 → 开发者模式 → 打开并重启；也可在电脑上执行 pymobiledevice3 amfi enable-developer-mode。"
    if "no such service" in lowered or "dtservicehub" in lowered or "invalidservice" in lowered:
        return "开发者服务不可用：确认开发者模式已开、开发者镜像(DDI)已挂载（iOS 27 是 Cryptex1 DDI，需要走 RSD 安装，首次要联网）。"
    if "userspacetunnelunavailable" in lowered or "coredeviceproxy" in lowered:
        return "该 iOS 版本（17.0–17.3）在 Windows 上无法用无 root 隧道：需要管理员终端跑 pymobiledevice3 remote tunneld；建议升级 iOS 到 17.4+。"
    if "accessdenied" in lowered or "admin" in lowered and "privilege" in lowered:
        return "该操作需要管理员：用「以管理员身份运行」打开终端后重试（仅内核/内核隧道降级需要，默认路径不需要）。"
    if "lockdown" in lowered and ("password" in lowered or "unlock" in lowered):
        return "iPhone 处于锁屏/未信任状态：解锁手机后重试。"
    return "重试；仍失败请保存本输出并运行 pymobiledevice3 doctor --json 一起反馈。"


# --- 步骤 -----------------------------------------------------------------
def _pmd3_version() -> Optional[str]:
    try:
        from importlib.metadata import version

        return version("pymobiledevice3")
    except Exception:
        return None


async def _kv(lockdown, key: str) -> Optional[str]:
    """兼容不同 pmd3 小版本的取值方式（与 locpilot/engine/pmd3_worker.py 一致）。"""
    try:
        value = await lockdown.get_value(key=key)
        if value:
            return str(value)
    except Exception:
        pass
    try:
        if key == "ProductVersion":
            value = getattr(lockdown, "product_version", None)
            if value:
                return str(value)
    except Exception:
        pass
    return None


async def run(args: argparse.Namespace) -> int:
    started = time.monotonic()

    # --- 0. 环境 ---------------------------------------------------------
    version = _pmd3_version()
    if version is None:
        record(
            "pymobiledevice3 可用性",
            "FAIL",
            "当前 Python 解释器里没有 pymobiledevice3",
            "pip install 'pymobiledevice3>=11.23.0'（Windows 默认引擎依赖它）。",
        )
        return 2
    record("pymobiledevice3 可用性", "PASS", "版本 %s（%s）" % (version, sys.executable))
    record("平台", "PASS" if sys.platform == "win32" else "WARN", "%s / Python %s" % (platform.platform(), platform.python_version()))

    try:
        from pymobiledevice3.exceptions import PyMobileDevice3Exception
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.services.mobile_image_mounter import auto_mount
        from pymobiledevice3.usbmux import list_devices
        from pymobiledevice3.exceptions import AlreadyMountedError
    except Exception as exc:  # 打包漏依赖时最常见的形态
        record("pmd3 模块导入", "FAIL", explain(exc), "打包时用 --collect-all pymobiledevice3；开发机先 pip install -e . 或 -r windows/requirements.txt。")
        return 2
    record("pmd3 模块导入", "PASS", "usbmux / lockdown / UserspaceRsdTunnel / auto_mount / DVT 均可用")

    # --- 1. 设备枚举 ------------------------------------------------------
    t = time.monotonic()
    try:
        devices = await list_devices()
    except Exception as exc:
        record("usbmux 设备枚举", "FAIL", explain(exc), "", _ms(t))
        return 2
    if not devices:
        if args.self_check:
            record(
                "usbmux 设备枚举",
                "WARN",
                "0 台设备（--self-check 允许；这只说明脚本/运行时可导入，不能替代 Windows 真机验收）",
                explain(RuntimeError("No device")),
                _ms(t),
            )
            return 0
        record(
            "usbmux 设备枚举",
            "FAIL",
            "0 台设备",
            explain(RuntimeError("No device")),
            _ms(t),
        )
        return 2
    detail = ", ".join("%s[%s]" % (d.serial, d.connection_type) for d in devices)
    record("usbmux 设备枚举", "PASS", detail, "", _ms(t))
    target = devices[0]
    if args.udid:
        matches = [d for d in devices if d.matches_udid(args.udid)]
        if not matches:
            record("选择设备", "FAIL", "找不到 UDID=%s" % args.udid)
            return 2
        target = matches[0]
    record("选择设备", "PASS", "UDID=%s connection=%s" % (target.serial, target.connection_type))

    if args.self_check:
        record("自检模式", "SKIP", "只做环境与设备枚举，不建隧道、不改定位")
        return 0

    lockdown = None
    tunnel = None
    was_cleared = True
    set_ok = False
    exit_code = 0
    try:
        # --- 2. lockdown（配对/信任） ------------------------------------
        t = time.monotonic()
        try:
            lockdown = await create_using_usbmux(serial=target.serial, autopair=True)
        except Exception as exc:
            record("lockdown 连接/配对", "FAIL", explain(exc), "", _ms(t))
            return 2
        product_version = await _kv(lockdown, "ProductVersion")
        product_type = await _kv(lockdown, "ProductType")
        device_name = await _kv(lockdown, "DeviceName")
        major: Optional[int] = None
        if product_version:
            try:
                major = int(str(product_version).split(".", 1)[0])
            except ValueError:
                major = None
        record(
            "lockdown 连接/配对",
            "PASS",
            "%s / %s / iOS %s" % (device_name or "?", product_type or "?", product_version or "?"),
            "",
            _ms(t),
        )
        if major is not None and major < 17:
            record(
                "iOS 17+ 判定",
                "WARN",
                "设备是 iOS %s：不需要 RSD 隧道，走 developer simulate-location 老路径；本探针只验证 17+ 路径。" % product_version,
            )
            return 2
        record("iOS 17+ 判定", "PASS", "需要 RSD 隧道（iOS %s）" % (product_version or "?"))

        # --- 3. 无 root userspace 隧道 ----------------------------------
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel

        t = time.monotonic()
        tunnel = UserspaceRsdTunnel(serial=target.serial, autopair=True)
        try:
            rsd = await asyncio.wait_for(tunnel.aopen(), timeout=args.tunnel_timeout)
        except asyncio.TimeoutError:
            record("userspace 隧道（无管理员）", "FAIL", "建立超时 %.0fs" % args.tunnel_timeout, "重插数据线后重试；若设备锁屏请解锁。", _ms(t))
            return 1
        except Exception as exc:
            record("userspace 隧道（无管理员）", "FAIL", explain(exc), "", _ms(t))
            return 1
        try:
            address = rsd.service.address
        except Exception:
            address = ("?", "?")
        record(
            "userspace 隧道（无管理员）",
            "PASS",
            "RSD %s:%s（in_process=%s）" % (address[0], address[1], getattr(rsd, "is_in_process_tunnel", "?")),
            "",
            _ms(t),
        )

        if args.clear_only:
            # 只清空：建一条 DTX 通道后 clear
            t = time.monotonic()
            try:
                async with DvtProvider(rsd) as dvt:
                    async with LocationSimulation(dvt) as sim:
                        await sim.clear()
                record("清空模拟定位", "PASS", "已发送 stopLocationSimulation", "", _ms(t))
                return 0
            except Exception as exc:
                record("清空模拟定位", "FAIL", explain(exc), "", _ms(t))
                return 1

        # --- 4. 挂载 DDI（iOS 27 = Cryptex1，必须走 RSD） ----------------
        t = time.monotonic()
        try:
            await asyncio.wait_for(auto_mount(rsd), timeout=args.mount_timeout)
            record("开发者镜像 DDI", "PASS", "已挂载（auto_mount 走 RSD/cryptexd）", "", _ms(t))
        except AlreadyMountedError:
            record("开发者镜像 DDI", "PASS", "之前已挂载，跳过", "", _ms(t))
        except asyncio.TimeoutError:
            record(
                "开发者镜像 DDI",
                "FAIL",
                "auto_mount 超时 %.0fs" % args.mount_timeout,
                "首次安装 Cryptex1 DDI 需要联网下载，网络慢就加大 --mount-timeout；确认 iPhone 已解锁。",
                _ms(t),
            )
            exit_code = 1
        except Exception as exc:
            record("开发者镜像 DDI", "FAIL", explain(exc), "", _ms(t))
            exit_code = 1
            # DDI 没挂上时 DVT 一定失败，直接收尾而不是再报一个误导性的错
            return exit_code

        # --- 5. DVT 定位通道 --------------------------------------------
        t = time.monotonic()
        try:
            dvt = await DvtProvider(rsd).__aenter__()
        except Exception as exc:
            record("DVT 通道（dtservicehub）", "FAIL", explain(exc), "开发者模式未开时 dtservicehub 打不开；开完需重启设备。", _ms(t))
            return 1
        record("DVT 通道（dtservicehub）", "PASS", "", "", _ms(t))
        sim = None
        try:
            t = time.monotonic()
            try:
                sim = await LocationSimulation(dvt).__aenter__()
            except Exception as exc:
                record("定位模拟通道", "FAIL", explain(exc), "", _ms(t))
                return 1
            record("定位模拟通道", "PASS", "com.apple.instruments.server.services.LocationSimulation", "", _ms(t))

            if args.lat is not None and args.lon is not None:
                was_cleared = False
                t = time.monotonic()
                try:
                    await asyncio.wait_for(sim.set(float(args.lat), float(args.lon)), timeout=args.set_timeout)
                except Exception as exc:
                    record("设置模拟定位", "FAIL", explain(exc), "", _ms(t))
                    return 1
                set_ok = True
                record(
                    "设置模拟定位",
                    "PASS",
                    "lat=%.6f lon=%.6f；保持 %.1fs" % (float(args.lat), float(args.lon), float(args.hold)),
                    "保持期间在 iPhone 自带地图上目视确认定位已到目标点。",
                    _ms(t),
                )
                if args.hold > 0:
                    await asyncio.sleep(float(args.hold))
                t = time.monotonic()
                try:
                    await asyncio.wait_for(sim.clear(), timeout=args.set_timeout)
                    was_cleared = True
                    record("清空模拟定位", "PASS", "已发送 stopLocationSimulation", "", _ms(t))
                except Exception as exc:
                    record("清空模拟定位", "FAIL", explain(exc), "", _ms(t))
                    exit_code = 1
            else:
                record("设置模拟定位", "SKIP", "没有传 --lat/--lon：只验证环境、隧道与 DDI")
        finally:
            if sim is not None:
                try:
                    await sim.__aexit__(None, None, None)
                except Exception:
                    pass
            try:
                await dvt.__aexit__(None, None, None)
            except Exception:
                pass
        return exit_code
    finally:
        # 尽力恢复真实定位（不掩盖上面的失败）
        if tunnel is not None and set_ok and not was_cleared:
            try:
                await tunnel.aclose()
            except Exception:
                pass
        elif tunnel is not None:
            try:
                await tunnel.aclose()
            except Exception:
                pass
        if lockdown is not None:
            try:
                await lockdown.close()
            except Exception:
                pass
        record("总耗时", "SKIP", "%.1fs" % ((time.monotonic() - started)), "")


def print_report(as_json: bool, exit_code: int) -> None:
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for item in RESULTS:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    if as_json:
        print(
            json.dumps(
                {
                    "probe": "windows-pmd3-device",
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
    print("LocPilot Windows 探针 #2：pymobiledevice3 userspace 隧道端到端（默认引擎，无管理员）")
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
    print("提醒：本脚本只验证定位链路；保持期间请用 iPhone 自带地图目视确认，结束后会自动清空。")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LocPilot Windows 真机探针（pymobiledevice3 userspace 隧道）")
    parser.add_argument("--self-check", action="store_true", help="只检查环境与设备枚举（不建隧道、不改定位）")
    parser.add_argument("--clear-only", action="store_true", help="只清空当前模拟定位")
    parser.add_argument("--udid", default=None, help="目标设备 UDID")
    parser.add_argument("--lat", type=float, default=None, help="模拟纬度")
    parser.add_argument("--lon", type=float, default=None, help="模拟经度")
    parser.add_argument("--hold", type=float, default=5.0, help="设置后保持秒数（默认 5）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--loop", choices=("default", "selector"), default="default", help="Windows 事件循环策略")
    parser.add_argument("--tunnel-timeout", type=float, default=60.0, help="隧道建立超时秒（默认 60）")
    parser.add_argument("--mount-timeout", type=float, default=300.0, help="DDI 下载/挂载超时秒（默认 300）")
    parser.add_argument("--set-timeout", type=float, default=20.0, help="单次定位调用超时秒（默认 20）")
    args = parser.parse_args(argv)

    if (args.lat is None) != (args.lon is None):
        parser.error("--lat 与 --lon 必须同时给")
    if sys.platform == "win32" and args.loop == "selector":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n用户中断；若已设置定位，请再跑 --clear-only 清空。", file=sys.stderr)
        code = 130
    print_report(args.json, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
