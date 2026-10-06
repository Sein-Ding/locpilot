"""引擎抽象：设备枚举、定位会话与能力声明。

所有具体引擎（pymobiledevice3 / libimobiledevice / go-ios / mock）都实现同一组方法，
上层 Session 与 HTTP API 只依赖这里定义的契约。

约定：
* list_devices() 只读枚举，不做配对、不弹信任提示。
* open() 建立可复用的定位会话；set_location() 可在会话内高频调用。
* 引擎必须把外部命令的 stderr 摘要带进异常，否则用户无法自助排障。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence


class EngineError(RuntimeError):
    """引擎运行期错误。"""


class EngineUnavailable(EngineError):
    """引擎不可用（缺少依赖、平台不支持等）。"""


class DeviceNotFound(EngineError):
    """未发现可用设备。"""


class CommandResult:
    def __init__(self, argv: Sequence[str], returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.argv = list(argv)
        self.returncode = int(returncode)
        self.stdout = stdout or ""
        self.stderr = stderr or ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, limit: int = 300) -> str:
        text = (self.stderr or self.stdout or "").strip()
        return text[-limit:]

    def to_dict(self) -> dict:
        return {"argv": self.argv, "returncode": self.returncode, "stdout": self.stdout[-2000:], "stderr": self.stderr[-2000:]}


class Runner:
    """子进程执行器；测试通过替换它来断言命令行构造，不真正执行。"""

    def run(self, argv: Sequence[str], timeout: float = 30.0, input_text: Optional[str] = None, env=None) -> CommandResult:
        try:
            proc = subprocess.run(
                list(argv),
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise EngineUnavailable("命令不存在: %s (%s)" % (argv[0], exc))
        except subprocess.TimeoutExpired as exc:
            raise EngineError("命令超时(%.0fs): %s" % (timeout, " ".join(argv)))
        return CommandResult(argv, proc.returncode, proc.stdout, proc.stderr)

    def popen(self, argv: Sequence[str], env=None) -> subprocess.Popen:
        try:
            return subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
        except FileNotFoundError as exc:
            raise EngineUnavailable("命令不存在: %s (%s)" % (argv[0], exc))


class DeviceInfo:
    def __init__(
        self,
        udid: str,
        name: Optional[str] = None,
        product_type: Optional[str] = None,
        ios_version: Optional[str] = None,
        connection: str = "usb",
        engine: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> None:
        self.udid = udid
        self.name = name
        self.product_type = product_type
        self.ios_version = ios_version
        self.connection = connection
        self.engine = engine
        self.extra = dict(extra or {})

    @property
    def ios_major(self) -> Optional[int]:
        if not self.ios_version:
            return None
        head = str(self.ios_version).split(".", 1)[0]
        try:
            return int(head)
        except ValueError:
            return None

    @property
    def needs_tunnel(self) -> bool:
        major = self.ios_major
        return bool(major and major >= 17)

    def to_dict(self) -> dict:
        return {
            "udid": self.udid,
            "name": self.name,
            "product_type": self.product_type,
            "ios_version": self.ios_version,
            "connection": self.connection,
            "engine": self.engine,
            "needs_tunnel": self.needs_tunnel,
            "extra": self.extra,
        }


class Engine:
    name = "base"
    label = "基础引擎"
    capabilities: Dict[str, object] = {}

    def __init__(self, runner: Optional[Runner] = None, settings: Optional[dict] = None) -> None:
        self.runner = runner or Runner()
        self.settings = dict(settings or {})
        self.device: Optional[DeviceInfo] = None
        self.opened = False
        self.last_position: Optional[dict] = None
        self.error: Optional[str] = None

    # --- 契约 ----------------------------------------------------------
    def availability(self) -> tuple:
        """返回 (是否可用, 原因/路径说明)。"""
        return False, "未实现"

    def list_devices(self) -> List[DeviceInfo]:
        raise EngineUnavailable("%s 不支持设备枚举" % self.name)

    def open(self, udid: Optional[str] = None) -> DeviceInfo:
        raise EngineUnavailable("%s 无法打开会话" % self.name)

    def set_location(self, lat: float, lon: float) -> None:
        raise EngineUnavailable("%s 无法下发坐标" % self.name)

    def clear_location(self) -> None:
        raise EngineUnavailable("%s 无法清除定位" % self.name)

    def close(self) -> None:
        self.opened = False
        self.device = None

    # --- 辅助 ----------------------------------------------------------
    def require_open(self) -> DeviceInfo:
        if not self.opened or self.device is None:
            raise EngineError("引擎 %s 尚未打开设备会话" % self.name)
        return self.device

    def describe(self) -> dict:
        available, reason = self.availability()
        return {
            "name": self.name,
            "label": self.label,
            "available": bool(available),
            "reason": reason,
            "capabilities": dict(self.capabilities),
            "opened": self.opened,
            "device": self.device.to_dict() if self.device else None,
            "last_position": self.last_position,
            "error": self.error,
        }


EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", str(Path.home() / ".local" / "bin"))


def which(binary: str) -> Optional[str]:
    """在 PATH 之外也搜索 Homebrew/用户级目录：GUI 与沙箱环境常常没有完整 PATH。"""
    if not binary:
        return None
    found = shutil.which(binary)
    if found:
        return found
    candidate = Path(binary)
    if candidate.is_absolute() and candidate.exists():
        return str(candidate)
    for directory in EXTRA_BIN_DIRS:
        target = Path(directory) / binary
        if target.exists() and os.access(str(target), os.X_OK):
            return str(target)
    return None
