#!/usr/bin/env bash
# 啟動會議逐字稿工具。
#
# 這是唯一一個你需要記得的指令。第一次執行會自動安裝（建立 .venv、裝套件、
# 有顯示卡就開 GPU、下載模型），之後就直接啟動。
#
#   ./run.sh                 啟動網頁介面
#   ./run.sh --port 8000     指定連接埠
#   ./run.sh --setup         強制重跑一次安裝檢查
#   ./run.sh models          列出辨識模型
#   ./run.sh run meeting.m4a -o out.txt
set -euo pipefail
cd "$(dirname "$0")"

venv_py() {
  if   [ -x '.venv/bin/python' ];         then echo '.venv/bin/python'
  elif [ -x '.venv/Scripts/python.exe' ]; then echo '.venv/Scripts/python.exe'
  else echo ''; fi
}

req_hash() {
  if   command -v sha256sum >/dev/null 2>&1; then sha256sum requirements.txt | cut -d' ' -f1
  elif command -v shasum    >/dev/null 2>&1; then shasum -a 256 requirements.txt | cut -d' ' -f1
  else echo 'nohash'; fi
}

# 安裝好了沒？setup.sh 跑完才會寫下 .scribe-ready。第一行是 requirements.txt
# 的雜湊，套件清單改過就重跑；中途被 Ctrl-C 打斷時檔案不存在，下次啟動補完。
#
# 顯示卡是**每次啟動由程式自己實測**的（PROVIDER 預設 auto，沒有硬碟快取），
# 所以獨顯開開關關不需要重裝，這裡刻意不管它現在開著沒有。唯一的例外是安裝
# 當時整台機器都沒有顯示卡，裝的是 CPU 版 wheel —— 只補這種情況，而且只做
# cpu -> gpu，不會反過來。
ready() {
  [ -n "$(venv_py)" ] || return 1
  [ -f '.venv/.scribe-ready' ] || return 1
  [ "$(head -n1 .venv/.scribe-ready)" = "$(req_hash)" ] || return 1

  if grep -q 'wheel=cpu' .venv/.scribe-ready; then
    if command -v nvidia-smi >/dev/null 2>&1 ||
       { command -v lspci >/dev/null 2>&1 && lspci 2>/dev/null | grep -qi 'nvidia'; }; then
      echo '  偵測到 NVIDIA 顯示卡，但目前裝的是 CPU 版，補裝 GPU 版…'
      return 1
    fi
  fi
}

force_setup=0
args=()
for a in "$@"; do
  if [ "$a" = '--setup' ]; then force_setup=1; else args+=("$a"); fi
done

if [ "$force_setup" = 1 ] || ! ready; then
  if [ -z "$(venv_py)" ]; then
    echo '  第一次執行，先安裝（約 5–10 分鐘，大部分時間在下載模型）…'
  else
    echo '  環境有變動，補跑安裝檢查…'
  fi
  bash ./setup.sh
fi

PY=$(venv_py)
if [ -z "$PY" ]; then echo '  安裝沒有完成，請看上面的訊息。' >&2; exit 1; fi

export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
exec "$PY" -m app ${args[@]+"${args[@]}"}
