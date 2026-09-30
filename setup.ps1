<#
  安裝與環境檢查。可以重複執行，已經好的步驟會自動跳過。

  平常你不用自己跑這個 —— run.ps1 會在需要的時候自動叫它。

  用法：
    .\setup.ps1          自動：偵測得到 NVIDIA 顯示卡就開 GPU 加速，沒有就純 CPU
    .\setup.ps1 -Cpu     強制純 CPU，完全不碰 GPU
    .\setup.ps1 -Revert  已經裝了 GPU 版，換回 CPU 版

  GPU 是預設會嘗試的，但**失敗不會讓安裝失敗**：沒有顯示卡、驅動沒裝好、
  CUDA 起不來，都只是退回 CPU 繼續跑完，功能完全一樣，只有辨識那段慢一點。
#>
param([switch]$Cpu, [switch]$Revert)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

$SherpaVersion = '1.13.6'
$CudaIndex = 'https://k2-fsa.github.io/sherpa/onnx/cuda.html'
$CudaRuntime = @('nvidia-cuda-runtime-cu12', 'nvidia-cudnn-cu12',
                 'nvidia-cublas-cu12', 'nvidia-cufft-cu12', 'nvidia-curand-cu12')

$venvPy = Join-Path $root '.venv\Scripts\python.exe'
$stamp = Join-Path $root '.venv\.scribe-ready'

function Say($text, $colour = 'Gray') { Write-Host $text -ForegroundColor $colour }

Write-Host ''
Say '  會議逐字稿工具 — 安裝' 'Cyan'
Say '  ----------------------------------------'

# --- 0. 只是要換回 CPU 版 ---------------------------------------------------
if ($Revert) {
    if (-not (Test-Path $venvPy)) { Say '  還沒安裝過，沒有東西要還原。' 'Yellow'; exit 0 }
    Say '  換回 CPU 版 sherpa-onnx …' 'Cyan'
    & $venvPy -m pip install --force-reinstall "sherpa-onnx==$SherpaVersion" --quiet
    Say '  完成。CUDA 執行環境套件留著不影響 CPU 使用。' 'Green'
    exit 0
}

