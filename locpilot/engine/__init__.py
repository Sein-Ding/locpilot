"""引擎注册表：统一创建、可用性探测与自动选择。"""

from __future__ import annotations

from typing import Dict, List, Optional

from .base import CommandResult, DeviceInfo, Engine, EngineError, EngineUnavailable, Runner
from .goios import GoIosEngine
from .legacy import LibIMobileDeviceEngine
from .mock import MockEngine
from .pmd3 import PyMobileDevice3Engine

ENGINE_CLASSES: Dict[str, type] = {
    PyMobileDevice3Engine.name: PyMobileDevice3Engine,
    GoIosEngine.name: GoIosEngine,
    LibIMobileDeviceEngine.name: LibIMobileDeviceEngine,
    MockEngine.name: MockEngine,
}

# 真机引擎优先级：pymobiledevice3 覆盖最全（含 iOS 17+），go-ios 为 MIT 备选，libimobiledevice 兜底到 iOS 16
DEFAULT_PRIORITY = (
    PyMobileDevice3Engine.name,
    GoIosEngine.name,
    LibIMobileDeviceEngine.name,
)
ENGINE_NAMES = tuple(ENGINE_CLASSES.keys())


def create(name: str, runner: Optional[Runner] = None, settings: Optional[dict] = None) -> Engine:
    key = (name or "").strip()
    if key == "auto" or not key:
        key = "mock"
    if key not in ENGINE_CLASSES:
        raise EngineError("未知引擎: %s（可选: %s）" % (name, ", ".join(ENGINE_NAMES)))
    return ENGINE_CLASSES[key](runner=runner, settings=settings)


def detect(settings: Optional[dict] = None, runner: Optional[Runner] = None, probe_devices: bool = False) -> List[dict]:
    """列出所有引擎的可用性；probe_devices=True 时额外枚举设备（会触发 USB 访问）。"""
    out: List[dict] = []
    for name in ENGINE_NAMES:
        engine = create(name, runner=runner, settings=settings)
        info = engine.describe()
        if probe_devices:
            try:
                devices = engine.list_devices()
                info["devices"] = [d.to_dict() for d in devices]
                info["device_count"] = len(devices)
            except Exception as exc:
                info["devices"] = []
                info["device_count"] = 0
                info["device_error"] = str(exc)[:300]
        out.append(info)
    return out


NO_DEVICE_HINT = (
    "未发现 iOS 设备：请确认①数据线能传数据（不是纯充电线）②手机已解锁——iOS 重启后首次解锁前"
    "不提供 USB 数据服务③手机上弹「要信任此电脑吗？」时点信任。若只想无设备演示，请把引擎显式选为 mock。"
)


def auto_select(settings: Optional[dict] = None, runner: Optional[Runner] = None, allow_mock: bool = True) -> str:
    """按优先级挑选第一个「可用且发现设备」的真机引擎。

    allow_mock=False 时绝不静默降级到虚拟设备——否则用户会看到"已连接"却根本不是自己的手机，
    这是必须显式失败而不是假装成功的场景。
    """
    for name in DEFAULT_PRIORITY:
        engine = create(name, runner=runner, settings=settings)
        available, _reason = engine.availability()
        if not available:
            continue
        try:
            if engine.list_devices():
                return name
        except Exception:
            continue
    if allow_mock:
        return MockEngine.name
    raise EngineError(NO_DEVICE_HINT)
