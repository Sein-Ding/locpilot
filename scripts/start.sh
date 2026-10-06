#!/usr/bin/env bash
# 启动 LocPilot Web 服务（默认 127.0.0.1:8799）。
#   ./scripts/start.sh              # 自动选择引擎
#   ./scripts/start.sh --mock       # 无设备演示模式
#   ./scripts/start.sh --connect    # 启动时尝试连接设备
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPYCACHEPREFIX="$ROOT/.pycache"
export PYTHONDONTWRITEBYTECODE=1

ARGS=("$@")
if [ "${1:-}" = "--mock" ]; then
  ARGS=("--engine" "mock" "--open")
fi
exec python3 -m locpilot serve "${ARGS[@]}"
