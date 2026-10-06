"""HTTP API 路由：与 socket 解耦的纯分发层，便于单元测试。

返回三元组 (status, payload, content_type)，payload 为 dict 时序列化为 JSON，
为 str 时按 content_type 原样返回（GPX 导出用）。
"""

from __future__ import annotations

import json
from typing import Any, Optional, Tuple
from urllib.parse import parse_qs

from . import config
from .core import geo
from .core.gpx import GpxError
from .core.places import PlaceError
from .core.playback import PlaybackError
from .core.route import RouteError
from .engine.base import EngineError, EngineUnavailable

JSON = "application/json; charset=utf-8"
GPX = "application/gpx+xml; charset=utf-8"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _as_float(data: dict, key: str, required: bool = True, default: Optional[float] = None) -> Optional[float]:
    if key not in data or data[key] is None or data[key] == "":
        if required:
            raise ApiError("缺少参数: %s" % key)
        return default
    try:
        return float(data[key])
    except (TypeError, ValueError):
        raise ApiError("参数 %s 必须是数字" % key)


def _points_from(data: dict) -> list:
    raw = data.get("points")
    if raw is None:
        raise ApiError("缺少参数: points")
    points = []
    for item in raw:
        if isinstance(item, dict):
            points.append((_as_float(item, "lat"), _as_float(item, "lon")))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            points.append((float(item[0]), float(item[1])))
        else:
            raise ApiError("points 元素必须是 {lat,lon} 或 [lat,lon]")
    if not points:
        raise ApiError("points 不能为空")
    return points


