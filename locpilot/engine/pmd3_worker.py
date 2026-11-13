"""pymobiledevice3 常驻定位工作进程（stdin/stdout JSON Lines 协议）。

为什么需要常驻进程：pymobiledevice3 的 CLI set 在设置坐标后会阻塞等待 SIGINT，
每次改坐标都重启进程会反复建立 tunnel / DTX 连接（iOS 17+ 上是秒级开销）；
本进程只建一次会话，之后按行接收坐标，实现亚秒级连续移动。

协议（stdin 一行一个 JSON，stdout 一一对应）：
  {"id":1,"cmd":"open","udid":"...","rsd":["host",port],"tunnel":"UDID","mount":true}
  {"id":2,"cmd":"set","lat":31.2,"lon":121.4}
  {"id":3,"cmd":"clear"}
  {"id":4,"cmd":"ping"}
  {"id":5,"cmd":"close"}
响应：{"id":n,"ok":true,"detail":{...}} 或 {"id":n,"ok":false,"error":"..."}

该文件必须能被 venv 里的 python 直接执行；除 pymobiledevice3 外只依赖标准库。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import traceback
from contextlib import AsyncExitStack

PROTOCOL_VERSION = 1


def _inproc() -> bool:
    """与 engine/pmd3.inproc_enabled() 同一判据（此处避免循环导入，故重复实现）。"""
    if os.environ.get("LOCPILOT_PMD3_INPROC") == "1":
        return True
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


class Session:
    """持有 lockdown/RSD 与定位服务的一次性会话。"""

    def __init__(self) -> None:
        self.stack = AsyncExitStack()
        self.lockdown = None
        self.simulation = None
        self.mode = None
        self.product_version = None
        self.udid = None
        self.detail = {}
        self._tunnel = None      # 进程内模式持有的 UserspaceRsdTunnel（可关闭句柄）

    async def open(self, udid=None, rsd=None, tunnel=None, mount=False) -> dict:
        await self.close()
        from pymobiledevice3.lockdown import create_using_usbmux

        self.lockdown = await create_using_usbmux(serial=udid, autopair=False)
        self.product_version = await self._product_version(self.lockdown)
        self.udid = getattr(self.lockdown, "identifier", None) or udid
        major = self._major(self.product_version)

        # 进程内模式：没有 pymobiledevice3 命令行，挂载要改用库调用；
        # 且 iOS 17.4+（尤其 iOS 27 的 Cryptex1）必须经 RSD 挂载，所以顺序改成"先建 RSD 再挂"。
        if mount and not _inproc():
            self.detail["mount"] = await self._auto_mount()

        if major is not None and major >= 17:
            provider = await self._rsd_provider(udid, rsd=rsd, tunnel=tunnel)
            from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
            from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

            closer = getattr(provider, "close", None)
            if callable(closer):
                self.detail["rsd_closer"] = type(provider).__name__
                self.stack.push_async_callback(closer)
            if mount and _inproc():
                self.detail["mount"] = await self._auto_mount(provider)
            dvt = await self._enter(DvtProvider(provider))
            self.simulation = await self._enter(LocationSimulation(dvt))
            self.mode = "dvt"
        else:
            from pymobiledevice3.services.simulate_location import DtSimulateLocation

            if mount and _inproc():
                self.detail["mount"] = await self._auto_mount(self.lockdown)
            self.simulation = await self._enter(DtSimulateLocation(self.lockdown))
            self.mode = "legacy"

        self.detail.update({"mode": self.mode, "product_version": self.product_version, "udid": self.udid})
        return dict(self.detail)

    async def set(self, lat: float, lon: float) -> dict:
        if self.simulation is None:
            raise RuntimeError("会话尚未打开")
        await self.simulation.set(float(lat), float(lon))
        return {"mode": self.mode, "lat": float(lat), "lon": float(lon)}

    async def clear(self) -> dict:
        if self.simulation is None:
            raise RuntimeError("会话尚未打开")
        await self.simulation.clear()
        return {"mode": self.mode, "cleared": True}

    async def close(self) -> None:
        try:
            await self.stack.aclose()
        except Exception:
            pass
        self.stack = AsyncExitStack()
        if self.lockdown is not None:
            closer = getattr(self.lockdown, "close", None)
            if callable(closer):
                await closer()
        self.simulation = None
        self.lockdown = None
        self.mode = None
        self.detail = {}
        if self._tunnel is not None:
            try:
                await self._tunnel.aclose()
            except Exception:
                pass
            self._tunnel = None

    # --- 内部 ----------------------------------------------------------
    async def _enter(self, obj):
        """兼容有/无 __aenter__ 的服务对象。"""
        if hasattr(obj, "__aenter__"):
            return await self.stack.enter_async_context(obj)
        return obj

    @staticmethod
    def _major(version):
        if not version:
            return None
        try:
            return int(str(version).split(".", 1)[0])
        except ValueError:
            return None

    @staticmethod
    async def _product_version(lockdown):
        info = getattr(lockdown, "short_info", None)
        if isinstance(info, dict) and info.get("ProductVersion"):
            return str(info["ProductVersion"])
        value = getattr(lockdown, "product_version", None)
        if value:
            return str(value)
        try:
            value = await lockdown.get_value(key="ProductVersion")
            if value:
                return str(value)
        except Exception:
            pass
        return None

    async def _rsd_provider(self, udid, rsd=None, tunnel=None):
        """iOS 17+ 需要 RSD：显式 --rsd / tunneld / 无 root 用户态隧道，三级降级。"""
        if rsd:
            from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService

            service = RemoteServiceDiscoveryService(tuple(rsd))
            await service.connect()
            return service
        if tunnel:
            from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid

            return await get_tunneld_device_by_udid(tunnel)
        try:
            from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid

            return await get_tunneld_device_by_udid(self.udid or udid or "")
        except Exception as exc:
            self.detail["tunneld_error"] = str(exc)[:200]
        from pymobiledevice3.remote import userspace_tunnel as ut

        try:
            if _inproc():
                # 库自身文档：Embedders should use UserspaceRsdTunnel directly — it is a closeable handle。
                # establish_userspace_rsd 把隧道存进模块全局（进程退出才关），进程内复用/重连会踩
                # one-tunnel-per-process，所以这里自己持有句柄，close() 时 aclose()。
                tunnel_obj = ut.UserspaceRsdTunnel(serial=self.udid or udid)
                rsd = await tunnel_obj.aopen()
                self._tunnel = tunnel_obj
                return rsd
            return await ut.establish_userspace_rsd(serial=self.udid or udid)
        except Exception as exc:
            raise RuntimeError(
                "iOS 17+ 需要 RSD 隧道，且自动建立失败(%s)。请先运行: sudo pymobiledevice3 remote tunneld，"
                "或使用 --rsd HOST PORT 指定隧道" % (exc,)
            )

    @staticmethod
    async def _auto_mount(provider=None) -> str:
        """best-effort 挂载 Developer Disk Image；失败只记录不阻断。

        进程内（Windows 打包）没有 pymobiledevice3 命令行，改用库里的 auto_mount；
        iOS 17+ 传 RSD provider（Cryptex1 DDI 必须经 RSD），老系统传 lockdown。
        """
        try:
            if _inproc():
                if provider is None:
                    return "auto-mount 跳过：缺少 lockdown/RSD 句柄"
                from pymobiledevice3.services.mobile_image_mounter import auto_mount

                await auto_mount(provider)
                return "auto-mount ok（库调用）"
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pymobiledevice3", "mounter", "auto-mount",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
            return (out or b"").decode("utf-8", "replace").strip()[-300:]
        except Exception as exc:
            return "auto-mount 失败: %s" % (exc,)


class Bridge:
    """把阻塞的 stdin 读取与 asyncio 事件循环桥接起来。"""

    def __init__(self) -> None:
        # Windows 默认 ProactorEventLoop 不支持 add_reader，Wi-Fi(QUIC/aioquic) 会失败；
        # 进程内模式不再需要 create_subprocess，因此可以安全地换 Selector 事件循环。
        if _inproc() and os.name == "nt":
            policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
            if policy is not None:
                asyncio.set_event_loop_policy(policy())
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="locpilot-pmd3-loop", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)

    def shutdown(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


def handle(bridge: Bridge, session: Session, message: dict) -> dict:
    cmd = message.get("cmd")
    if cmd == "ping":
        return {"protocol": PROTOCOL_VERSION, "pid": None}
    if cmd == "open":
        return bridge.call(
            session.open(
                udid=message.get("udid"),
                rsd=message.get("rsd"),
                tunnel=message.get("tunnel"),
                mount=bool(message.get("mount")),
            ),
            timeout=float(message.get("timeout") or 120.0),
        )
    if cmd == "set":
        return bridge.call(session.set(message.get("lat"), message.get("lon")), timeout=float(message.get("timeout") or 20.0))
    if cmd == "clear":
        return bridge.call(session.clear(), timeout=float(message.get("timeout") or 20.0))
    if cmd == "close":
        bridge.call(session.close(), timeout=20.0)
        return {"closed": True}
    raise ValueError("未知命令: %r" % (cmd,))


def main() -> int:
    bridge = Bridge()
    session = Session()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            print(json.dumps({"id": None, "ok": False, "error": "JSON 解析失败: %s" % (exc,)}), flush=True)
            continue
        try:
            detail = handle(bridge, session, message)
            print(json.dumps({"id": message.get("id"), "ok": True, "detail": detail}, ensure_ascii=False), flush=True)
        except Exception as exc:  # 单条命令失败不能杀死常驻进程
            print(
                json.dumps(
                    {"id": message.get("id"), "ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "trace": traceback.format_exc()[-800:]},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if message.get("cmd") == "close":  # 正常关闭也要退出，否则父进程 terminate 前会挂住
            break
    try:
        bridge.call(session.close(), timeout=10.0)
    except Exception:
        pass
    bridge.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
