#!/usr/bin/env bash
# 一次性安裝：建立 .venv、安裝套件、下載模型。
#   用法：  ./setup.sh
#
# 這是 setup.ps1 的 bash 版本，給 macOS / Linux / WSL 用。模型清單與位元組數
# 兩邊必須一致 —— 改了一邊記得改另一邊。
set -euo pipefail
cd "$(dirname "$0")"
root="$PWD"

echo ''
echo '  會議逐字稿工具 — 安裝'
echo '  ----------------------------------------'

# --- 1. 找到 Python --------------------------------------------------------
py=''
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      py="$c"; break
    fi
  fi
done
if [ -z "$py" ]; then
  echo '  找不到 Python 3.9 以上。請先安裝：' >&2
  echo '    macOS：  brew install python' >&2
  echo '    Ubuntu： sudo apt install python3 python3-venv' >&2
  exit 1
fi
echo "  Python： $("$py" --version)"

# --- 2. 虛擬環境 -----------------------------------------------------------
if [ -x '.venv/bin/python' ]; then
  venv_py='.venv/bin/python'
elif [ -x '.venv/Scripts/python.exe' ]; then
  venv_py='.venv/Scripts/python.exe'        # 在 Windows 的 Git Bash 下
else
  echo '  建立虛擬環境 .venv …'
  "$py" -m venv .venv
  venv_py=$([ -x '.venv/bin/python' ] && echo '.venv/bin/python' \
                                      || echo '.venv/Scripts/python.exe')
fi

# --- 3. 套件 ---------------------------------------------------------------
echo '  安裝相依套件（第一次約 1–3 分鐘）…'
"$venv_py" -m pip install --upgrade pip --quiet
"$venv_py" -m pip install -r requirements.txt --quiet
echo '  套件安裝完成'

# --- 4. 模型 ---------------------------------------------------------------
# 模型太大不進版控，第一次安裝時從上游抓。每個大小都是對過的實際位元組數，
# 下載完會驗證；大小不符就當作失敗，不留下一個壞掉的檔案。
HF='https://huggingface.co'
models=(
  "models/silero_vad.onnx|https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx|643854"
  "models/segmentation/model.onnx|$HF/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0/resolve/main/model.onnx?download=true|5992913"
  "models/speaker-embedding.onnx|$HF/csukuangfj/speaker-embedding-models/resolve/main/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx?download=true|39593761"
  "models/sense-voice/tokens.txt|$HF/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt?download=true|315894"
  "models/sense-voice/model.onnx|$HF/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.onnx?download=true|937617178"
)

filesize() { wc -c < "$1" | tr -d ' '; }

fetch_model() {
  local path="$1" url="$2" want="$3"
  if [ -f "$path" ] && [ "$(filesize "$path")" = "$want" ]; then
    return 0
  fi
  mkdir -p "$(dirname "$path")"
  echo "    下載 $path（$((want / 1000000)) MB）…"
  if ! curl -fL --retry 3 --retry-delay 2 --progress-bar -o "$path.part" "$url"; then
    echo "      下載失敗" >&2
    rm -f "$path.part"
    return 1
  fi
  local got
  got=$(filesize "$path.part")
  if [ "$got" != "$want" ]; then
    echo "      大小不符：拿到 $got 位元組，應該是 $want" >&2
    rm -f "$path.part"
    return 1
  fi
  mv -f "$path.part" "$path"
}

if ! command -v curl >/dev/null 2>&1; then
  echo '  找不到 curl，無法下載模型。請先安裝 curl 後再執行一次。' >&2
  exit 1
fi

echo '  檢查模型檔案（第一次要下載約 1 GB，之後會跳過）…'
failed=()
for entry in "${models[@]}"; do
  IFS='|' read -r path url want <<< "$entry"
  fetch_model "$path" "$url" "$want" || failed+=("$path")
done

if [ ${#failed[@]} -gt 0 ]; then
  echo '  以下模型沒下載成功：'
  for f in "${failed[@]}"; do echo "    - $f"; done
  echo '  再執行一次 ./setup.sh 會重抓這幾個，已完成的不會重來。'
else
  echo '  模型檔案齊全'
fi

echo ''
echo '  安裝完成。啟動方式：'
echo '    ./run.sh'
echo ''
