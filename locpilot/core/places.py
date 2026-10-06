"""地名检索与逆地理编码客户端（默认 Nominatim）。

工程约束：
* 必须带自定义 User-Agent，并做最小请求间隔限流，否则公共实例会直接封禁来源。
* 结果在内存里做 TTL 缓存：地图拖拽/反复搜索很容易打出重复请求。
* offline=True 时不发任何外部请求，直接返回空结果，保证断网时 UI 不卡死。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple


class PlaceError(RuntimeError):
    """地理编码服务不可用。"""


# 原生地图落针标签的选择优先级：POI 名 → 建筑/设施 → 门牌+道路 → 街区 → 城市 → display_name 首段。
# 顺序即「离用户最近的可识别名称」，与 Apple 地图落针显示街道/建筑名的行为一致。
POI_KEYS = ("amenity", "building", "shop", "tourism", "leisure", "office", "healthcare")
AREA_KEYS = ("neighbourhood", "quarter", "suburb", "city_district", "hamlet", "village", "town", "city", "county")


def pick_label(payload: dict, address: dict) -> Optional[str]:
    """从 Nominatim 结果里挑一个像原生地图那样的短名；挑不到就返回 None（界面宁可不显示，也不显示坐标）。"""
    name = payload.get("name")
    if name:
        return str(name)
    for key in POI_KEYS:
        value = address.get(key)
        if value:
            return str(value)
    road = address.get("road") or address.get("pedestrian") or address.get("footway")
    if road:
        number = address.get("house_number")
        return ("%s %s" % (number, road)).strip() if number else str(road)
    for key in AREA_KEYS:
        value = address.get(key)
        if value:
            return str(value)
    display = payload.get("display_name")
    if display:
        head = str(display).split(",", 1)[0].strip()
        return head or None
    return None


class PlaceClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 8.0,
        user_agent: str = "LocPilot/1.0",
        offline: bool = False,
        min_interval: float = 1.0,
        cache_ttl: float = 600.0,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        self.offline = offline
        self.min_interval = max(0.0, min_interval)
        self.cache_ttl = cache_ttl
        self._lock = threading.RLock()
        self._last_request = 0.0
        self._cache: Dict[str, Tuple[float, object]] = {}

    # --- 内部 ----------------------------------------------------------
    def _cached(self, key: str):
        with self._lock:
            hit = self._cache.get(key)
            if hit and (time.time() - hit[0]) < self.cache_ttl:
                return hit[1]
            if hit:
                self._cache.pop(key, None)
        return None

    def _remember(self, key: str, value) -> None:
        with self._lock:
            self._cache[key] = (time.time(), value)

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            wait = self.min_interval - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.time()

    def _get(self, path: str, params: dict) -> object:
        cache_key = path + "?" + urllib.parse.urlencode(sorted(params.items()))
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        if self.offline:
            raise PlaceError("离线模式：已跳过地理编码请求")
        self._throttle()
        url = "%s%s?%s" % (self.base_url, path, urllib.parse.urlencode(params))
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PlaceError("地理编码请求失败: %s" % (exc,))
        self._remember(cache_key, payload)
        return payload

    # --- 对外 ----------------------------------------------------------
    def search(self, query: str, limit: int = 6) -> List[dict]:
        query = (query or "").strip()
        if not query:
            return []
        payload = self._get("/search", {"q": query, "format": "jsonv2", "limit": max(1, min(int(limit), 20))})
        if not isinstance(payload, list):
            return []
        out: List[dict] = []
        for item in payload:
            try:
                out.append(
                    {
                        "name": item.get("name") or (item.get("display_name") or "").split(",")[0],
                        "display_name": item.get("display_name"),
                        "lat": float(item["lat"]),
                        "lon": float(item["lon"]),
                        "type": item.get("type"),
                        "category": item.get("category"),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def reverse(self, lat: float, lon: float) -> Optional[dict]:
        payload = self._get(
            "/reverse",
            {"lat": "%.7f" % float(lat), "lon": "%.7f" % float(lon), "format": "jsonv2"},
        )
        if not isinstance(payload, dict):
            return None
        address = payload.get("address") if isinstance(payload.get("address"), dict) else {}
        address = payload.get("address") if isinstance(payload.get("address"), dict) else {}
        return {
            "display_name": payload.get("display_name"),
            "name": payload.get("name"),
            "lat": float(payload.get("lat", lat)),
            "lon": float(payload.get("lon", lon)),
            "type": payload.get("type"),
            "label": pick_label(payload, address),
            "label": pick_label(payload, address),
        }
