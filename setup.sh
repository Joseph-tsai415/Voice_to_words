#!/usr/bin/env bash
# 安裝與環境檢查。可以重複執行，已經好的步驟會自動跳過。
#
# 平常你不用自己跑這個 —— run.sh 會在需要的時候自動叫它。
#
#   ./setup.sh           自動：偵測得到 NVIDIA 顯示卡就開 GPU 加速，沒有就純 CPU
#   ./setup.sh --cpu     強制純 CPU，完全不碰 GPU
#   ./setup.sh --revert  已經裝了 GPU 版，換回 CPU 版
#
# GPU 是預設會嘗試的，但**失敗不會讓安裝失敗**：沒有顯示卡、驅動沒裝好、
# CUDA 起不來，都只是退回 CPU 繼續跑完，功能完全一樣，只有辨識那段慢一點。
#
# 這是 setup.ps1 的 bash 版本。模型清單與位元組數兩邊必須一致 ——
# 改了一邊記得改另一邊。
set -euo pipefail
cd "$(dirname "$0")"
root="$PWD"

SHERPA_VERSION='1.13.6'
CUDA_INDEX='https://k2-fsa.github.io/sherpa/onnx/cuda.html'
CUDA_RUNTIME=(nvidia-cuda-runtime-cu12 nvidia-cudnn-cu12 nvidia-cublas-cu12
              nvidia-cufft-cu12 nvidia-curand-cu12)

force_cpu=0
revert=0
for a in "$@"; do
  case "$a" in
    --cpu) force_cpu=1 ;;
    --revert) revert=1 ;;
    *) echo "  不認識的參數：$a" >&2; exit 2 ;;
  esac
done

echo ''
echo '  會議逐字稿工具 — 安裝'
echo '  ----------------------------------------'

venv_py() {
  if   [ -x '.venv/bin/python' ];           then echo '.venv/bin/python'
  elif [ -x '.venv/Scripts/python.exe' ];   then echo '.venv/Scripts/python.exe'
  else echo ''; fi
}

# --- 0. 只是要換回 CPU 版 ---------------------------------------------------
if [ "$revert" = 1 ]; then
  vp=$(venv_py)
  if [ -z "$vp" ]; then echo '  還沒安裝過，沒有東西要還原。'; exit 0; fi
  echo '  換回 CPU 版 sherpa-onnx …'
  "$vp" -m pip install --force-reinstall "sherpa-onnx==$SHERPA_VERSION" --quiet
  echo '  完成。'
  exit 0
fi

# --- 1. 找到 Python --------------------------------------------------------
py=''
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 &&
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    py="$c"; break
  fi
done
if [ -z "$py" ]; then
  echo '  找不到 Python 3.9 以上。請先安裝：' >&2
  echo '    macOS：  brew install python' >&2
  echo '    Ubuntu： sudo apt install python3 python3-venv' >&2
  exit 1
fi
echo "  [v] Python $("$py" --version | sed 's/Python //')"

# --- 2. 虛擬環境 -----------------------------------------------------------
if [ -z "$(venv_py)" ]; then
  echo '  建立虛擬環境 .venv …'
  "$py" -m venv .venv
fi
vp=$(venv_py)
if [ -z "$vp" ]; then echo '  建立 .venv 失敗。' >&2; exit 1; fi
echo '  [v] 虛擬環境'

# --- 3. 套件 ---------------------------------------------------------------
echo '  安裝相依套件（第一次約 1–3 分鐘）…'
"$vp" -m pip install --upgrade pip --quiet
"$vp" -m pip install -r requirements.txt --quiet
echo '  [v] 相依套件'

# --- 4. GPU 加速（預設就試，失敗只是退回 CPU）-------------------------------
gpu_on=0
probe_cuda() {
  # 光看套件名稱會騙人 —— CUDA 版的 wheel 沒有 CUDA 執行環境也裝得起來，
  # onnxruntime 只會印一行 "Fallback to cpu!" 就繼續跑。所以真的載一個模型試。
  # 多行 Python 用 -c 傳會被引號拆壞，寫成檔案再執行。
  local probe; probe="$(mktemp -t scribe-gpu-probe.XXXXXX.py)"
  cat > "$probe" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
from app import gpu
from app.config import PROVIDER
gpu.refresh()
st = gpu.status(PROVIDER)
print("    " + st["reason"])
if st["install_hint"]:
    print("    缺少：" + st["install_hint"])
sys.exit(0 if st["provider"] == "cuda" else 1)
PYEOF
  PYTHONUTF8=1 PYTHONIOENCODING=utf-8 "$vp" "$probe" "$root" || return 1
}

