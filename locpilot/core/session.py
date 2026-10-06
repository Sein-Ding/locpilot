"""会话编排：引擎 + 路线 + 回放 + 历史 + 事件流。

线程模型：
* HTTP 线程调用 Session 的公开方法（内部加锁）；
* 一个后台 ticker 线程按 tick_interval 推进回放并下发坐标；
* 状态变化通过订阅者回调推给 SSE，UI 不需要轮询。

错误策略：引擎下发失败不能杀死 ticker —— 记录错误、暂停回放、保留现场供 UI 展示，
否则一次 USB 抖动会让整个服务变得不可用。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Sequence

from .. import config
from ..engine import DEFAULT_PRIORITY, NO_DEVICE_HINT, auto_select, create, detect
from ..engine.base import DeviceInfo, Engine, EngineError
from ..engine.mock import MockEngine
from . import geo
from .gpx import route_from_gpx, to_gpx
from .places import PlaceClient
from .playback import LOOP_MODES, LOOP_NONE, STATE_PLAYING, Playback, PlaybackError
from .route import OsrmRouter, Route, build_route
from .store import Store

LOG_LIMIT = 200


class Session:
    def __init__(
        self,
        settings: Optional[dict] = None,
        store: Optional[Store] = None,
        engine: Optional[Engine] = None,
        clock: Optional[Callable[[], float]] = None,
        start_ticker: bool = True,
    ) -> None:
        self.settings = dict(settings or config.resolve_settings())
        self.lock = threading.RLock()
        # 最近一次成功构建的快照。读接口（/api/status）在写操作持锁时直接返回它，
        # 避免被 connect() 的设备枚举堵住十几秒 —— 前端会表现为"启动卡住"。
        self._snapshot_cache: Optional[dict] = None
        self.store = store if store is not None else Store()
        self.engine: Optional[Engine] = engine
        self.route: Optional[Route] = None
        self.playback = Playback(clock=clock)
        self.current: Optional[dict] = None
        self.error: Optional[str] = None
        self.logs: deque = deque(maxlen=LOG_LIMIT)
        self._subscribers: List[Callable[[str, dict], None]] = []
        self._ticker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.places = PlaceClient(
            base_url=config.NOMINATIM_URL,
            timeout=config.HTTP_TIMEOUT,
            user_agent=config.HTTP_USER_AGENT,
            offline=bool(self.settings.get("offline")),
        )
        self.router = OsrmRouter(
            base_url=config.OSRM_URL,
            timeout=config.HTTP_TIMEOUT,
            user_agent=config.HTTP_USER_AGENT,
            offline=bool(self.settings.get("offline")),
        )
        self.log("LocPilot %s 就绪" % config.VERSION)
        if start_ticker:
            self.start_ticker()

    # --- 日志与事件 ----------------------------------------------------
    def log(self, message: str, level: str = "info") -> dict:
        entry = {"ts": time.time(), "level": level, "message": str(message)}
        self.logs.append(entry)
        if level in ("error", "warn"):
            self.emit("log", entry)
        return entry

    def subscribe(self, callback: Callable[[str, dict], None]) -> Callable[[], None]:
        with self.lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self.lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def emit(self, event: str, payload: Optional[dict] = None) -> None:
        with self.lock:
            targets = list(self._subscribers)
        data = payload if payload is not None else {}
        for callback in targets:
            try:
                callback(event, data)
            except Exception:
                continue

    # --- 引擎 ----------------------------------------------------------
    def list_engines(self, probe: bool = False) -> List[dict]:
        return detect(settings=self._engine_settings(), probe_devices=probe)

    @staticmethod
    def _device_major(report: dict) -> Optional[int]:
        for device in report.get("devices") or []:
            head = str(device.get("ios_version") or "").split(".", 1)[0]
            try:
                return int(head)
            except ValueError:
                continue
        return None

    def _auto_engine(self) -> str:
        """自动选引擎：先按优先级排队，再按设备系统版本筛掉能力不足的引擎。

        libimobiledevice 能"看见" iOS 17+ 设备但无法模拟定位，若只按"是否发现设备"挑选，
        会挑中一个必然失败的引擎——所以必须用 capabilities.supports_ios17 过滤。
        """
        reports = self.list_engines(probe=True)
        with_devices = [r for r in reports if r.get("device_count") and r["name"] != MockEngine.name]
        if not with_devices:
            details = "; ".join(
                r["name"] + ": " + str(r.get("device_error"))[:120] for r in reports if r.get("device_error")
            )
            if details:
                raise EngineError(NO_DEVICE_HINT + "（引擎探测: " + details + "）")
            raise EngineError(NO_DEVICE_HINT)
        compatible = []
        incompatible = []
        for report in with_devices:
            major = self._device_major(report)
            supports = bool((report.get("capabilities") or {}).get("supports_ios17"))
            if major is not None and major >= 17 and not supports:
                incompatible.append(report)
            else:
                compatible.append(report)
        if compatible:
            return compatible[0]["name"]
        names = ", ".join(r["name"] for r in incompatible)
        major = self._device_major(incompatible[0])
        raise EngineError(
            "设备为 iOS %s，需要 pymobiledevice3 引擎，但该引擎当前未发现设备（当前仅 %s 能看到它）。"
            "请检查数据线/信任状态，或改用 --engine pymobiledevice3 重试。" % (major, names)
        )

    def _engine_settings(self) -> dict:
        keys = ("pmd3_bin", "pmd3_python", "pmd3_home", "goios_bin", "worker", "auto_mount", "rsd", "tunnel", "open_timeout", "offline")
        return {k: self.settings[k] for k in keys if k in self.settings}

    def _fallback_for(self, name: str, reports: List[dict]) -> Optional[tuple]:
        """显式选中的引擎与设备系统版本不匹配时，给出可用的替代引擎。

        用户没有义务记住"哪个引擎支持哪个 iOS 版本"，让界面报一句"请改用 X 引擎"
        却要他自己去找下拉框，是糟糕的交互；这里直接换成能用的引擎并说明原因。
        """
        chosen = next((r for r in reports if r.get("name") == name), None)
        if chosen is None:
            return None
        if bool((chosen.get("capabilities") or {}).get("supports_ios17")):
            return None
        # 只认真机引擎报告的版本；mock 也会报告 iOS 版本，混进来会让提示写成错误的系统版本
        major = None
        for report in reports:
            if report.get("name") == MockEngine.name or not report.get("device_count"):
                continue
            device_major = self._device_major(report)
            if device_major is not None:
                major = device_major
                break
        if major is None or major < 17:
            return None
        for candidate in DEFAULT_PRIORITY:
            if candidate == name:
                continue
            report = next((r for r in reports if r.get("name") == candidate), None)
            if not report or not report.get("device_count"):
                continue
            if (report.get("capabilities") or {}).get("supports_ios17"):
                return candidate, major
        return None

    def connect(self, engine_name: str = "auto", udid: Optional[str] = None) -> dict:
        with self.lock:
            self.playback.stop()
            self.current = None
            if self.engine is not None and getattr(self.engine, "opened", False):
                self.engine.close()
            self.emit("disconnected", {})
            name = engine_name or "auto"
            notes: List[str] = []
            if name == "auto":
                name = self._auto_engine()
            elif name != MockEngine.name:
                reports = self.list_engines(probe=True)
                fallback = self._fallback_for(name, reports)
                if fallback:
                    replacement, major = fallback
                    notes.append(
                        "%s 无法处理 iOS %s 设备，已自动改用 %s 引擎。" % (name, major, replacement)
                    )
                    self.log(notes[-1], "warn")
                    name = replacement
            engine = create(name, settings=self._engine_settings())
            device: DeviceInfo = engine.open(udid)
            self.engine = engine
            self.error = None
            self.log("已连接 %s（%s / iOS %s）" % (device.name or device.udid, name, device.ios_version or "?"))
            payload: dict = {"engine": name, "device": device.to_dict(), "notes": notes}
            self.emit("connected", payload)
            return payload

    def disconnect(self, clear: bool = True) -> dict:
        with self.lock:
            engine = self.engine
            if engine is None:
                return {"connected": False}
            try:
                if clear and getattr(engine, "opened", False):
                    engine.clear_location()
            except EngineError as exc:
                self.log("断开前清除定位失败: %s" % (exc,), "warn")
            engine.close()
            self.current = None
            self.playback.stop()
            self.log("已断开设备")
            self.emit("disconnected", {})
            return {"connected": False}

    def _require_engine(self) -> Engine:
        if self.engine is None or not getattr(self.engine, "opened", False):
            raise EngineError("尚未连接设备：请先调用 /api/connect")
        return self.engine

    # --- 定位动作 ------------------------------------------------------
    def teleport(self, lat: float, lon: float, label: Optional[str] = None, source: str = "manual") -> dict:
        lat, lon = geo.validate_point((lat, lon))
        with self.lock:
            engine = self._require_engine()
            if self.playback.playing:
                self.playback.stop()
                self.log("手动传送，已停止路线回放", "warn")
            engine.set_location(lat, lon)
            self.current = {"lat": lat, "lon": lon, "ts": time.time(), "source": source, "label": label}
            self.error = None
            entry = self.store.add_history(lat, lon, label=label, source=source)
            self.emit("position", dict(self.current))
            return {"position": self.current, "history_id": entry["id"]}

    def clear_location(self) -> dict:
        with self.lock:
            engine = self._require_engine()
            self.playback.stop()
            engine.clear_location()
            self.current = None
            self.emit("position", {"cleared": True})
            self.log("已清除虚拟定位，设备恢复真实 GPS")
            return {"cleared": True}

    def joystick(self, bearing: float, meters: float, label: Optional[str] = None) -> dict:
        base = self.current or (self.engine.last_position if self.engine else None)
        if not base:
            raise EngineError("没有当前位置，请先传送或开启路线")
        target = geo.move_by((base["lat"], base["lon"]), float(bearing) % 360.0, float(meters))
        return self.teleport(target[0], target[1], label=label, source="joystick")

    # --- 路线 ----------------------------------------------------------
    def set_route(
        self,
        points: Sequence[Sequence[float]],
        profile: str = "driving",
        speed: Optional[float] = None,
        loop: str = LOOP_NONE,
        use_router: Optional[bool] = None,
        name: Optional[str] = None,
    ) -> dict:
        if not points:
            raise PlaybackError("路线至少需要一个点")
        if loop not in LOOP_MODES:
            raise PlaybackError("未知循环模式: %r" % (loop,))
        use_router = bool(self.settings.get("use_router", True)) if use_router is None else bool(use_router)
        speed = float(speed if speed is not None else self.settings.get("default_speed", config.SPEED_PRESETS["walk"]))
        route = build_route(
            points,
            profile=profile,
            use_router=use_router,
            router=self.router,
            step_m=float(self.settings.get("densify_step", config.DENSIFY_STEP_M)),
            name=name,
        )
        with self.lock:
            self.playback.load(route, speed=speed, loop=loop)
            self.route = route
            self.log("已装载路线：%d 个途经点 / %s 米%s" % (len(route.waypoints), int(route.distance), "（道路）" if route.routed else "（直线）"))
            payload = self.route_summary()
            self.emit("route", payload)
            return payload

    def import_gpx(self, text: str, speed: Optional[float] = None, loop: str = LOOP_NONE) -> dict:
        route = route_from_gpx(text)
        with self.lock:
            self.playback.load(route, speed=float(speed if speed is not None else self.settings.get("default_speed", 1.4)), loop=loop)
            self.route = route
            self.log("已导入 GPX：%s（%d 点 / %s 米）" % (route.name, len(route.geometry), int(route.distance)))
            payload = self.route_summary()
            self.emit("route", payload)
            return payload

    def export_gpx(self) -> str:
        with self.lock:
            if self.route is None:
                raise PlaybackError("当前没有路线可导出")
            return to_gpx(self.route.geometry, name=self.route.name or "LocPilot route", description="由 LocPilot 导出")

    def route_summary(self) -> dict:
        if self.route is None:
            return {"loaded": False}
        data = self.route.to_dict()
        data["loaded"] = True
        data["waypoint_count"] = len(self.route.waypoints)
        data["geometry_count"] = len(self.route.geometry)
        return data

    def start_route(self, speed: Optional[float] = None, loop: Optional[str] = None) -> dict:
        with self.lock:
            self._require_engine()
            if self.route is None:
                raise PlaybackError("请先设置路线")
            new_speed = Playback.validate_speed(self.playback.speed if speed is None else speed)
            if loop is not None:
                if loop not in LOOP_MODES:
                    raise PlaybackError("未知循环模式: %r" % (loop,))
                self.playback.loop = loop
            self.playback.speed = new_speed
            status = self.playback.start()
            self.log("开始回放：速度 %.2f m/s，循环 %s" % (self.playback.speed, self.playback.loop))
            self.emit("playback", status)
            return status

    def pause_route(self) -> dict:
        with self.lock:
            status = self.playback.pause()
            self.log("回放暂停")
            self.emit("playback", status)
            return status

    def resume_route(self) -> dict:
        with self.lock:
            status = self.playback.resume()
            self.emit("playback", status)
            return status

    def stop_route(self) -> dict:
        with self.lock:
            status = self.playback.stop()
            self.log("回放停止")
            self.emit("playback", status)
            return status

    # --- 回放推进 ------------------------------------------------------
    def tick(self) -> Optional[dict]:
        with self.lock:
            if not self.playback.playing:
                return None
            position = self.playback.tick()
            if position is None:
                return None
            engine = self.engine
            if engine is None or not getattr(engine, "opened", False):
                self.playback.pause()
                self.error = "设备已断开，回放已暂停"
                self.log(self.error, "error")
                self.emit("error", {"message": self.error})
                return None
            try:
                engine.set_location(position["lat"], position["lon"])
            except EngineError as exc:
                self.playback.pause()
                self.error = str(exc)
                self.log("下发坐标失败，回放已暂停：%s" % (exc,), "error")
                self.emit("error", {"message": self.error})
                return None
            self.current = {
                "lat": position["lat"],
                "lon": position["lon"],
                "ts": time.time(),
                "source": "route",
                "bearing": position.get("bearing"),
                "distance": position.get("distance"),
            }
            self.error = None
            self.emit("position", dict(self.current))
            return self.current

    def start_ticker(self) -> None:
        if self._ticker is not None and self._ticker.is_alive():
            return
        self._stop_event.clear()
        self._ticker = threading.Thread(target=self._run_ticker, name="locpilot-ticker", daemon=True)
        self._ticker.start()

    def _run_ticker(self) -> None:
        interval = float(self.settings.get("tick_interval", config.TICK_INTERVAL)) or config.TICK_INTERVAL
        while not self._stop_event.wait(interval):
            try:
                self.tick()
            except Exception as exc:  # ticker 绝不能因单次异常退出
                self.log("ticker 异常: %s" % (exc,), "error")

    def stop_ticker(self) -> None:
        self._stop_event.set()
        if self._ticker is not None:
            self._ticker.join(timeout=3)
            self._ticker = None

    # --- 状态快照 ------------------------------------------------------
    def runtime_info(self) -> dict:
        """运行期诊断：加载的是哪份代码、哪个解释器、引擎路径来自哪里。

        排查"App 里连不上设备、命令行却正常"这类问题时，这一节是决定性的证据。
        """
        import os
        import sys
        from pathlib import Path

        paths = config.bundled_paths()
        return {
            "package": str(Path(__file__).resolve().parent.parent / "locpilot"),
            "python": sys.executable,
            "bundled_root": paths.get("root"),
            "bundled_pmd3_exists": Path(paths.get("venv_pmd3", "")).exists(),
            "bundled_python_exists": Path(paths.get("venv_python", "")).exists(),
            "env_pmd3": os.environ.get("LOCPILOT_PMD3") or None,
            "env_pmd3_python": os.environ.get("LOCPILOT_PMD3_PYTHON") or None,
            "path_head": os.environ.get("PATH", "")[:120],
            "cwd": os.getcwd(),
            "data_dir": str(config.data_dir()),
        }

    def snapshot(self) -> dict:
        # 读接口绝不阻塞：写操作（connect/teleport）持锁时直接给上一份快照。
        # 实测无设备时 /api/connect 的枚举要 14 秒，期间 /api/status 会一直等锁，
        # App 启动看起来就像卡死（冒烟与端到端也因此误报超时）。
        if not self.lock.acquire(timeout=0.2):
            cached = self._snapshot_cache
            if cached is not None:
                return cached
            self.lock.acquire()          # 首次调用还没有缓存，只能等
        try:
            return self._snapshot_locked()
        finally:
            self.lock.release()

    def _snapshot_locked(self) -> dict:
        if True:
            engine = self.engine
            snapshot = {
                "app": {"name": config.APP_TITLE, "version": config.VERSION},
                "runtime": self.runtime_info(),
                "engine": {
                    "name": getattr(engine, "name", None),
                    "label": getattr(engine, "label", None),
                    "opened": bool(getattr(engine, "opened", False)),
                    "mode": getattr(engine, "mode", None),
                    "device": engine.device.to_dict() if engine is not None and engine.device else None,
                    "last_position": getattr(engine, "last_position", None),
                },
                "position": self.current,
                "playback": self.playback.status(),
                "route": self.route_summary(),
                "error": self.error,
                "settings": {
                    "tick_interval": self.settings.get("tick_interval", config.TICK_INTERVAL),
                    "default_speed": self.settings.get("default_speed"),
                    "speed_presets": dict(config.SPEED_PRESETS),
                    "min_speed": config.MIN_SPEED,
                    "max_speed": config.MAX_SPEED,
                    "use_router": self.settings.get("use_router", True),
                    "offline": bool(self.settings.get("offline")),
                },
                "tile_url": self.settings.get("tile_url", config.TILE_URL),
                "tile_attribution": config.TILE_ATTRIBUTION,
                "tile_provider": self.settings.get("tile_provider", config.DEFAULT_TILE_PROVIDER),
                "tile_providers": config.TILE_PROVIDERS,
                "logs": list(self.logs)[-30:],
            }
            self._snapshot_cache = snapshot
            return snapshot

    def shutdown(self) -> None:
        self.stop_ticker()
        try:
            if self.engine is not None and getattr(self.engine, "opened", False):
                self.engine.close()
        except Exception:
            pass