# --- 1. 找到 Python ---------------------------------------------------------
function Find-Python {
    foreach ($c in @('python', 'py', 'python3')) {
        try {
            $v = & $c --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $v -match 'Python (\d+)\.(\d+)') {
                if ([int]$Matches[1] -ge 3 -and [int]$Matches[2] -ge 9) { return $c }
            }
        } catch { }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Say '  找不到 Python 3.9+，嘗試自動安裝…' 'Yellow'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Python.Python.3.12 -e --source winget `
            --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
        $py = Find-Python
    }
    if (-not $py) {
        Say '  自動安裝失敗。請到 https://www.python.org/downloads/ 裝好後再執行一次。' 'Red'
        exit 1
    }
}
Say "  [v] Python $((& $py --version) -replace 'Python ')" 'Green'

# --- 2. 虛擬環境 ------------------------------------------------------------
if (-not (Test-Path $venvPy)) {
    Say '  建立虛擬環境 .venv …'
    & $py -m venv (Join-Path $root '.venv')
}
if (-not (Test-Path $venvPy)) { Say '  建立 .venv 失敗。' 'Red'; exit 1 }
Say '  [v] 虛擬環境' 'Green'

# --- 3. 套件 ---------------------------------------------------------------
Say '  安裝相依套件（第一次約 1–3 分鐘）…'
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $root 'requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) { Say '  套件安裝失敗。' 'Red'; exit 1 }
Say '  [v] 相依套件' 'Green'

# --- 4. GPU 加速（預設就試，失敗只是退回 CPU）--------------------------------
# 不需要 3 GB 的 CUDA Toolkit：NVIDIA 的 pip 套件就帶了需要的 DLL。
function Test-CudaReally {
    # 光看套件名稱會騙人 —— CUDA 版的 wheel 沒有 CUDA 執行環境也裝得起來，
    # onnxruntime 只會印一行 "Fallback to cpu!" 就繼續跑。所以真的載一個模型試。
    # 多行 Python 用 -c 傳會被命令列引號拆壞，寫成檔案再執行。
    $probe = Join-Path $env:TEMP 'scribe-gpu-probe.py'
    @'
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
'@ | Set-Content -Path $probe -Encoding UTF8
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    # Capture, then read the exit code, then print. Letting the probe write
    # straight to the pipeline would make this function return an array of
    # its output *plus* the boolean, and a non-empty array is truthy - so a
    # failed probe would have reported GPU enabled. That is the same false
    # positive the probe exists to prevent.
    $out = & $venvPy $probe $root 2>&1
    $ok = ($LASTEXITCODE -eq 0)
    $out | Where-Object { $_ -match '\S' } | ForEach-Object { Say "    $_" 'DarkGray' }
    return $ok
}

# 筆電的獨顯可以被關掉（Optimus / MUX / 裝置管理員停用），所以「現在能不能用」
# 和「這台機器有沒有顯示卡」是兩回事。安裝時只要**硬體在**就把 GPU 版套件裝好，
# 至於這一次要用 GPU 還是 CPU，交給每次啟動時的實際偵測（PROVIDER 預設 auto）。
# 這樣把獨顯切回來之後不用重裝，直接跑 run.ps1 就會用到 GPU。
$gpuOn = $false
$hasNvidia = $false
$hwStatus = ''
if (-not $Cpu) {
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { $hasNvidia = $true }
    $nv = Get-PnpDevice -Class Display -ErrorAction SilentlyContinue |
          Where-Object { $_.InstanceId -match 'VEN_10DE' } | Select-Object -First 1
    if ($nv) { $hasNvidia = $true; $hwStatus = $nv.Status }
}

if ($Cpu) {
    Say '  [-] 依 -Cpu 參數跳過 GPU' 'DarkGray'
} elseif (-not $hasNvidia) {
    Say '  [-] 這台機器上找不到 NVIDIA 顯示卡，使用 CPU' 'DarkGray'
} else {
    try {
        # nvidia-smi can be present but refuse to run (permissions, sandbox).
        # Only believe it when it exits 0 - otherwise it prints its complaint
        # on stdout and we would show that as the card's name. The real probe
        # below is the thing that decides, so a failure here is not fatal.
        $smi = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>&1
        if ($LASTEXITCODE -eq 0) {
            $smi | Where-Object { $_ -match '\S' } |
                ForEach-Object { Say "  [v] 顯示卡：$_" 'Green' }
        } else {
            Say '  [!] nvidia-smi 跑不起來，直接實測 CUDA 能不能用…' 'DarkGray'
        }

        $installed = ''
        $show = & $venvPy -m pip show sherpa-onnx 2>$null
        if ($show) { $installed = ($show | Select-String '^Version:') -replace 'Version:\s*', '' }

        if ($installed -notmatch 'cuda') {
            Say "  安裝 GPU 版 sherpa-onnx（約 200 MB）…" 'Cyan'
            & $venvPy -m pip install --force-reinstall --quiet `
                "sherpa-onnx==$SherpaVersion+cuda12.cudnn9" --no-index -f $CudaIndex
        }

        $needRuntime = $false
        foreach ($pkg in $CudaRuntime) {
            & $venvPy -m pip show $pkg *> $null
            if ($LASTEXITCODE -ne 0) { $needRuntime = $true }
        }
        if ($needRuntime) {
            Say '  安裝 CUDA 12 執行環境與 cuDNN 9（pip 版，約 600 MB）…' 'Cyan'
            & $venvPy -m pip install --quiet @CudaRuntime
        }

        Say '  實測 CUDA 是否真的能用 …' 'Cyan'
        $gpuOn = Test-CudaReally
    } catch {
        Say "  GPU 設定沒成功（$($_.Exception.Message)），改用 CPU。" 'Yellow'
        $gpuOn = $false
    }
    if ($gpuOn) {
        Say '  [v] GPU 已啟用（語音辨識快約 14 倍；講者分離實測 CPU 較快，維持 CPU）' 'Green'
    } else {
        $why = if ($hwStatus -and $hwStatus -ne 'OK') { "顯示卡目前的狀態是 $hwStatus" }
               else { '顯示卡目前不可用' }
        Say "  [-] $why，這次用 CPU。" 'Yellow'
        Say '      GPU 版套件已經裝好了。之後把獨顯切回來（BIOS／NVIDIA 控制面板／' 'DarkGray'
        Say '      裝置管理員），直接再跑 .\run.ps1 就會自動用 GPU，不必重裝。' 'DarkGray'
    }
}

# --- 5. 模型 ---------------------------------------------------------------
# 模型太大不進版控，第一次安裝時從上游抓。每個大小都是對過的實際位元組數，
# 下載完會驗證；大小不符就當作失敗，不留下一個壞掉的檔案。
$XASR = 'https://huggingface.co/csukuangfj2/sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03/resolve/main'

