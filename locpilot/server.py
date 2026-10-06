"""本地 HTTP 服务：静态站点 + JSON API + SSE 事件流。

安全边界：
* 默认只绑定 127.0.0.1，不对外网暴露；
* 静态文件做了目录穿越防护（resolve 后必须仍在 web 根内）；
* 可选 token：设置后所有 /api 请求必须带 X-LocPilot-Token 或 ?token=。
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from . import api as api_module
from . import config

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 8 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "LocPilot/" + config.VERSION
    protocol_version = "HTTP/1.1"

    # --- 基础工具 ------------------------------------------------------
    def log_message(self, fmt, *args):  # 降低噪音：转发到 session 日志
        session = getattr(self.server, "session", None)
        if session is not None:
            session.log("%s - %s" % (self.address_string(), fmt % args), "debug")

    @property
    def session(self):
        return getattr(self.server, "session")

    def _send(self, status: int, payload, content_type: str = "application/json; charset=utf-8", extra_headers=None) -> None:
        if isinstance(payload, (dict, list)):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json_error(self, status: int, message: str) -> None:
        self._send(status, {"ok": False, "error": message})

    def _authorized(self) -> bool:
        token = getattr(self.server, "token", None)
        if not token:
            return True
        header = self.headers.get("X-LocPilot-Token")
        if header == token:
            return True
        query = parse_qs(urlparse(self.path).query)
        return (query.get("token") or [""])[0] == token

    # --- 路由 ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path.startswith("/api/"):
            if not self._authorized():
                return self._json_error(401, "token 无效")
            if path in ("/api/events", "/api/events/"):
                return self._serve_events()
            query = parse_qs(parsed.query)
            try:
                status, payload, content_type = api_module.handle(self.session, "GET", path, query=query)
            except api_module.ApiError as exc:
                return self._json_error(exc.status, str(exc))
            except Exception as exc:  # 未预期异常也要返回结构化错误
                return self._json_error(500, "内部错误: %s" % (exc,))
            return self._send(status, payload, content_type)
        return self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/api/"):
            return self._json_error(404, "未知路径")
        if not self._authorized():
            return self._json_error(401, "token 无效")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            return self._json_error(413, "请求体过大")
        raw = self.rfile.read(length) if length else b""
        body = api_module.parse_body(raw, self.headers.get("Content-Type") or "")
        query = parse_qs(parsed.query)
        try:
            status, payload, content_type = api_module.handle(
                self.session, "POST", path, query=query, body=body, raw_body=(raw or b"").decode("utf-8", "replace")
            )
        except api_module.ApiError as exc:
            return self._json_error(exc.status, str(exc))
        except Exception as exc:
            return self._json_error(500, "内部错误: %s" % (exc,))
        return self._send(status, payload, content_type)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/api/"):
            return self._json_error(404, "未知路径")
        if not self._authorized():
            return self._json_error(401, "token 无效")
        try:
            status, payload, content_type = api_module.handle(self.session, "DELETE", path, query=parse_qs(parsed.query))
        except api_module.ApiError as exc:
            return self._json_error(exc.status, str(exc))
        except Exception as exc:
            return self._json_error(500, "内部错误: %s" % (exc,))
        return self._send(status, payload, content_type)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET,POST,DELETE,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- SSE -----------------------------------------------------------
    def _serve_events(self):
        session = self.session
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        queue_lock = threading.Lock()
        pending = []

        def on_event(event: str, payload: dict) -> None:
            snapshot = session.snapshot() if event in {
                "connected", "disconnected", "position", "playback", "route", "error", "settings"
            } else None
            with queue_lock:
                pending.append({"event": event, "data": payload})
                if snapshot is not None:
                    pending.append({"event": "snapshot", "data": snapshot})

        unsubscribe = session.subscribe(on_event)
        try:
            self._write_event("snapshot", session.snapshot())
            last_beat = time.time()
            while True:
                with queue_lock:
                    batch, pending[:] = list(pending), []
                for item in batch:
                    self._write_event(item["event"], item["data"])
                if time.time() - last_beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe()

    def _write_event(self, event: str, payload: dict) -> None:
        chunk = "event: %s\ndata: %s\n\n" % (event, json.dumps(payload, ensure_ascii=False))
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    # --- 静态文件 ------------------------------------------------------
    def _serve_static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._json_error(403, "路径越界")
        if target.is_dir():
            target = target / "index.html"
        if not target.exists() or not target.is_file():
            return self._json_error(404, "未找到: %s" % rel)
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass


class LocPilotServer:
    def __init__(self, session, host: str = config.DEFAULT_HOST, port: int = config.DEFAULT_PORT, token: Optional[str] = None) -> None:
        self.session = session
        self.host = host
        self.port = int(port)
        self.token = token
        self.httpd = ThreadingHTTPServer((host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.session = session
        self.httpd.token = token
        self.port = self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return "http://%s:%d/" % (self.host, self.port)

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.3)

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def find_free_port(host: str = config.DEFAULT_HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]
