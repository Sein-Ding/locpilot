"""全局配置与运行期偏好。

配置来源优先级（后者覆盖前者）：
1. 代码内 DEFAULTS
2. 状态目录中的 settings.json（用户偏好，可持久化）
3. 环境变量 LOCPILOT_*

状态目录默认 ~/.locpilot，可用 LOCPILOT_HOME 覆盖（测试与隔离运行必需）。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

APP_NAME = "locpilot"
APP_TITLE = "LocPilot"
VERSION = "1.0.1"

# --- 网络服务默认值 -------------------------------------------------------
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8799

# --- 地图 / 地理服务 ------------------------------------------------------
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_ATTRIBUTION = "&copy; OpenStreetMap contributors"

# 底图源。crs 字段决定是否需要坐标纠偏：
#   国内底图（高德）使用 GCJ-02，设备真值是 WGS-84，直接叠加会偏 300~800 米。
TILE_PROVIDERS = {
    "amap": {
        "label": "高德地图",
        "url": "https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
        "subdomains": "1234",
        "attribution": "&copy; 高德地图",
        "crs": "gcj02",
        "max_zoom": 18,
    },
    "amap-satellite": {
        "label": "高德卫星",
        "url": "https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
        "subdomains": "1234",
        "attribution": "&copy; 高德地图",
        "crs": "gcj02",
        "max_zoom": 18,
    },
    "osm": {
        "label": "OpenStreetMap",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "subdomains": "",
        "attribution": "&copy; OpenStreetMap contributors",
        "crs": "wgs84",
        "max_zoom": 19,
    },
}
DEFAULT_TILE_PROVIDER = "amap"
NOMINATIM_URL = "https://nominatim.openstreetmap.org"
OSRM_URL = "https://router.project-osrm.org"
HTTP_TIMEOUT = 8.0
HTTP_USER_AGENT = "LocPilot/1.0 (iOS virtual location tool; local desktop use)"

# --- 定位回放 -------------------------------------------------------------
# 引擎下发间隔（秒）。USB/隧道链路不宜过密，1s 与 iAnyGo 的体感一致。
TICK_INTERVAL = 1.0
# 位移速度（米/秒）
SPEED_PRESETS = {
    "walk": 1.4,
    "run": 3.0,
    "cycle": 6.9,
    "drive": 13.9,
    "sport": 30.0,
}
MIN_SPEED = 0.1
MAX_SPEED = 200.0
# 直线模式下每段折线的加密步长（米），保证拐弯处平滑
DENSIFY_STEP_M = 20.0
HISTORY_LIMIT = 100

DEFAULTS: Dict[str, Any] = {
    "engine": "auto",
    "tick_interval": TICK_INTERVAL,
    "default_speed": SPEED_PRESETS["walk"],
    "tile_url": TILE_URL,
    "tile_provider": DEFAULT_TILE_PROVIDER,
    "offline": False,
    "use_router": True,
    "history_limit": HISTORY_LIMIT,
}

_ENV_KEYS = {
    "LOCPILOT_HOME": "home",
    "LOCPILOT_HOST": "host",
    "LOCPILOT_PORT": "port",
    "LOCPILOT_ENGINE": "engine",
    "LOCPILOT_PMD3": "pmd3_bin",
    "LOCPILOT_GOIOS": "goios_bin",
    "LOCPILOT_OFFLINE": "offline",
    "LOCPILOT_TICK_INTERVAL": "tick_interval",
    "LOCPILOT_TILE_URL": "tile_url",
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def data_dir() -> Path:
    """状态目录；LOCPILOT_HOME 可覆盖。"""
    raw = os.environ.get("LOCPILOT_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".locpilot"
    return path


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> Dict[str, Any]:
    """读取持久化偏好；文件损坏时回退默认值而不是崩溃。"""
    data: Dict[str, Any] = {}
    path = settings_path()
    # 兼容旧 API 存在 state.json 中的偏好，新 settings.json 优先。
    legacy = data_dir() / "state.json"
    if legacy.exists():
        try:
            state = json.loads(legacy.read_text(encoding="utf-8"))
            stored = state.get("settings", {}).get("settings", {})
            if isinstance(stored, dict):
                data.update(stored)
        except (OSError, ValueError, AttributeError):
            pass
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, ValueError):
            data = {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS or k.startswith("recent_")})
    return merged


def save_settings(settings: Dict[str, Any], path: Optional[Path] = None) -> None:
    path = Path(path) if path is not None else settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def env_overrides() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for env_key, name in _ENV_KEYS.items():
        if env_key not in os.environ:
            continue
        value: Any = os.environ[env_key]
        if name == "offline":
            value = _as_bool(value)
        elif name == "port":
            try:
                value = int(value)
            except ValueError:
                continue
        elif name == "tick_interval":
            value = _as_float(value, TICK_INTERVAL)
        out[name] = value
    return out


def resolve_settings(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合并 默认值 + settings.json + 环境变量 + 显式覆盖。"""
    settings = load_settings()
    settings.update(env_overrides())
    if overrides:
        settings.update({k: v for k, v in overrides.items() if v is not None})
    return settings


def bundled_paths() -> Dict[str, str]:
    """项目内可直接使用的可执行文件位置（引擎 venv）。"""
    root = Path(__file__).resolve().parent.parent
    venv = root / ".venv"
    return {
        "root": str(root),
        "venv_python": str(venv / "bin" / "python"),
        "venv_pmd3": str(venv / "bin" / "pymobiledevice3"),
    }
