"""GPX 1.1 读写（标准库实现）。

支持 trkpt / rtept / wpt 三类点；导入时优先使用 trk（轨迹），其次 rte（路线），
最后 wpt（航点）。导出固定为 GPX 1.1，带 creator 标记。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import List, Optional, Sequence
from xml.sax.saxutils import escape

from . import geo
from .route import Route

GPX_NS = "http://www.topografix.com/GPX/1/1"
CREATOR = "LocPilot 1.0"


class GpxError(ValueError):
    """GPX 内容无法解析。"""


def _tag(name: str) -> str:
    return "{%s}%s" % (GPX_NS, name)


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _iter_points(root) -> List[dict]:
    points: List[dict] = []
    for trk in root.iter():
        if _strip_ns(trk.tag) != "trkpt":
            continue
        lat, lon = trk.get("lat"), trk.get("lon")
        if lat is None or lon is None:
            continue
        item = {"lat": float(lat), "lon": float(lon), "ele": None, "time": None}
        for child in trk:
            name = _strip_ns(child.tag)
            if name == "ele" and child.text:
                item["ele"] = float(child.text)
            elif name == "time" and child.text:
                item["time"] = child.text.strip()
        points.append(item)
    return points


def _iter_named_points(root, kind: str) -> List[dict]:
    out: List[dict] = []
    for node in root.iter():
        if _strip_ns(node.tag) != kind:
            continue
        lat, lon = node.get("lat"), node.get("lon")
        if lat is None or lon is None:
            continue
        out.append({"lat": float(lat), "lon": float(lon)})
    return out


def parse_gpx(text: str) -> dict:
    """解析 GPX 文本，返回轨迹点与元信息。"""
    if not text or not text.strip():
        raise GpxError("GPX 内容为空")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise GpxError("GPX 解析失败: %s" % (exc,))
    if _strip_ns(root.tag) != "gpx":
        raise GpxError("根节点不是 gpx: %s" % (root.tag,))
    name = None
    for node in root.iter():
        if _strip_ns(node.tag) == "name" and node.text:
            name = node.text.strip()
            break
    points = _iter_points(root)
    source = "trkpt"
    if not points:
        points = _iter_named_points(root, "rtept")
        source = "rtept"
    if not points:
        points = _iter_named_points(root, "wpt")
        source = "wpt"
    if not points:
        raise GpxError("GPX 中没有任何轨迹点")
    return {"name": name, "points": points, "source": source, "count": len(points)}


def route_from_gpx(text: str, name: Optional[str] = None) -> Route:
    """把 GPX 轨迹直接当作几何（不做联网路由），保留原始形状。"""
    parsed = parse_gpx(text)
    coords = [(p["lat"], p["lon"]) for p in parsed["points"]]
    dense = geo.densify(coords, step_m=0.0) if len(coords) < 2 else coords
    return Route(
        waypoints=coords,
        geometry=dense,
        routed=False,
        profile="gpx",
        name=name or parsed.get("name") or "GPX 轨迹",
    )


def to_gpx(points: Sequence[Sequence[float]], name: str = "LocPilot route", description: Optional[str] = None) -> str:
    """把坐标序列导出为 GPX 1.1（轨迹形式）。"""
    pts = [geo.validate_point(p) for p in points]
    if not pts:
        raise GpxError("没有可导出的坐标")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="%s" xmlns="%s">' % (escape(CREATOR), GPX_NS),
        "  <metadata>",
        "    <name>%s</name>" % escape(name),
    ]
    if description:
        lines.append("    <desc>%s</desc>" % escape(description))
    lines += [
        "  </metadata>",
        "  <trk>",
        "    <name>%s</name>" % escape(name),
        "    <trkseg>",
    ]
    for lat, lon in pts:
        lines.append('      <trkpt lat="%.7f" lon="%.7f"></trkpt>' % (lat, lon))
    lines += ["    </trkseg>", "  </trk>", "</gpx>", ""]
    return "\n".join(lines)
