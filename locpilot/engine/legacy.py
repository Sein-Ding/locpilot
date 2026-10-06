"""libimobiledevice 引擎：通过 idevicesetlocation 控制 iOS 16 及以下设备的定位。

事实边界（离线验证自 libimobiledevice 1.4.0 与上游源码）：
* idevicesetlocation 走 com.apple.dt.simulatelocation 服务，iOS 17 起该服务不可用，
  工具会直接打印 "not supported on iOS 17+"；因此本引擎在 iOS>=17 时主动拒绝并提示切换引擎。
* 每次调用只发一条消息就退出；定位是设备侧状态，reset 或重启才会恢复真实定位。
"""

from __future__ import annotations

import os
import shutil
import time
from typing import List, Optional

from .base import DeviceInfo, Engine, EngineError, EngineUnavailable, which as which_bin


class LibIMobileDeviceEngine(Engine):
    name = "libimobiledevice"
    label = "libimobiledevice / idevicesetlocation（iOS 16 及以下）"
    capabilities = {
        "persistent_session": False,
        "supports_ios17": False,
        "needs_tunnel": False,
        "needs_sudo": False,
        "supports_gpx_play": False,
        "real_device": True,
    }

    def _bin(self, name: str) -> Optional[str]:
        env_key = "LOCPILOT_" + name.upper().replace("-", "_")
        explicit = self.settings.get(name.replace("-", "_")) or os.environ.get(env_key)
        if explicit and which_bin(explicit):
            return explicit
        return which_bin(name)

    def availability(self):
        binary = self._bin("idevicesetlocation")
        if not binary:
            return False, "未找到 idevicesetlocation（brew install libimobiledevice）"
        return True, "binary=%s" % binary

    def list_devices(self) -> List[DeviceInfo]:
        id_bin = self._bin("idevice_id")
        if not id_bin:
            raise EngineUnavailable("未找到 idevice_id")
        result = self.runner.run([id_bin, "-l"], timeout=20.0)
        if not result.ok:
            raise EngineError("idevice_id -l 失败: %s" % (result.tail(),))
        devices: List[DeviceInfo] = []
        for udid in [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]:
            info = {"ProductVersion": None, "DeviceName": None, "ProductType": None}
            info_bin = self._bin("ideviceinfo")
            if info_bin:
                for key in list(info.keys()):
                    probe = self.runner.run([info_bin, "-u", udid, "-k", key], timeout=15.0)
                    if probe.ok and (probe.stdout or "").strip():
                        info[key] = (probe.stdout or "").strip()
            devices.append(
                DeviceInfo(
                    udid=udid,
                    name=info.get("DeviceName"),
                    product_type=info.get("ProductType"),
                    ios_version=info.get("ProductVersion"),
                    connection="usb",
                    engine=self.name,
                )
            )
        return devices

    def open(self, udid: Optional[str] = None) -> DeviceInfo:
        devices = self.list_devices()
        if not devices:
            raise EngineError("未发现 iOS 设备：请用 USB 连接并在手机上点信任")
        target = None
        for dev in devices:
            if udid is None or dev.udid == udid:
                target = dev
                break
        if target is None:
            raise EngineError("未找到指定设备: %s" % (udid,))
        major = target.ios_major
        if major is None or major >= 17:
            raise EngineError(
                "libimobiledevice 不支持 iOS 17+（当前 %s）。请在引擎下拉框选择「自动选择」或 pymobiledevice3；"
                "若手机系统低于 17 则本引擎可用。" % (target.ios_version or "未知版本",)
            )
        self.device = target
        self.opened = True
        self.error = None
        return target

    def set_location(self, lat: float, lon: float) -> None:
        device = self.require_open()
        binary = self._bin("idevicesetlocation")
        if not binary:
            raise EngineUnavailable("未找到 idevicesetlocation")
        result = self.runner.run(
            [binary, "-u", device.udid, "--", "%.6f" % float(lat), "%.6f" % float(lon)], timeout=30.0
        )
        if not result.ok:
            self.error = result.tail()
            raise EngineError("设置定位失败: %s" % (self.error,))
        self.last_position = {"lat": float(lat), "lon": float(lon), "ts": time.time()}
        self.error = None

    def clear_location(self) -> None:
        device = self.require_open()
        binary = self._bin("idevicesetlocation")
        if not binary:
            raise EngineUnavailable("未找到 idevicesetlocation")
        result = self.runner.run([binary, "-u", device.udid, "reset"], timeout=30.0)
        if not result.ok:
            raise EngineError("清除定位失败: %s" % (result.tail(),))
        self.last_position = None
