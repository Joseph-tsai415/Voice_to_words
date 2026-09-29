#!/usr/bin/env bash
# 在 bash / WSL / macOS / Linux 啟動
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"          # Windows 的 venv 版面
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  echo "  建立虛擬環境 ..."
  python3 -m venv .venv || python -m venv .venv
  PY=$([ -x ".venv/bin/python" ] && echo ".venv/bin/python" || echo ".venv/Scripts/python.exe")
  "$PY" -m pip install --upgrade pip --quiet
  "$PY" -m pip install -r requirements.txt --quiet
fi

export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
exec "$PY" -m app "$@"
