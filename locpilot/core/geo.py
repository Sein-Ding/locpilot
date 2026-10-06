"""球面地理计算（纯函数，无外部依赖）。

约定：
* 所有距离单位为米，角度单位为度（正北为 0，顺时针）。
* 点统一表示为 (lat, lon) 元组，函数不做隐式投影转换。
"""

from __future__ import annotations

import math
import re
from typing import Iterable, List, Optional, Sequence, Tuple

EARTH_RADIUS_M = 6371008.8
Point = Tuple[float, float]

_COORD_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*[, ]\s*([+-]?\d+(?:\.\d+)?)\s*$")


class GeoError(ValueError):
    """坐标非法或输入无法解析。"""


def validate_lat(value) -> float:
    try:
        lat = float(value)
    except (TypeError, ValueError):
        raise GeoError("纬度必须是数字: %r" % (value,))
    if not -90.0 <= lat <= 90.0:
        raise GeoError("纬度超出 [-90, 90]: %r" % (value,))
    return lat


def validate_lon(value) -> float:
    try:
        lon = float(value)
    except (TypeError, ValueError):
        raise GeoError("经度必须是数字: %r" % (value,))
    if not -180.0 <= lon <= 180.0:
        raise GeoError("经度超出 [-180, 180]: %r" % (value,))
    return lon


def validate_point(point) -> Point:
    if not isinstance(point, (tuple, list)) or len(point) != 2:
        raise GeoError("坐标必须是 (lat, lon): %r" % (point,))
    return validate_lat(point[0]), validate_lon(point[1])


def haversine(a, b) -> float:
    """两点大圆距离（米）。"""
    lat1, lon1 = validate_point(a)
    lat2, lon2 = validate_point(b)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def normalize_point(lat: float, lon: float) -> Point:
    """经度归一到 [-180, 180)，纬度裁剪到 [-90, 90]。"""
    lon = ((lon + 180.0) % 360.0) - 180.0
    lat = max(-90.0, min(90.0, lat))
    return round(lat, 7), round(lon, 7)


def bearing(a, b) -> float:
    """a 指向 b 的初始方位角（0-360 度，正北为 0）。"""
    lat1, lon1 = validate_point(a)
    lat2, lon2 = validate_point(b)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination(point, bearing_deg: float, distance_m: float) -> Point:
    """从 point 沿 bearing_deg 前进 distance_m 后的坐标。"""
    lat, lon = validate_point(point)
    phi1 = math.radians(lat)
    lambda1 = math.radians(lon)
    theta = math.radians(bearing_deg)
    delta = distance_m / EARTH_RADIUS_M
    sin_phi2 = math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    phi2 = math.asin(max(-1.0, min(1.0, sin_phi2)))
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * sin_phi2,
    )
    return normalize_point(math.degrees(phi2), math.degrees(lambda2))


def interpolate(a, b, fraction: float) -> Point:
    """a 到 b 的线性插值（短段足够精确，长段由 densify 控制误差）。"""
    lat1, lon1 = validate_point(a)
    lat2, lon2 = validate_point(b)
    t = max(0.0, min(1.0, float(fraction)))
    return normalize_point(lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t)


def path_length(points: Sequence[Sequence[float]]) -> float:
    return sum(haversine(points[i], points[i + 1]) for i in range(len(points) - 1))


def cumulative_distances(points: Sequence[Sequence[float]]) -> List[float]:
    """每个顶点的累计里程，长度与 points 相同。"""
    if not points:
        return []
    out = [0.0]
    for i in range(1, len(points)):
        out.append(out[-1] + haversine(points[i - 1], points[i]))
    return out


def densify(points: Sequence[Sequence[float]], step_m: float = 20.0) -> List[Point]:
    """按 step_m 在每段内插点，保证拐弯与长直线都有足够控制点。"""
    pts = [validate_point(p) for p in points]
    if len(pts) < 2 or step_m <= 0:
        return pts
    out: List[Point] = [pts[0]]
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg = haversine(a, b)
        if seg <= step_m:
            out.append(b)
            continue
        steps = int(math.ceil(seg / step_m))
        for k in range(1, steps + 1):
            out.append(interpolate(a, b, k / float(steps)))
    return out


def point_at_distance(points: Sequence[Sequence[float]], distance_m: float, cumulative=None) -> Optional[dict]:
    """按里程取点：返回 lat/lon/segment/t/bearing/distance。"""
    if not points:
        return None
    cum = list(cumulative) if cumulative is not None else cumulative_distances(points)
    if len(points) == 1 or cum[-1] <= 0:
        lat, lon = validate_point(points[0])
        return {"lat": lat, "lon": lon, "segment": 0, "t": 0.0, "bearing": 0.0, "distance": 0.0}
    d = max(0.0, min(float(distance_m), cum[-1]))
    lo, hi = 0, len(cum) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if cum[mid] <= d:
            lo = mid
        else:
            hi = mid
    seg_len = cum[lo + 1] - cum[lo]
    t = 0.0 if seg_len <= 0 else (d - cum[lo]) / seg_len
    pos = interpolate(points[lo], points[lo + 1], t)
    return {
        "lat": pos[0],
        "lon": pos[1],
        "segment": lo,
        "t": round(t, 6),
        "bearing": round(bearing(points[lo], points[lo + 1]), 2),
        "distance": round(d, 3),
    }


def bbox(points: Sequence[Sequence[float]], pad_deg: float = 0.0) -> Optional[dict]:
    if not points:
        return None
    lats = [validate_point(p)[0] for p in points]
    lons = [validate_point(p)[1] for p in points]
    return {
        "south": min(lats) - pad_deg,
        "north": max(lats) + pad_deg,
        "west": min(lons) - pad_deg,
        "east": max(lons) + pad_deg,
    }


def meters_per_degree(lat: float) -> Tuple[float, float]:
    """该纬度上 1 度纬度 / 1 度经度对应的米数。"""
    m_per_deg_lat = math.pi * EARTH_RADIUS_M / 180.0
    m_per_deg_lon = m_per_deg_lat * max(0.0, math.cos(math.radians(lat)))
    return m_per_deg_lat, m_per_deg_lon


def move_by(point, bearing_deg: float, distance_m: float) -> Point:
    return destination(point, bearing_deg, distance_m)


def parse_coord(text: str) -> Point:
    """解析 lat,lon 或 lat lon 形式的坐标串。"""
    match = _COORD_RE.match(text or "")
    if not match:
        raise GeoError("无法解析坐标，请使用 lat,lon 形式: %r" % (text,))
    return validate_lat(match.group(1)), validate_lon(match.group(2))


def format_coord(lat: float, lon: float, digits: int = 6) -> str:
    return "%.*f, %.*f" % (digits, lat, digits, lon)


def total_length(points: Iterable[Sequence[float]]) -> float:
    return path_length(list(points))
