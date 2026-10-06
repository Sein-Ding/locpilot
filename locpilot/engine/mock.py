"""Mock 引擎：没有真机时也能完整跑通 UI / API / 回放链路。

用途：
1. 演示与教学（无设备即可看到坐标流动）；
2. 自动化测试与 CDP 浏览器验收（不依赖 USB、不依赖 macOS）；
3. 故障注入（fail_after 模拟设备掉线），验证上层错误处理。
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

from .base import DeviceInfo, Engine, EngineError

DEFAULT_UDID = "MOCK-0000-0000-0000-000000000001"


class MockEngine(Engine):
    name = "mock"
    label = "虚拟设备（演示/测试）"
    capabilities = {
        "persistent_session": True,
        "supports_ios17": True,
        "needs_tunnel": False,
        "needs_sudo": False,
        "supports_gpx_play": False,
        "real_device": False,
    }

    def __init__(self, runner=None, settings=None) -> None:
        super().__init__(runner=runner, settings=settings)
        self._lock = threading.RLock()
        self.set_calls: List[dict] = []
        self.clear_calls = 0
        self.fail_after: Optional[int] = self.settings.get("fail_after")
        self.latency: float = float(self.settings.get("latency") or 0.0)
        self.fail_next = False

    def availability(self):
        return True, "内置虚拟设备"

    def list_devices(self) -> List[DeviceInfo]:
        return [
            DeviceInfo(
                udid=DEFAULT_UDID,
                name="LocPilot 虚拟 iPhone",
                product_type="iPhone15,3",
                ios_version="18.2",
                connection="virtual",
                engine=self.name,
                extra={"note": "由 mock 引擎提供，用于演示与自动化验证"},
            )
        ]

    def open(self, udid: Optional[str] = None) -> DeviceInfo:
        devices = self.list_devices()
        target = None
        for dev in devices:
            if udid is None or dev.udid == udid:
                target = dev
                break
        if target is None:
            raise EngineError("未找到虚拟设备: %s" % (udid,))
        self.device = target
        self.opened = True
        self.error = None
        return target

    def set_location(self, lat: float, lon: float) -> None:
        self.require_open()
        if self.latency:
            time.sleep(self.latency)
        with self._lock:
            if self.fail_next:
                self.fail_next = False
                self.error = "注入的定位失败"
                raise EngineError("注入的定位失败")
            if self.fail_after is not None and len(self.set_calls) >= int(self.fail_after):
                self.error = "虚拟设备已掉线（fail_after 触发）"
                raise EngineError(self.error)
            self.last_position = {"lat": float(lat), "lon": float(lon), "ts": time.time()}
            self.set_calls.append(dict(self.last_position))

    def clear_location(self) -> None:
        self.require_open()
        with self._lock:
            self.clear_calls += 1
            self.last_position = None
        self.error = None

    def close(self) -> None:
        super().close()