def handle(session, method: str, path: str, query: Optional[dict] = None, body: Any = None, raw_body: str = "") -> Tuple[int, Any, str]:
    method = (method or "GET").upper()
    query = query or {}
    body = body if isinstance(body, dict) else {}

    try:
        endpoint = path.rstrip("/")
        post_only = {
            "/api/connect", "/api/disconnect", "/api/teleport", "/api/clear", "/api/joystick",
            "/api/route", "/api/route/start", "/api/route/pause", "/api/route/resume",
            "/api/route/stop", "/api/route/import", "/api/gpx/import",
        }
        read_only = {
            "/api/health", "/api/status", "/api/engines", "/api/search", "/api/reverse",
            "/api/route/export", "/api/gpx/export",
        }
        allowed = ({"POST"} if endpoint in post_only else
                   {"GET"} if endpoint in read_only else
                   {"GET", "DELETE"} if endpoint == "/api/history" else
                   {"GET", "POST"} if endpoint in ("/api/settings", "/api/favorites") else None)
        if allowed is not None and method not in allowed:
            raise ApiError("不支持的方法: %s" % method, 405)
        # --- 健康与状态 -------------------------------------------------
        if path in ("/api/health", "/api/health/"):
            return 200, {"ok": True}, JSON

        if path in ("/api/status", "/api/status/"):
            return 200, session.snapshot(), JSON

        if path in ("/api/engines", "/api/engines/"):
            probe = str(query.get("probe", ["0"])[0]).lower() in ("1", "true", "yes")
            return 200, {"engines": session.list_engines(probe=probe)}, JSON

        # --- 设备连接 ---------------------------------------------------
        if path in ("/api/connect", "/api/connect/"):
            payload = session.connect(engine_name=body.get("engine") or "auto", udid=body.get("udid"))
            return 200, payload, JSON

        if path in ("/api/disconnect", "/api/disconnect/"):
            return 200, session.disconnect(clear=bool(body.get("clear", True))), JSON

        # --- 定位动作 ---------------------------------------------------
        if path in ("/api/teleport", "/api/teleport/"):
            lat = _as_float(body, "lat")
            lon = _as_float(body, "lon")
            return 200, session.teleport(lat, lon, label=body.get("label"), source=body.get("source") or "manual"), JSON

        if path in ("/api/clear", "/api/clear/"):
            return 200, session.clear_location(), JSON

        if path in ("/api/joystick", "/api/joystick/"):
            bearing = _as_float(body, "bearing")
            meters = _as_float(body, "meters", required=False, default=1.0)
            return 200, session.joystick(bearing, meters), JSON

        # --- 路线 -------------------------------------------------------
        if path in ("/api/route", "/api/route/"):
            points = _points_from(body)
            payload = session.set_route(
                points,
                profile=body.get("profile") or "driving",
                speed=_as_float(body, "speed", required=False),
                loop=body.get("loop") or "none",
                use_router=body.get("use_router"),
                name=body.get("name"),
            )
            return 200, payload, JSON

        if path in ("/api/route/start", "/api/route/start/"):
            return 200, session.start_route(speed=_as_float(body, "speed", required=False), loop=body.get("loop")), JSON

        if path in ("/api/route/pause", "/api/route/pause/"):
            return 200, session.pause_route(), JSON

        if path in ("/api/route/resume", "/api/route/resume/"):
            return 200, session.resume_route(), JSON

        if path in ("/api/route/stop", "/api/route/stop/"):
            return 200, session.stop_route(), JSON

        if path in ("/api/route/export", "/api/route/export/", "/api/gpx/export"):
            return 200, session.export_gpx(), GPX

        if path in ("/api/route/import", "/api/route/import/", "/api/gpx/import"):
            text = body.get("gpx") if isinstance(body.get("gpx"), str) else raw_body
            if not text:
                raise ApiError("缺少 GPX 内容")
            return 200, session.import_gpx(text, speed=_as_float(body, "speed", required=False), loop=body.get("loop") or "none"), JSON

        # --- 地理编码 ---------------------------------------------------
        if path in ("/api/search", "/api/search/"):
            q = (query.get("q", [""])[0] or "").strip()
            if not q:
                raise ApiError("缺少查询参数 q")
            limit = int(query.get("limit", ["6"])[0])
            return 200, {"results": session.places.search(q, limit=limit)}, JSON

        if path in ("/api/reverse", "/api/reverse/"):
            lat = float(query.get("lat", ["0"])[0])
            lon = float(query.get("lon", ["0"])[0])
            return 200, {"result": session.places.reverse(lat, lon)}, JSON

        # --- 历史与收藏 -------------------------------------------------
        if path in ("/api/history", "/api/history/"):
            if method == "DELETE":
                return 200, {"removed": session.store.clear_history()}, JSON
            limit = query.get("limit", [None])[0]
            return 200, {"history": session.store.list_history(limit=int(limit) if limit else None)}, JSON

        if path in ("/api/favorites", "/api/favorites/"):
            if method == "POST":
                lat = _as_float(body, "lat")
                lon = _as_float(body, "lon")
                entry = session.store.add_favorite(
                    body.get("name") or "未命名", lat, lon, kind=body.get("kind") or "point", points=body.get("points")
                )
                return 200, {"favorite": entry}, JSON
            return 200, {"favorites": session.store.list_favorites()}, JSON

        if path.startswith("/api/favorites/"):
            favorite_id = path.rstrip("/").rsplit("/", 1)[-1]
            if method == "DELETE":
                return 200, {"removed": session.store.remove_favorite(favorite_id)}, JSON
            raise ApiError("不支持的方法: %s" % method, 405)

        # --- 设置 -------------------------------------------------------
        if path in ("/api/settings", "/api/settings/"):
            if method == "POST":
                session.settings.update({k: v for k, v in body.items() if k in session.settings or k.startswith("recent_")})
                session.places.offline = bool(session.settings.get("offline"))
                session.router.offline = bool(session.settings.get("offline"))
                session.store.set_setting("settings", {k: session.settings[k] for k in sorted(session.settings)})
                settings_file = session.store.path.parent / "settings.json" if session.store.path else None
                config.save_settings(session.settings, path=settings_file)
                session.emit("settings", {})
                return 200, {"settings": session.snapshot()["settings"]}, JSON
            return 200, {"settings": session.snapshot()["settings"]}, JSON

        raise ApiError("未知接口: %s" % path, 404)

    except ApiError:
        raise
    except EngineUnavailable as exc:
        raise ApiError("引擎不可用: %s" % exc, 503)
    except EngineError as exc:
        raise ApiError("设备操作失败: %s" % exc, 409)
    except (PlaybackError, RouteError, GpxError, PlaceError, geo.GeoError, ValueError) as exc:
        raise ApiError(str(exc), 400)


def parse_body(raw: bytes, content_type: str = "") -> Any:
    text = (raw or b"").decode("utf-8", "replace")
    if not text.strip():
        return {}
    if "json" in (content_type or "") or text.lstrip()[:1] in ("{", "["):
        try:
            return json.loads(text)
        except ValueError:
            return {}
    if "form-urlencoded" in (content_type or ""):
        return {k: v[0] for k, v in parse_qs(text).items()}
    return {"raw": text}
