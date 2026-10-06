"""WGS-84 与 GCJ-02（火星坐标）互转。

为什么必须有这个模块：iPhone 上报的是 WGS-84，而高德/腾讯等国内底图使用 GCJ-02，
两者在国内相差 300~800 米。若直接拿 WGS-84 坐标画在高德底图上，蓝点会明显偏移；
反过来，用户在高德底图上点的位置也不能直接下发给设备。

约定：**设备侧与 API 全部使用 WGS-84**，只在渲染底图时转换，避免污染定位真值。
算法为公开的标准实现（克拉索夫斯基椭球 + 三次多项式扰动），境外坐标原样返回。
"""

from __future__ import annotations

import math
from typing import Tuple

A = 6378245.0  # 克拉索夫斯基椭球长半轴
EE = 0.00669342162296594323  # 第一偏心率平方

Point = Tuple[float, float]


def out_of_china(lat: float, lon: float) -> bool:
    """粗略判断是否在中国境外：境外不做偏移（GCJ-02 只在国内生效）。"""
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lon: float) -> Point:
    """WGS-84 → GCJ-02（用于把设备真实坐标画到国内底图上）。"""
    if out_of_china(lat, lon):
        return float(lat), float(lon)
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = math.radians(lat)
    magic = 1 - EE * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (A / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


def gcj02_to_wgs84(lat: float, lon: float) -> Point:
    """GCJ-02 → WGS-84（用于把用户在国内底图上点的位置下发到设备）。

    解析反函数不存在，用不动点迭代逼近；实测 3~4 次即收敛到 1e-9 度（~0.1 毫米）。
    """
    if out_of_china(lat, lon):
        return float(lat), float(lon)
    wlat, wlon = float(lat), float(lon)
    for _ in range(8):
        glat, glon = wgs84_to_gcj02(wlat, wlon)
        dlat, dlon = glat - lat, glon - lon
        if abs(dlat) < 1e-11 and abs(dlon) < 1e-11:
            break
        wlat -= dlat
        wlon -= dlon
    return wlat, wlon


def offset_meters(lat: float, lon: float) -> float:
    """某点 WGS-84 与 GCJ-02 的偏移距离（米），用于自检与提示。"""
    from .geo import haversine

    return haversine((lat, lon), wgs84_to_gcj02(lat, lon))
