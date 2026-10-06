"""pymobiledevice3 引擎：优先使用常驻 worker，缺 Python 环境时退回单发 CLI。

两种模式：
* worker（推荐）：一次性建立 lockdown/RSD + DTX 会话，之后每次改坐标只走一行 JSON，
  适合路线回放与摇杆这类高频更新；进程退出或崩溃会带来明确错误而不是静默失效。
* cli（兜底）：每次 set 起一个 pymobiledevice3 子进程并常驻（该命令会阻塞等待 SIGINT），
  下一次 set 前杀掉旧进程。开销较大，但只依赖 CLI 二进制，不需要可导入的 Python 包。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import List, Optional

from .. import config
from .base import CommandResult, DeviceInfo, Engine, EngineError, EngineUnavailable

WORKER_SCRIPT = str(Path(__file__).with_name("pmd3_worker.py"))


class _WorkerProc:
    """JSON Lines 子进程封装：带响应队列、超时与 stderr 尾巴。"""

    def __init__(self, argv: List[str], env=None) -> None:
        self.argv = argv
        self._env = env
        self.proc: Optional[subprocess.Popen] = None
        self._responses: "queue.Queue" = queue.Queue()
        self._stderr = deque(maxlen=40)
        self._counter = 0
        self._lock = threading.RLock()

    def start(self) -> None:
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=self._env,
            )
        except FileNotFoundError as exc:
            raise EngineUnavailable("worker 启动失败: %s" % (exc,))
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._responses.put(json.loads(line))
            except ValueError:
                self._stderr.append("非 JSON 输出: %s" % line[:200])

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self._stderr.append(line.strip())

    @property
    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def stderr_tail(self, limit: int = 400) -> str:
        return " | ".join(list(self._stderr)[-4:])[-limit:]

    def request(self, payload: dict, timeout: float = 30.0) -> dict:
        with self._lock:
            if not self.alive:
                raise EngineError("worker 已退出: %s" % self.stderr_tail())
            self._counter += 1
            payload = dict(payload)
            payload["id"] = self._counter
            try:
                assert self.proc and self.proc.stdin
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise EngineError("worker 写入失败: %s (%s)" % (exc, self.stderr_tail()))
            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise EngineError("worker 响应超时(%.0fs): %s" % (timeout, payload.get("cmd")))
                try:
                    message = self._responses.get(timeout=remaining)
                except queue.Empty:
                    raise EngineError("worker 响应超时(%.0fs): %s" % (timeout, payload.get("cmd")))
                if message.get("id") != payload["id"]:
                    continue
                if not message.get("ok"):
                    raise EngineError(message.get("error") or "worker 返回失败")
                return message.get("detail") or {}

    def stop(self) -> None:
        try:
            if self.alive:
                try:
                    self.request({"cmd": "close"}, timeout=5.0)
                except Exception:
                    pass
                assert self.proc
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        finally:
            self.proc = None


class PyMobileDevice3Engine(Engine):
    name = "pymobiledevice3"
    label = "pymobiledevice3（推荐，支持 iOS 17+）"
    capabilities = {
        "persistent_session": True,
        "supports_ios17": True,
        "needs_tunnel": "auto",
        "needs_sudo": False,
        "supports_gpx_play": True,
        "real_device": True,
    }

    def __init__(self, runner=None, settings=None) -> None:
        super().__init__(runner=runner, settings=settings)
        self._worker: Optional[_WorkerProc] = None
        self._cli_proc: Optional[subprocess.Popen] = None
        self.mode = "idle"
        self.tunnel_hint = ""
        self._home_override: Optional[str] = None
        self.home_fallback_used = False

    # --- 受限环境：~/.pymobiledevice3 不可写时的 HOME 兜底 -----------------
    def _child_env(self) -> dict:
        """pymobiledevice3 会在 Path.home()/.pymobiledevice3 下放缓存与配对记录。

        沙箱 / CI / 只读家目录里这一步会直接 PermissionError，整个引擎不可用；
        因此支持显式 LOCPILOT_PMD3_HOME 覆盖，并在探测到权限错误后自动退到状态目录。
        """
        env = dict(os.environ)
        override = self.settings.get("pmd3_home") or os.environ.get("LOCPILOT_PMD3_HOME") or self._home_override
        if override:
            target = Path(override).expanduser()
            try:
                target.mkdir(parents=True, exist_ok=True)
                env["HOME"] = str(target)
            except OSError:
                pass
        return env

    @staticmethod
    def _looks_like_home_permission_error(text: str) -> bool:
        lowered = (text or "").lower()
        return "pymobiledevice3" in lowered and ("operation not permitted" in lowered or "permission denied" in lowered)

    # --- 环境探测 ------------------------------------------------------
    def binary(self) -> Optional[str]:
        explicit = self.settings.get("pmd3_bin") or os.environ.get("LOCPILOT_PMD3")
        if explicit and Path(explicit).exists():
            return explicit
        bundled = config.bundled_paths().get("venv_pmd3")
        if bundled and Path(bundled).exists():
            return bundled
        return shutil.which("pymobiledevice3")

    def worker_python(self) -> Optional[str]:
        explicit = self.settings.get("pmd3_python") or os.environ.get("LOCPILOT_PMD3_PYTHON")
        if explicit and Path(explicit).exists():
            return explicit
        bundled = config.bundled_paths().get("venv_python")
        if bundled and Path(bundled).exists():
            return bundled
        binary = self.binary()
        if binary:
            for name in ("python3", "python"):
                candidate = Path(binary).parent / name
                if candidate.exists():
                    return str(candidate)
        return shutil.which("python3") or sys.executable

    def availability(self):
        binary = self.binary()
        if not binary:
            return False, "未找到 pymobiledevice3；请运行 scripts/setup-engine.sh 或 pip install pymobiledevice3"
        python = self.worker_python()
        return True, "CLI=%s；worker python=%s" % (binary, python or "缺失（将使用 CLI 兜底）")

    # --- 设备枚举 ------------------------------------------------------
    def _cli(self, args: List[str], timeout: float = 30.0) -> CommandResult:
        binary = self.binary()
        if not binary:
            raise EngineUnavailable("未找到 pymobiledevice3 可执行文件")
        attempts = [[binary, "--no-color"] + args, [binary] + args]
        last: Optional[CommandResult] = None
        for argv in attempts:
            result = self.runner.run(argv, timeout=timeout, env=self._child_env())
            last = result
            if result.ok:
                return result
        # 家目录不可写（沙箱/CI）时自动换到状态目录下的 HOME 再试一次
        if last is not None and not self.home_fallback_used and self._looks_like_home_permission_error(last.stderr):
            self._home_override = str(Path(config.data_dir()) / "pmd3-home")
            self.home_fallback_used = True
            retry = self.runner.run([binary, "--no-color"] + args, timeout=timeout, env=self._child_env())
            if retry.ok:
                return retry
            last = retry
        assert last is not None
        raise EngineError("pymobiledevice3 %s 失败: %s" % (" ".join(args), last.tail()))

    def list_devices(self) -> List[DeviceInfo]:
        result = self._cli(["usbmux", "list"], timeout=40.0)
        payload = None
        for chunk in (result.stdout or "").strip().split("\n"):
            chunk = chunk.strip()
            if chunk.startswith("[") or chunk.startswith("{"):
                try:
                    payload = json.loads(chunk)
                    break
                except ValueError:
                    continue
        if payload is None:
            text = (result.stdout or "").strip()
            if text.startswith("["):
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
        if payload is None:
            raise EngineError("无法解析 usbmux list 输出: %s" % (result.tail(),))
        if isinstance(payload, dict):
            payload = [payload]
        devices: List[DeviceInfo] = []
        for item in payload or []:
            if isinstance(item, str):
                devices.append(DeviceInfo(udid=item, engine=self.name))
                continue
            udid = item.get("UniqueDeviceID") or item.get("Identifier") or item.get("udid") or item.get("SerialNumber")
            if not udid:
                continue
            devices.append(
                DeviceInfo(
                    udid=udid,
                    name=item.get("DeviceName"),
                    product_type=item.get("ProductType"),
                    ios_version=item.get("ProductVersion"),
                    connection=str(item.get("ConnectionType") or "usb").lower(),
                    engine=self.name,
                    extra={"build": item.get("BuildVersion")},
                )
            )
        return devices

    # --- 会话 ----------------------------------------------------------
    def open(self, udid: Optional[str] = None) -> DeviceInfo:
        devices = self.list_devices()
        if not devices:
            raise EngineError("未发现 iOS 设备：请用 USB 连接并在手机上点信任")
        target = None
        for dev in devices:
            if udid is None or dev.udid == udid:
                target = dev
                break
        if target is None:
            raise EngineError("未找到指定设备: %s" % (udid,))
        self.device = target
        python = self.worker_python()
        use_worker = bool(self.settings.get("worker", True)) and bool(python)
        if use_worker:
            argv = [python, WORKER_SCRIPT]
            self._worker = _WorkerProc(argv, env=self._child_env())
            self._worker.start()
            try:
                detail = self._worker.request(
                    {
                        "cmd": "open",
                        "udid": target.udid,
                        "mount": bool(self.settings.get("auto_mount", True)),
                        "rsd": self.settings.get("rsd"),
                        "tunnel": self.settings.get("tunnel"),
                        "timeout": float(self.settings.get("open_timeout", 180.0)),
                    },
                    timeout=float(self.settings.get("open_timeout", 180.0)) + 10.0,
                )
            except EngineError as exc:
                self._worker.stop()
                self._worker = None
                raise self._explain(exc, target)
            self.mode = "worker:" + str(detail.get("mode") or "?")
            self.tunnel_hint = str(detail.get("mount") or "")
        else:
            self.mode = "cli"
        self.opened = True
        self.error = None
        return target

    @staticmethod
    def _explain(exc: EngineError, device: DeviceInfo) -> EngineError:
        """把底层异常翻译成用户能照着做的提示。"""
        text = str(exc)
        if "dtservicehub" in text or "InvalidServiceError" in text:
            return EngineError(
                "设备（iOS %s）未开启开发者模式，DVT 服务不可用。请在 iPhone 上开启："
                "设置 → 隐私与安全性 → 开发者模式（开启后需重启并输入锁屏密码），"
                "或执行 idevicedevmodectl enable；随后重试连接。" % (device.ios_version or "?",)
            )
        if "tunnel" in text.lower() or "rsd" in text.lower():
            return EngineError(
                "无法建立 iOS 17+ 隧道：%s。可先运行 sudo pymobiledevice3 remote tunneld，"
                "或用 --rsd HOST PORT 指定隧道。" % (text[:200],)
            )
        return exc

    def set_location(self, lat: float, lon: float) -> None:
        self.require_open()
        try:
            if self._worker is not None:
                self._worker.request({"cmd": "set", "lat": float(lat), "lon": float(lon)}, timeout=25.0)
            else:
                self._cli_set(lat, lon)
        except EngineError as exc:
            self.error = str(exc)
            raise
        self.last_position = {"lat": float(lat), "lon": float(lon), "ts": time.time()}
        self.error = None

    def clear_location(self) -> None:
        self.require_open()
        if self._worker is not None:
            self._worker.request({"cmd": "clear"}, timeout=25.0)
        else:
            self._kill_cli_proc()
            self._cli(self._simulate_args("clear"), timeout=30.0)
        self.last_position = None

    def _simulate_args(self, action: str) -> List[str]:
        device = self.require_open()
        args: List[str] = ["developer"]
        if device is not None and device.needs_tunnel:
            args.append("dvt")
        args += ["simulate-location", action, "--udid", device.udid]
        return args

    def _kill_cli_proc(self) -> None:
        if self._cli_proc is not None and self._cli_proc.poll() is None:
            self._cli_proc.terminate()
            try:
                self._cli_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._cli_proc.kill()
        self._cli_proc = None

    def _cli_set(self, lat: float, lon: float) -> None:
        """CLI 兜底：set 会阻塞等待 SIGINT，因此常驻并在下次 set 前杀掉旧进程。"""
        binary = self.binary()
        if not binary:
            raise EngineUnavailable("未找到 pymobiledevice3 可执行文件")
        self._kill_cli_proc()
        argv = [binary, "--no-color"] + self._simulate_args("set") + ["--", "%.6f" % float(lat), "%.6f" % float(lon)]
        proc = self.runner.popen(argv, env=self._child_env())
        self._cli_proc = proc
        # 允许 CLI 完成初始化并暴露即时错误；iOS 17+ 成功后继续持有进程。
        try:
            code = proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            return
        self._cli_proc = None
        if code != 0:
            try:
                out, err = proc.communicate(timeout=1.0)
                detail = (err or out or "").strip()[-400:]
            except Exception:
                detail = "退出码 %s" % code
            raise EngineError("pymobiledevice3 设置定位失败: %s" % detail)

    def close(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker = None
        self._kill_cli_proc()
        self.mode = "idle"
        super().close()

    def describe(self) -> dict:
        data = super().describe()
        data.update({
            "mode": self.mode,
            "binary": self.binary(),
            "worker_python": self.worker_python(),
            "worker_stderr": self._worker.stderr_tail() if self._worker else "",
            "home_override": self._home_override or self.settings.get("pmd3_home") or os.environ.get("LOCPILOT_PMD3_HOME"),
            "home_fallback_used": self.home_fallback_used,
        })
        return data
