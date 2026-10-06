"""路线：途经点、几何、里程，以及 OSRM 联网路由与直线兜底。

关键约束：
* 回放里程一律以 geometry 的累计长度为准，router 返回的道路里程只作为参考保存
  （road_distance），否则会出现「进度 100% 但人还没到终点」的错位。
* 联网路由失败必须降级为直线加密，不能抛出到用户面前中断操作。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Sequence

from . import geo


class RouteError(ValueError):
    """路线参数非法。"""


class RouterError(RouteError):
    """路由服务不可用或返回不可解析结果。"""


class Route:
    def __init__(
        self,
        waypoints: Sequence[Sequence[float]],
        geometry: Optional[Sequence[Sequence[float]]] = None,
        routed: bool = False,
        profile: str = "driving",
        name: Optional[str] = None,
        road_distance: Optional[float] = None,
    ) -> None:
        if not waypoints:
            raise RouteError("路线至少需要一个点")
        self.waypoints = [geo.validate_point(p) for p in waypoints]
        self.geometry = [geo.validate_point(p) for p in (geometry if geometry else waypoints)]
        if not self.geometry:
            raise RouteError("路线几何为空")
        self.routed = bool(routed)
        self.profile = profile
        self.name = name
        self.road_distance = float(road_distance) if road_distance is not None else None
        self._cum = geo.cumulative_distances(self.geometry)
        self.distance = self._cum[-1] if self._cum else 0.0

    # --- 查询 ----------------------------------------------------------
    def position_at(self, distance_m: float) -> Optional[dict]:
        return geo.point_at_distance(self.geometry, distance_m, self._cum)

    def duration_at(self, speed_mps: float) -> Optional[float]:
        if speed_mps <= 0:
            return None
        return self.distance / speed_mps

    def bounds(self) -> Optional[dict]:
        return geo.bbox(self.geometry, 0.0)

    def to_dict(self) -> dict:
        return {
            "waypoints": [list(p) for p in self.waypoints],
            "geometry": [list(p) for p in self.geometry],
            "routed": self.routed,
            "profile": self.profile,
            "name": self.name,
            "distance": round(self.distance, 3),
            "road_distance": round(self.road_distance, 3) if self.road_distance is not None else None,
            "bounds": self.bounds(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Route":
        return cls(
            waypoints=[tuple(p) for p in data.get("waypoints", [])],
            geometry=[tuple(p) for p in data.get("geometry", [])] or None,
            routed=bool(data.get("routed")),
            profile=data.get("profile") or "driving",
            name=data.get("name"),
            road_distance=data.get("road_distance"),
        )


def straight_route(
    waypoints: Sequence[Sequence[float]],
    step_m: float = 20.0,
    profile: str = "driving",
    name: Optional[str] = None,
) -> Route:
    """直线连线并按 step_m 加密，作为离线/路由失败时的兜底。"""
    dense = geo.densify(waypoints, step_m=step_m)
    return Route(waypoints=waypoints, geometry=dense, routed=False, profile=profile, name=name)


class OsrmRouter:
    """OSRM HTTP 路由客户端（默认使用公共 demo 服务）。"""

    def __init__(self, base_url: str, timeout: float = 8.0, user_agent: str = "", offline: bool = False) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent or "LocPilot/1.0"
        self.offline = offline

    def route(self, waypoints: Sequence[Sequence[float]], profile: str = "driving") -> dict:
        if self.offline:
            raise RouterError("离线模式已开启，跳过联网路由")
        pts = [geo.validate_point(p) for p in waypoints]
        if len(pts) < 2:
            raise RouterError("至少两个点才能路由")
        coords = ";".join("%.6f,%.6f" % (p[1], p[0]) for p in pts)
        url = "%s/route/v1/%s/%s?overview=full&geometries=geojson" % (self.base_url, profile, coords)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RouterError("路由请求失败: %s" % (exc,))
        routes = payload.get("routes") or []
        if not routes:
            raise RouterError("路由服务未返回路线")
        first = routes[0]
        geometry = [(float(lat), float(lon)) for lon, lat in first.get("geometry", {}).get("coordinates", [])]
        if len(geometry) < 2:
            raise RouterError("路由几何为空")
        return {"geometry": geometry, "distance": float(first.get("distance") or 0.0), "profile": profile}


def build_route(
    waypoints: Sequence[Sequence[float]],
    profile: str = "driving",
    use_router: bool = True,
    router: Optional[OsrmRouter] = None,
    step_m: float = 20.0,
    name: Optional[str] = None,
) -> Route:
    """构建路线：优先联网路由，失败自动降级为直线加密。"""
    pts = [geo.validate_point(p) for p in waypoints]
    if len(pts) < 2:
        return Route(waypoints=pts, geometry=pts, profile=profile, name=name)
    if use_router:
        client = router or OsrmRouter("")
        try:
            result = client.route(pts, profile=profile)
            return Route(
                waypoints=pts,
                geometry=result["geometry"],
                routed=True,
                profile=profile,
                name=name,
                road_distance=result.get("distance"),
            )
        except RouterError:
            pass
    return straight_route(pts, step_m=step_m, profile=profile, name=name)
