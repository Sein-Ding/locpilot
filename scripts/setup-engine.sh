#!/usr/bin/env bash
# LocPilot 引擎安装脚本：创建 .venv 并安装 pymobiledevice3（iOS 17+ 的关键依赖）。
#
# 设计取舍：
# * 优先使用新版 Python（3.10+），但必须在 3.9 上也能装成（macOS 自带 3.9.6）。
# * 兼容 Apple 版 Python 的 __pycache__ 重定向：PYTHONPYCACHEPREFIX 指到项目内，
#   否则在受限沙箱里会出现 "Operation not permitted: ~/Library/Caches/com.apple.python"。
# * 只装引擎，不动系统环境；LocPilot 本体保持零依赖。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PY=""

for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$(command -v "$candidate")"; break; fi
done
if [ -z "$PY" ]; then
  echo "未找到 python3，请先安装 Python 3.9+（brew install python@3.12）" >&2
  exit 1
fi

export PYTHONPYCACHEPREFIX="$ROOT/.pycache"
export PYTHONDONTWRITEBYTECODE=1
export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

echo "使用解释器: $PY ($($PY -V 2>&1))"
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV"
fi

VENV_PY="$VENV/bin/python"
if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
  echo "venv 缺少 pip，使用 get-pip 引导…"
  VERSION="$("$VENV_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  curl -fsSL "https://bootstrap.pypa.io/pip/$VERSION/get-pip.py" -o /tmp/locpilot-get-pip.py
  "$VENV_PY" /tmp/locpilot-get-pip.py
fi

"$VENV_PY" -m pip install --quiet --upgrade pip setuptools wheel || true
echo "安装 pymobiledevice3 …"
"$VENV_PY" -m pip install pymobiledevice3

echo
echo "引擎安装完成:"
"$VENV/bin/pymobiledevice3" version 2>/dev/null | head -1 || true
echo "自检: $VENV_PY -m locpilot engines"
echo
echo "可选（iOS 17+ 若自动隧道失败时使用）:"
echo "  sudo $VENV/bin/pymobiledevice3 remote tunneld"
echo "备选引擎（MIT 许可，闭源发行时改用）:"
echo "  brew install danielpaulus/go-ios/go-ios"
