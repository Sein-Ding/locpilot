"""JSON 状态存储：历史、收藏与用户偏好。

约束：
* 写盘必须原子（mkstemp + os.replace），否则断电/崩溃会留下半个 JSON。
* 读取必须容错：文件损坏时回退为空状态并继续服务，不抛异常打断 UI。
* 所有写操作走同一把可重入锁，HTTP 线程与回放线程并发访问是常态。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class Store:
    def __init__(self, path: Optional[Path] = None, history_limit: int = 100) -> None:
        self.path = Path(path) if path else None
        self.history_limit = int(history_limit)
        self._lock = threading.RLock()
        self._state: Dict[str, Any] = {"history": [], "favorites": [], "settings": {}}
        self.load()

    # --- 持久化 --------------------------------------------------------
    def load(self) -> Dict[str, Any]:
        with self._lock:
            if self.path and self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        self._state["history"] = list(data.get("history") or [])
                        self._state["favorites"] = list(data.get("favorites") or [])
                        self._state["settings"] = dict(data.get("settings") or {})
                except (OSError, ValueError):
                    self._state = {"history": [], "favorites": [], "settings": {}}
            return self._state

    def save(self) -> None:
        with self._lock:
            if not self.path:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, self.path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # --- 历史 ----------------------------------------------------------
    def add_history(self, lat: float, lon: float, label: Optional[str] = None, source: str = "manual") -> dict:
        entry = {
            "id": uuid.uuid4().hex[:12],
            "lat": float(lat),
            "lon": float(lon),
            "label": label,
            "source": source,
            "ts": time.time(),
        }
        with self._lock:
            history: List[dict] = self._state["history"]
            history.insert(0, entry)
            del history[self.history_limit :]
        self.save()
        return entry

    def list_history(self, limit: Optional[int] = None) -> List[dict]:
        with self._lock:
            items = list(self._state["history"])
        return items[: int(limit)] if limit else items

    def clear_history(self) -> int:
        with self._lock:
            count = len(self._state["history"])
            self._state["history"] = []
        self.save()
        return count

    # --- 收藏 ----------------------------------------------------------
    def add_favorite(self, name: str, lat: float, lon: float, kind: str = "point", points=None) -> dict:
        entry = {
            "id": uuid.uuid4().hex[:12],
            "name": name or "未命名",
            "lat": float(lat),
            "lon": float(lon),
            "kind": kind,
            "points": [list(p) for p in (points or [])],
            "ts": time.time(),
        }
        with self._lock:
            self._state["favorites"].insert(0, entry)
        self.save()
        return entry

    def remove_favorite(self, favorite_id: str) -> bool:
        with self._lock:
            before = len(self._state["favorites"])
            self._state["favorites"] = [f for f in self._state["favorites"] if f.get("id") != favorite_id]
            removed = len(self._state["favorites"]) != before
        if removed:
            self.save()
        return removed

    def list_favorites(self) -> List[dict]:
        with self._lock:
            return list(self._state["favorites"])

    # --- 偏好 ----------------------------------------------------------
    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._state["settings"][key] = value
        self.save()

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._state["settings"].get(key, default)

    def settings(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state["settings"])

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "history": list(self._state["history"]),
                "favorites": list(self._state["favorites"]),
                "settings": dict(self._state["settings"]),
                "path": str(self.path) if self.path else None,
            }
