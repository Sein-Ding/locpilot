"""go-ios 引擎（MIT 许可的备选实现）。

命令面（依据 go-ios v1.3.2 实测，见 docs/engine-options.md）：
* 枚举：ios list --details  → {"deviceList": [...]}；再 ios info --udid=<udid> 取 ProductVersion
* 定位：ios setlocation --udid=<udid> --lat=<v> --lon=<v>
    - iOS ≤16：立即返回（lockdown com.apple.dt.simulatelocation）
    - iOS 17+：RSD+DTX 会话**持有到 SIGINT**，进程活着即代表坐标已生效；收到 SIGINT 会 revert
    因此 iOS 17+ 必须用常驻进程语义，不能等待其退出（等待只会超时假失败）。
* 清除：常驻进程发 SIGINT（iOS 17+）；iOS ≤16 用 ios resetlocation
* 隧道：ios tunnel start --userspace --tunnel-info-port=<port>
    端口必须显式统一：帮助文本写 28100，实现默认 60105/GO_IOS_AGENT_PORT，不一致会导致 agent 与 client 对不上。
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
from typing import List, Optional

from .base import DeviceInfo, Engine, EngineError, EngineUnavailable, which as which_bin

UDID_RE = re.compile(r"\b([0-9A-Za-z]{8}-[0-9A-Za-z]{16}|[0-9A-Fa-f]{40})\b")
# go-ios 实现里的默认隧道 HTTP API 端口（帮助文本中的 28100 是过期文档）
DEFAULT_TUNNEL_INFO_PORT = 60105


class GoIosEngine(Engine):
    name = "go-ios"
    label = "go-ios（MIT，备选引擎）"
    capabilities = {
        "persistent_session": False,
        # iOS 17+ 每次下发都新建 DTX 会话（CLI 形态），无法复用会话
        "supports_ios17": True,
        "needs_tunnel": "manual",
        "needs_sudo": False,
        # setlocationgpx 在 v1.3.2 没有 RSD 分支：iOS 17+ 上不可用，故不声明支持
        "supports_gpx_play": False,
        "real_device": True,
    }

    def __init__(self, runner=None, settings=None) -> None:
        super().__init__(runner=runner, settings=settings)
        self._location_proc = None
        self.tunnel_port: Optional[int] = None

    # --- 基础 -----------------------------------------------------------

    def binary(self) -> Optional[str]:
        explicit = self.settings.get("goios_bin") or os.environ.get("LOCPILOT_GOIOS")
        if explicit and which_bin(explicit):
            return explicit
        return which_bin("ios")

    def availability(self):
        binary = self.binary()
        if not binary:
            return False, "未找到 go-ios 可执行文件（brew install danielpaulus/go-ios/go-ios）"
        return True, "binary=%s" % binary

    def _run(self, args: List[str], timeout: float = 30.0):
        binary = self.binary()
        if not binary:
            raise EngineUnavailable("未找到 go-ios 可执行文件")
        return self.runner.run([binary] + args, timeout=timeout)

    # --- 设备枚举 -------------------------------------------------------

    def list_devices(self) -> List[DeviceInfo]:
        """解析 ios list --details 的 {"deviceList": [...]}。

        实测形状（v1.3.2）：
          {"deviceList":[{"Udid":"...","ConnectionType":"usb","DeviceName":"..."}]}
        不带 --details 时元素是裸 UDID 字符串；两种都要兼容，否则 iOS 版本拿不到，
        needs_tunnel 会恒为 False，iOS 17+ 分支直接失效。
        """
        result = self._run(["list", "--details"], timeout=25.0)
        if not result.ok:
            raise EngineError("ios list 失败: %s" % (result.tail(),))
        text = (result.stdout or "").strip()
        devices: List[DeviceInfo] = []
        for entry in self._device_entries(text):
            if isinstance(entry, dict):
                udid = entry.get("Udid") or entry.get("udid") or entry.get("UDID") or entry.get("serial")
                if not udid:
                    continue
                devices.append(
                    DeviceInfo(
                        udid=str(udid),
                        name=entry.get("DeviceName") or entry.get("name"),
                        product_type=entry.get("ProductType") or entry.get("productType"),
                        ios_version=entry.get("ProductVersion") or entry.get("productVersion"),
                        connection=str(entry.get("ConnectionType") or entry.get("connectionType") or "usb").lower(),
                        engine=self.name,
                    )
                )
            elif isinstance(entry, str) and UDID_RE.fullmatch(entry.strip()):
                devices.append(DeviceInfo(udid=entry.strip(), engine=self.name))
        if not devices:
            # 兜底：任何形态的文本里抠 UDID，至少保证能连上
            for match in UDID_RE.finditer(text):
                devices.append(DeviceInfo(udid=match.group(1), engine=self.name))
        for device in devices:
            self._enrich(device)
        return devices

    @staticmethod
    def _device_entries(text: str) -> List[object]:
        if not text:
            return []
        try:
            payload = json.loads(text)
        except ValueError:
            return []
        if isinstance(payload, dict):
            entries = payload.get("deviceList") or payload.get("DeviceList") or []
            return list(entries) if isinstance(entries, list) else []
        if isinstance(payload, list):
            return list(payload)
        return []

    def _enrich(self, device: DeviceInfo) -> None:
        """补齐 ProductVersion：needs_tunnel 依赖 ios_major，缺了 iOS 17+ 就判断不出来。"""
        if device.ios_version:
            return
        try:
            result = self._run(["info", "--udid=%s" % device.udid], timeout=20.0)
        except EngineError:
            return
        if not result.ok:
            return
        try:
            info = json.loads(result.stdout or "{}")
        except ValueError:
            return
        values = info.get("values") if isinstance(info, dict) else None
        source = values if isinstance(values, dict) else info
        if not isinstance(source, dict):
            return
        device.ios_version = source.get("ProductVersion") or source.get("productVersion")
        device.product_type = device.product_type or source.get("ProductType")
        device.name = device.name or source.get("DeviceName")

    # --- 会话 -----------------------------------------------------------

    def open(self, udid: Optional[str] = None) -> DeviceInfo:
        devices = self.list_devices()
        if not devices:
            raise EngineError("未发现 iOS 设备（go-ios）")
        target = None
        for dev in devices:
            if udid is None or dev.udid == udid:
                target = dev
                break
        if target is None:
            raise EngineError("未找到指定设备: %s" % (udid,))
        self.device = target
        self.opened = True
        self.error = None
        return target

    def _udid_args(self) -> List[str]:
        device = self.require_open()
        return ["--udid=%s" % device.udid]

    # --- 定位 -----------------------------------------------------------

    def set_location(self, lat: float, lon: float) -> None:
        """下发坐标。

        iOS 17+ 的 setlocation 会阻塞到 SIGINT，等待退出必然超时——那是**假失败**：
        坐标其实已经生效。所以这里用常驻进程语义：短等一小会儿，
        进程仍活着即视为成功，并把句柄留着供后续 SIGINT 清除。
        """
        binary = self.binary()
        if not binary:
            raise EngineUnavailable("未找到 go-ios 可执行文件")
        self._stop_location_process()
        argv = [binary, "setlocation"] + self._udid_args() + ["--lat=%.6f" % float(lat), "--lon=%.6f" % float(lon)]
        proc = self.runner.popen(argv, env=dict(os.environ))
        deadline = time.time() + 1.5
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.05)
        if proc.poll() is not None:
            # 立即退出：iOS ≤16 的正常路径（下发完即返回）
            if proc.returncode != 0:
                self._location_proc = None
                self.error = self._drain(proc)
                raise EngineError("go-ios setlocation 失败: %s" % (self.error,))
            self._location_proc = None
        else:
            # 仍在运行：iOS 17+ 的常驻 DTX 会话，持有坐标直到 SIGINT
            self._location_proc = proc
        self.last_position = {"lat": float(lat), "lon": float(lon), "ts": time.time()}
        self.error = None

    def clear_location(self) -> None:
        device = self.require_open()
        if self._stop_location_process():
            # 常驻进程收到 SIGINT 后会 revert 定位
            self.last_position = None
            return
        result = self._run(["resetlocation", "--udid=%s" % device.udid], timeout=20.0)
        if not result.ok:
            hint = ""
            if device.needs_tunnel:
                hint = "（iOS %s 上 resetlocation 没有 RSD 分支，需先下发一次坐标再清除）" % (device.ios_version or "17+",)
            raise EngineError("go-ios resetlocation 失败: %s%s" % (result.tail(), hint))
        self.last_position = None

    def _stop_location_process(self) -> bool:
        proc = self._location_proc
        self._location_proc = None
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.send_signal(signal.SIGINT)
        except Exception:
            pass
        deadline = time.time() + 3.0
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.05)
        if proc.poll() is None:
            proc.terminate()
            deadline = time.time() + 2.0
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.05)
            if proc.poll() is None:
                proc.kill()
        return True

    @staticmethod
    def _drain(proc) -> str:
        try:
            _out, err = proc.communicate(timeout=1.0)
            return (err or "").strip()[-300:]
        except Exception:
            return "returncode=%s" % getattr(proc, "returncode", "?")

    # --- 隧道 -----------------------------------------------------------

    def start_tunnel(self, port: Optional[int] = None) -> str:
        """启动用户态隧道。端口显式传入并写入 GO_IOS_AGENT_PORT，
        避免 agent/client 各自用不同的默认值（帮助文本 28100 vs 实现 60105）。"""
        binary = self.binary()
        if not binary:
            raise EngineUnavailable("未找到 go-ios 可执行文件")
        resolved = int(port or self.settings.get("goios_tunnel_port") or DEFAULT_TUNNEL_INFO_PORT)
        env = dict(os.environ)
        env["GO_IOS_AGENT_PORT"] = str(resolved)
        proc = self.runner.popen(
            [binary, "tunnel", "start", "--userspace", "--tunnel-info-port=%d" % resolved],
            env=env,
        )
        self.tunnel_port = resolved
        return "go-ios tunnel 已启动 pid=%s port=%d" % (getattr(proc, "pid", "?"), resolved)

    def close(self) -> None:
        self._stop_location_process()
        super().close()
