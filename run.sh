#!/usr/bin/env bash
# 在 bash / WSL / macOS / Linux 啟動
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"          # Windows 的 venv 版面
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  # setup.sh 會建立 .venv、裝套件，並把模型抓下來。少了模型也是啟動不了的，
  # 所以不要在這裡自己裝一半。
  echo "  尚未安裝，先執行 setup.sh ..."
  bash ./setup.sh
  PY=$([ -x ".venv/bin/python" ] && echo ".venv/bin/python" || echo ".venv/Scripts/python.exe")
fi

export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
exec "$PY" -m app "$@"
