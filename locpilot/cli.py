"""LocPilot 命令行入口。

CLI 与 Web UI 共用同一个 Session，因此两条路径的行为完全一致（同一引擎、同一回放逻辑）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path
from typing import List, Optional

from . import config
from .core import geo
from .core.session import Session
from .core.store import Store
from .engine import detect
from .engine.base import EngineError
from .server import LocPilotServer, find_free_port


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2))


def _make_session(args) -> Session:
    settings = config.resolve_settings({"engine": getattr(args, "engine", None), "offline": getattr(args, "offline", None)})
    store = Store(path=config.ensure_data_dir() / "state.json", history_limit=int(settings.get("history_limit", 100)))
    return Session(settings=settings, store=store, start_ticker=False)


def _connect(session: Session, args) -> None:
    session.connect(engine_name=getattr(args, "engine", None) or "auto", udid=getattr(args, "udid", None))


# --- 子命令 -------------------------------------------------------------
def _start_parent_watch(session: Session) -> None:
    """父进程（App 外壳）消失后自行退出。

    App 被 kill -9 时 applicationWillTerminate 不会执行，后端会变成孤儿并长期占用端口；
    这里用 ppid 变化做兜底。仅当显式传入 --watch-parent 时启用，
    以免影响用户自己用命令行启动的常驻服务。
    """
    import os
    import threading

    original = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(3)
            if os.getppid() != original:
                session.log("父进程已退出，LocPilot 后端自动关闭", "warn")
                session.shutdown()
                os._exit(0)

    threading.Thread(target=watch, name="locpilot-parent-watch", daemon=True).start()


def cmd_serve(args) -> int:
    settings = config.resolve_settings({"engine": args.engine, "tick_interval": args.tick})
    store = Store(path=config.ensure_data_dir() / "state.json", history_limit=int(settings.get("history_limit", 100)))
    session = Session(settings=settings, store=store, start_ticker=True)
    if getattr(args, "watch_parent", False):
        _start_parent_watch(session)
    port = args.port or (config.DEFAULT_PORT if args.port != 0 else find_free_port())
    server = LocPilotServer(session, host=args.host, port=port, token=args.token)
    print("LocPilot %s 已启动: %s" % (config.VERSION, server.url))
    print("引擎: %s | 状态目录: %s" % (settings.get("engine", "auto"), config.data_dir()))
    if args.connect:
        try:
            info = session.connect(engine_name=args.engine or "auto")
            print("已连接设备: %s" % json.dumps(info.get("device"), ensure_ascii=False))
        except EngineError as exc:
            print("连接设备失败（服务照常启动，可在 UI 中重试）: %s" % exc, file=sys.stderr)
    if args.open:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        session.shutdown()
        server.shutdown()
    return 0


def cmd_engines(args) -> int:
    engines = detect(probe_devices=args.probe)
    if args.json:
        _print({"engines": engines}, True)
        return 0
    for item in engines:
        mark = "✓" if item["available"] else "✗"
        print("%s %-18s %s" % (mark, item["name"], item["reason"]))
        for device in item.get("devices", []):
            print("    - %s %s iOS %s (%s)" % (device.get("udid"), device.get("name"), device.get("ios_version"), device.get("connection")))
    return 0


def cmd_devices(args) -> int:
    session = _make_session(args)
    for item in session.list_engines(probe=True):
        for device in item.get("devices", []):
            print("%s\t%s\t%s\t%s\t%s" % (item["name"], device.get("udid"), device.get("name"), device.get("ios_version"), device.get("connection")))
    return 0


def cmd_set(args) -> int:
    session = _make_session(args)
    _connect(session, args)
    result = session.teleport(args.lat, args.lon, label=args.label)
    _print(result, args.json)
    session.shutdown()
    return 0


def cmd_clear(args) -> int:
    session = _make_session(args)
    _connect(session, args)
    _print(session.clear_location(), args.json)
    session.shutdown()
    return 0


def cmd_route(args) -> int:
    session = _make_session(args)
    _connect(session, args)
    if args.gpx:
        text = Path(args.gpx).read_text(encoding="utf-8")
        summary = session.import_gpx(text, speed=args.speed, loop=args.loop)
    else:
        points = [geo.parse_coord(item) for item in args.point]
        if len(points) < 2:
            print("至少需要两个 --point", file=sys.stderr)
            return 2
        summary = session.set_route(points, speed=args.speed, loop=args.loop, use_router=not args.no_router)
    if args.json:
        _print(summary, True)
    else:
        print("路线: %s 米 / %s 个途经点 / %s" % (int(summary.get("distance", 0)), summary.get("waypoint_count"), "道路" if summary.get("routed") else "直线"))
    if args.dry_run:
        session.shutdown()
        return 0
    session.start_route()
    try:
        while session.playback.playing:
            position = session.tick()
            if position:
                print("%.6f,%.6f  %.0f%%  %.0fm" % (
                    position["lat"], position["lon"],
                    session.playback.status()["progress"] * 100,
                    session.playback.status()["distance"],
                ), flush=True)
            else:
                time.sleep(0.2)
    except KeyboardInterrupt:
        print("已中断")
    finally:
        session.shutdown()
    return 0


def cmd_search(args) -> int:
    session = _make_session(args)
    results = session.places.search(args.query, limit=args.limit)
    _print({"results": results}, args.json)
    session.shutdown()
    return 0


def cmd_export_gpx(args) -> int:
    session = _make_session(args)
    if args.gpx:
        text = Path(args.gpx).read_text(encoding="utf-8")
        session.import_gpx(text)
    else:
        points = [geo.parse_coord(item) for item in args.point]
        session.set_route(points, use_router=not args.no_router)
    payload = session.export_gpx()
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print("已写入 %s" % args.out)
    else:
        print(payload)
    session.shutdown()
    return 0


def cmd_doctor(args) -> int:
    settings = config.resolve_settings()
    report = {
        "version": config.VERSION,
        "python": sys.version.split()[0],
        "data_dir": str(config.data_dir()),
        "paths": config.bundled_paths(),
        "engines": detect(settings=settings, probe_devices=args.probe),
        "tile_url": settings.get("tile_url"),
        "offline": bool(settings.get("offline")),
    }
    _print(report, True)
    return 0


def cmd_version(args) -> int:
    print("LocPilot %s" % config.VERSION)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="locpilot", description="LocPilot — 类 iAnyGo 的 iOS 虚拟定位工具")
    parser.add_argument("--version", action="version", version="LocPilot %s" % config.VERSION)
    sub = parser.add_subparsers(dest="command")

    def add_common(p, engine=True):
        if engine:
            p.add_argument("--engine", default=None, help="引擎: pymobiledevice3 | go-ios | libimobiledevice | mock | auto")
        p.add_argument("--udid", default=None, help="指定设备 UDID")
        p.add_argument("--offline", action="store_true", help="离线模式：不访问地图与地理编码服务")
        p.add_argument("--json", action="store_true", help="以 JSON 输出")

    p_serve = sub.add_parser("serve", help="启动本地 Web 服务")
    p_serve.add_argument("--host", default=config.DEFAULT_HOST)
    p_serve.add_argument("--port", type=int, default=config.DEFAULT_PORT, help="0 表示自动选择空闲端口")
    p_serve.add_argument("--engine", default=None)
    p_serve.add_argument("--tick", type=float, default=None, help="回放节拍（秒）")
    p_serve.add_argument("--token", default=None, help="可选访问令牌")
    p_serve.add_argument("--open", action="store_true", help="启动后打开浏览器")
    p_serve.add_argument("--connect", action="store_true", help="启动时尝试连接设备")
    p_serve.add_argument("--watch-parent", action="store_true", help="父进程退出时自动关闭（供 App 外壳使用）")
    p_serve.set_defaults(func=cmd_serve)

    p_engines = sub.add_parser("engines", help="列出引擎可用性")
    p_engines.add_argument("--probe", action="store_true", help="同时枚举设备")
    p_engines.add_argument("--json", action="store_true")
    p_engines.set_defaults(func=cmd_engines)

    p_devices = sub.add_parser("devices", help="列出已连接设备")
    add_common(p_devices)
    p_devices.set_defaults(func=cmd_devices)

    p_set = sub.add_parser("set", help="单点传送")
    p_set.add_argument("lat", type=float)
    p_set.add_argument("lon", type=float)
    p_set.add_argument("--label", default=None)
    add_common(p_set)
    p_set.set_defaults(func=cmd_set)

    p_clear = sub.add_parser("clear", help="清除虚拟定位")
    add_common(p_clear)
    p_clear.set_defaults(func=cmd_clear)

    p_route = sub.add_parser("route", help="多点路线回放")
    p_route.add_argument("--point", action="append", default=[], help="途经点 lat,lon（可重复）")
    p_route.add_argument("--gpx", default=None, help="GPX 文件路径")
    p_route.add_argument("--speed", type=float, default=None, help="米/秒")
    p_route.add_argument("--loop", default="none", choices=["none", "loop", "pingpong"])
    p_route.add_argument("--no-router", action="store_true", help="不联网路由，直接直线连接")
    p_route.add_argument("--dry-run", action="store_true", help="只装载路线不开始回放")
    add_common(p_route)
    p_route.set_defaults(func=cmd_route)

    p_search = sub.add_parser("search", help="地址搜索")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=6)
    add_common(p_search, engine=False)
    p_search.set_defaults(func=cmd_search)

    p_export = sub.add_parser("export-gpx", help="导出 GPX")
    p_export.add_argument("--point", action="append", default=[])
    p_export.add_argument("--gpx", default=None, help="输入 GPX（转换/规范化）")
    p_export.add_argument("--out", default=None)
    p_export.add_argument("--no-router", action="store_true")
    add_common(p_export, engine=False)
    p_export.set_defaults(func=cmd_export_gpx)

    p_doctor = sub.add_parser("doctor", help="环境自检")
    p_doctor.add_argument("--probe", action="store_true")
    p_doctor.set_defaults(func=cmd_doctor)

    p_version = sub.add_parser("version", help="版本号")
    p_version.set_defaults(func=cmd_version)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