# 獨顯可以被關掉，所以「現在能不能用」和「這台機器有沒有顯示卡」是兩回事。
# 只要硬體在就把 GPU 版套件裝好；這一次要用 GPU 還是 CPU，交給每次啟動時的
# 實際偵測（PROVIDER 預設 auto）。切回獨顯之後不用重裝。
has_nvidia=0
if [ "$force_cpu" = 0 ]; then
  command -v nvidia-smi >/dev/null 2>&1 && has_nvidia=1
  if command -v lspci >/dev/null 2>&1 && lspci 2>/dev/null | grep -qi 'nvidia'; then
    has_nvidia=1
  fi
fi

if [ "$force_cpu" = 1 ]; then
  echo '  [-] 依 --cpu 參數跳過 GPU'
elif [ "$has_nvidia" = 0 ]; then
  echo '  [-] 這台機器上找不到 NVIDIA 顯示卡，使用 CPU'
else
  if smi=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null); then
    echo "  [v] 顯示卡：$smi"
  else
    echo '  [!] nvidia-smi 跑不起來，直接實測 CUDA 能不能用…'
  fi
  if ! "$vp" -m pip show sherpa-onnx 2>/dev/null | grep -qi 'cuda'; then
    echo '  安裝 GPU 版 sherpa-onnx（約 200 MB）…'
    "$vp" -m pip install --force-reinstall --quiet \
        "sherpa-onnx==$SHERPA_VERSION+cuda12.cudnn9" --no-index -f "$CUDA_INDEX" \
      || echo '  GPU 版安裝失敗，改用 CPU。'
  fi
  need_runtime=0
  for pkg in "${CUDA_RUNTIME[@]}"; do
    "$vp" -m pip show "$pkg" >/dev/null 2>&1 || need_runtime=1
  done
  if [ "$need_runtime" = 1 ]; then
    echo '  安裝 CUDA 12 執行環境與 cuDNN 9（pip 版，約 600 MB）…'
    "$vp" -m pip install --quiet "${CUDA_RUNTIME[@]}" \
      || echo '  CUDA 執行環境安裝失敗，改用 CPU。'
  fi
  echo '  實測 CUDA 是否真的能用 …'
  if probe_cuda; then gpu_on=1; fi
  if [ "$gpu_on" = 1 ]; then
    echo '  [v] GPU 已啟用（語音辨識快約 14 倍；講者分離實測 CPU 較快，維持 CPU）'
  else
    echo '  [-] GPU 沒能啟用，使用 CPU。功能完全一樣，只有辨識那段慢一點。'
  fi
fi

# --- 5. 模型 ---------------------------------------------------------------
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
  echo ''
  echo '  以下模型沒下載成功：'
  for f in "${failed[@]}"; do echo "    - $f"; done
  echo '  再執行一次 ./setup.sh 會重抓這幾個，已完成的不會重來。'
  exit 1
fi
echo '  [v] 模型檔案齊全'

# --- 6. 記下安裝完成 -------------------------------------------------------
# run.sh 靠這個判斷不用再跑一次安裝。存 requirements.txt 的雜湊，套件清單
# 改了就會重跑。
# 第一行是 requirements.txt 的雜湊（套件清單改了就重跑），第二行是裝的是哪一
# 種 wheel。記 wheel 的原因：安裝時沒有顯示卡就會裝 CPU 版，之後插了顯示卡也
# 永遠用不到 GPU。run.sh 只做 cpu -> gpu 的升級，不會反過來 —— GPU 版在沒有
# 顯示卡時本來就會退回 CPU，所以不必降級，也才不會因為獨顯開關而每次都重跑。
req_hash() {
  if   command -v sha256sum >/dev/null 2>&1; then sha256sum requirements.txt | cut -d' ' -f1
  elif command -v shasum    >/dev/null 2>&1; then shasum -a 256 requirements.txt | cut -d' ' -f1
  else echo 'nohash'; fi
}
wheel='cpu'
if "$vp" -m pip show sherpa-onnx 2>/dev/null | grep -i '^Version:' | grep -qi 'cuda'; then
  wheel='gpu'
fi
{ req_hash; echo "wheel=$wheel"; } > .venv/.scribe-ready

echo ''
echo '  安裝完成。'
if [ "$gpu_on" = 1 ]; then
  echo '  運算裝置：GPU（辨識）＋ CPU（講者分離）'
else
  echo '  運算裝置：CPU'
fi
echo ''
echo '    ./run.sh       啟動'
echo ''
