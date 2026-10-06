"""路线回放引擎：把「路线 + 速度 + 循环模式」变成随时间推进的坐标流。

设计要点：
* 纯逻辑、无 IO，时钟可注入，可完全确定性地单元测试。
* tick() 由上层定时器调用；未在播放时只刷新时间基准，不产生坐标。
* loop=pingpong 时到终点反向，direction 表达当前行进方向。
"""

from __future__ import annotations

import math
import time
from typing import Callable, Optional

from .route import Route

STATE_IDLE = "idle"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"
STATE_FINISHED = "finished"

LOOP_NONE = "none"
LOOP_REPEAT = "loop"
LOOP_PINGPONG = "pingpong"
LOOP_MODES = (LOOP_NONE, LOOP_REPEAT, LOOP_PINGPONG)


class PlaybackError(RuntimeError):
    """回放状态或参数非法。"""


class Playback:
    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self.route: Optional[Route] = None
        self.speed = 0.0
        self.loop = LOOP_NONE
        self.state = STATE_IDLE
        self.distance = 0.0
        self.direction = 1
        self._last_tick: Optional[float] = None
        self._elapsed = 0.0

    # --- 装载与状态切换 ------------------------------------------------
    @staticmethod
    def validate_speed(speed: float) -> float:
        try:
            value = float(speed)
        except (TypeError, ValueError):
            raise PlaybackError("速度必须是有限的正数")
        if not math.isfinite(value) or value <= 0:
            raise PlaybackError("速度必须是有限的正数")
        return value

    def load(self, route: Route, speed: float, loop: str = LOOP_NONE) -> None:
        if route is None or not route.geometry:
            raise PlaybackError("路线为空，无法装载")
        speed = self.validate_speed(speed)
        if loop not in LOOP_MODES:
            raise PlaybackError("未知循环模式: %r" % (loop,))
        self.route = route
        self.speed = float(speed)
        self.loop = loop
        self.distance = 0.0
        self.direction = 1
        self.state = STATE_IDLE
        self._last_tick = None
        self._elapsed = 0.0

    def start(self) -> dict:
        if self.route is None:
            raise PlaybackError("尚未装载路线")
        self.validate_speed(self.speed)
        self.state = STATE_PLAYING
        self._last_tick = self._clock()
        return self.status()

    def pause(self) -> dict:
        if self.state == STATE_PLAYING:
            self.state = STATE_PAUSED
        self._last_tick = None
        return self.status()

    def resume(self) -> dict:
        if self.state == STATE_PAUSED:
            self.state = STATE_PLAYING
            self._last_tick = self._clock()
        return self.status()

    def stop(self) -> dict:
        self.state = STATE_IDLE
        self.distance = 0.0
        self.direction = 1
        self._last_tick = None
        self._elapsed = 0.0
        return self.status()

    # --- 推进 ----------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> Optional[dict]:
        """按真实时间推进；返回新位置或 None。"""
        now = self._clock() if now is None else now
        if self.state != STATE_PLAYING:
            self._last_tick = now
            return None
        last = self._last_tick if self._last_tick is not None else now
        dt = max(0.0, now - last)
        self._last_tick = now
        return self.advance(dt)

    def advance(self, dt: float) -> Optional[dict]:
        """按给定步长推进（确定性，供测试直接调用）。"""
        if self.state != STATE_PLAYING or self.route is None:
            return None
        dt = float(dt)
        if not math.isfinite(dt):
            raise PlaybackError("时间步长必须是有限数字")
        dt = max(0.0, dt)
        self._elapsed += dt
        total = self.route.distance
        if total <= 0:
            self.state = STATE_FINISHED
            return self._position(total)
        if dt == 0:
            return self._position(self.distance)
        travel = self.speed * dt
        if self.loop == LOOP_PINGPONG:
            phase = self.distance if self.direction > 0 else 2 * total - self.distance
            phase = (phase + travel) % (2 * total)
            self.distance = phase if phase < total else 2 * total - phase
            self.direction = 1 if phase < total else -1
        elif self.loop == LOOP_REPEAT:
            self.distance = (self.distance + travel) % total
        else:
            self.distance = min(total, self.distance + travel)
            if self.distance >= total:
                self.state = STATE_FINISHED
        return self._position(self.distance)

    def _position(self, distance: float) -> Optional[dict]:
        if self.route is None:
            return None
        pos = self.route.position_at(distance)
        if pos is None:
            return None
        pos["speed"] = self.speed
        pos["direction"] = self.direction
        return pos

    # --- 状态输出 ------------------------------------------------------
    def status(self) -> dict:
        total = self.route.distance if self.route else 0.0
        remaining = max(0.0, total - self.distance)
        return {
            "state": self.state,
            "loop": self.loop,
            "speed": self.speed,
            "distance": round(self.distance, 3),
            "total_distance": round(total, 3),
            "remaining_distance": round(remaining, 3),
            "progress": round(self.distance / total, 6) if total > 0 else 0.0,
            "eta_seconds": round(remaining / self.speed, 1) if self.speed > 0 else None,
            "direction": self.direction,
            "route_name": getattr(self.route, "name", None),
        }

    @property
    def playing(self) -> bool:
        return self.state == STATE_PLAYING