$models = @(
    @{ Path = 'models\silero_vad.onnx'
       Url  = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx'
       Size = 643854 }
    @{ Path = 'models\segmentation\model.onnx'
       Url  = 'https://huggingface.co/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0/resolve/main/model.onnx?download=true'
       Size = 5992913 }
    @{ Path = 'models\speaker-embedding.onnx'
       Url  = 'https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/main/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx?download=true'
       Size = 39593761 }
    # The default recogniser: X-ASR, not SenseVoice. Measured on a real
    # bilingual seminar it recovered 9 of 13 technical terms against
    # SenseVoice's 5, and it is 176 MB instead of 940 MB - so the whole
    # install drops from about 1 GB to about 220 MB. SenseVoice is still in
    # the catalogue and downloads on demand if someone wants日/韓/粵.
    @{ Path = 'models\hub\x-asr-zipformer-punct\tokens.txt'
       Url  = "$XASR/tokens.txt?download=true"
       Size = 58806 }
    @{ Path = 'models\hub\x-asr-zipformer-punct\joiner-epoch-99-avg-1.int8.onnx'
       Url  = "$XASR/joiner-epoch-99-avg-1.int8.onnx?download=true"
       Size = 2581422 }
    @{ Path = 'models\hub\x-asr-zipformer-punct\decoder-epoch-99-avg-1.onnx'
       Url  = "$XASR/decoder-epoch-99-avg-1.onnx?download=true"
       Size = 11309084 }
    @{ Path = 'models\hub\x-asr-zipformer-punct\encoder-epoch-99-avg-1.int8.onnx'
       Url  = "$XASR/encoder-epoch-99-avg-1.int8.onnx?download=true"
       Size = 161744450 }
)

function Get-ModelFile($item) {
    $dest = Join-Path $root $item.Path
    if ((Test-Path $dest) -and (Get-Item $dest).Length -eq $item.Size) { return $true }

    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    $part = "$dest.part"
    $label = if ($item.Size -ge 1MB) { "$([math]::Round($item.Size / 1MB)) MB" }
             else { "$([math]::Round($item.Size / 1KB)) KB" }
    Say "    下載 $($item.Path)（$label）…"

    # Invoke-WebRequest 的進度條會讓大檔慢上好幾倍，關掉。
    $prev = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest -Uri $item.Url -OutFile $part -UseBasicParsing -MaximumRedirection 10
    } catch {
        Say "      失敗：$($_.Exception.Message)" 'Red'
        if (Test-Path $part) { Remove-Item $part -Force }
        return $false
    } finally {
        $ProgressPreference = $prev
    }

    $got = (Get-Item $part).Length
    if ($got -ne $item.Size) {
        Say "      大小不符：拿到 $got 位元組，應該是 $($item.Size)" 'Red'
        Remove-Item $part -Force
        return $false
    }
    Move-Item -Force $part $dest
    return $true
}

$missing = $models | Where-Object {
    $d = Join-Path $root $_.Path
    -not ((Test-Path $d) -and (Get-Item $d).Length -eq $_.Size)
}
if ($missing) {
    Say '  下載模型（第一次約 220 MB）…' 'Cyan'
} else {
    Say '  [v] 模型檔案齊全' 'Green'
}

$failed = @()
foreach ($m in $models) {
    if (-not (Get-ModelFile $m)) { $failed += $m.Path }
}

if ($failed) {
    Write-Host ''
    Say '  以下模型沒下載成功：' 'Yellow'
    $failed | ForEach-Object { Say "    - $_" 'Yellow' }
    Say '  再執行一次 .\setup.ps1 會重抓這幾個，已完成的不會重來。' 'Yellow'
    Write-Host ''
    exit 1
}
if ($missing) { Say '  [v] 模型檔案齊全' 'Green' }

# --- 6. 記下安裝完成 --------------------------------------------------------
# run.ps1 靠這個判斷不用再跑一次安裝。第一行是 requirements.txt 的雜湊（套件
# 清單改了就重跑），第二行是裝的是哪一種 wheel。
#
# 記 wheel 的原因：如果安裝時這台機器上沒有顯示卡，裝的就是 CPU 版，之後就算
# 插了顯示卡也永遠用不到 GPU。run.ps1 會比對，只做 cpu -> gpu 的升級，不會反
# 過來 —— GPU 版的 wheel 在沒有顯示卡時本來就會自動退回 CPU，所以不需要降級，
# 也才不會因為獨顯開開關關而每次啟動都重跑安裝。
$wheel = 'cpu'
$show = & $venvPy -m pip show sherpa-onnx 2>$null
if ($show -and (($show | Select-String '^Version:') -match 'cuda')) { $wheel = 'gpu' }

@(
    (Get-FileHash (Join-Path $root 'requirements.txt') -Algorithm SHA256).Hash
    "wheel=$wheel"
) | Set-Content -Path $stamp -Encoding ASCII

Write-Host ''
Say '  安裝完成。' 'Green'
if ($gpuOn) {
    Say '  這次的運算裝置：GPU（辨識）＋ CPU（講者分離）'
} elseif ($hasNvidia -and -not $Cpu) {
    Say '  這次的運算裝置：CPU（顯示卡沒開，開了之後會自動改用 GPU）'
} else {
    Say '  這次的運算裝置：CPU'
}
# 裝置是每次啟動重新偵測的，不是裝好就定死 —— 筆電的獨顯可以隨時開關。
Say '  每次啟動都會重新偵測一次，所以顯示卡開開關關都不用重裝。' 'DarkGray'
Write-Host ''
Say '    .\run.ps1      啟動' 'Cyan'
Write-Host ''
