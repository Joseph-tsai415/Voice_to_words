<#
  開啟 GPU 加速。

  用法：  powershell -ExecutionPolicy Bypass -File setup-gpu.ps1
          powershell -ExecutionPolicy Bypass -File setup-gpu.ps1 -Revert   # 換回 CPU 版

  不需要安裝 3 GB 的 CUDA Toolkit —— NVIDIA 的 pip 套件就帶了需要的 DLL。
  只需要顯示卡已啟用且裝好驅動（nvidia-smi 可執行）。
#>
param([switch]$Revert)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host '  找不到 .venv，請先執行 setup.ps1' -ForegroundColor Red
    exit 1
}

$version = '1.13.6'
$cudaIndex = 'https://k2-fsa.github.io/sherpa/onnx/cuda.html'
$runtimePkgs = @('nvidia-cuda-runtime-cu12', 'nvidia-cudnn-cu12',
                 'nvidia-cublas-cu12', 'nvidia-cufft-cu12', 'nvidia-curand-cu12')

if ($Revert) {
    Write-Host ''
    Write-Host '  換回 CPU 版 sherpa-onnx …' -ForegroundColor Cyan
    & $venvPy -m pip install --force-reinstall "sherpa-onnx==$version"
    Write-Host '  完成。CUDA 執行環境套件留著不影響 CPU 使用。' -ForegroundColor Green
    exit 0
}

Write-Host ''
Write-Host '  GPU 加速設定' -ForegroundColor Cyan
Write-Host '  ----------------------------------------'

# --- 1. 顯示卡與驅動 --------------------------------------------------------
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Host '  [X] 找不到 nvidia-smi' -ForegroundColor Red
    Write-Host ''
    $nv = Get-PnpDevice -Class Display -ErrorAction SilentlyContinue |
          Where-Object { $_.InstanceId -match 'VEN_10DE' }
    if ($nv) {
        Write-Host "      偵測到 NVIDIA 裝置，但狀態是：$($nv.Status)" -ForegroundColor Yellow
        Write-Host '      請先在「裝置管理員 > 顯示卡」啟用它，並安裝 NVIDIA 驅動。'
        Write-Host '      筆電可能還需要在 BIOS 或 NVIDIA 控制面板切換獨顯模式。'
    } else {
        Write-Host '      這台機器上找不到 NVIDIA 顯示卡。'
    }
    Write-Host ''
    Write-Host '      驅動下載： https://www.nvidia.com/download/index.aspx'
    exit 1
}
Write-Host '  [v] NVIDIA 驅動已安裝' -ForegroundColor Green
& nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader |
    ForEach-Object { Write-Host "      $_" }

# --- 2. GPU 版 sherpa-onnx --------------------------------------------------
$installed = ''
$show = & $venvPy -m pip show sherpa-onnx 2>$null
if ($show) { $installed = ($show | Select-String '^Version:') -replace 'Version:\s*', '' }

if ($installed -match 'cuda') {
    Write-Host "  [v] 已安裝 GPU 版 sherpa-onnx（$installed）" -ForegroundColor Green
} else {
    Write-Host "  安裝 sherpa-onnx $version+cuda12.cudnn9 …（約 200 MB）" -ForegroundColor Cyan
    & $venvPy -m pip install --force-reinstall `
        "sherpa-onnx==$version+cuda12.cudnn9" --no-index -f $cudaIndex
    if ($LASTEXITCODE -ne 0) {
        Write-Host '  安裝失敗。可改試 CUDA 11.8 版：' -ForegroundColor Yellow
        Write-Host "    $venvPy -m pip install --force-reinstall ""sherpa-onnx==$version+cuda"" --no-index -f $cudaIndex"
        exit 1
    }
}

# --- 3. CUDA 執行環境（pip 版，免裝 Toolkit）--------------------------------
$needRuntime = $false
foreach ($pkg in $runtimePkgs) {
    & $venvPy -m pip show $pkg *> $null
    if ($LASTEXITCODE -ne 0) { $needRuntime = $true }
}
if ($needRuntime) {
    Write-Host '  安裝 CUDA 12 執行環境與 cuDNN 9（pip 版，約 600 MB）…' -ForegroundColor Cyan
    & $venvPy -m pip install @runtimePkgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host '  CUDA 執行環境安裝失敗。' -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host '  [v] CUDA 執行環境與 cuDNN 已就緒' -ForegroundColor Green
}

# --- 4. 實際測試 ------------------------------------------------------------
Write-Host ''
Write-Host '  實測 CUDA 是否真的能用 …' -ForegroundColor Cyan
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
# 多行 Python 用 -c 傳會被命令列引號拆壞，寫成檔案再執行。
$probeFile = Join-Path $env:TEMP 'scribe-gpu-probe.py'
@'
import sys
sys.path.insert(0, sys.argv[1])
from app import gpu
from app.config import DIARIZE_PROVIDER, PROVIDER
gpu.refresh()
st = gpu.status(PROVIDER)
print("    ASR      :", st["provider"].upper())
print("    Diarize  :", gpu.resolve(DIARIZE_PROVIDER)[0].upper(), "(CPU is faster here)")
print("   ", st["reason"])
if st["install_hint"]:
    print("    Missing  :", st["install_hint"])
sys.exit(0 if st["provider"] == "cuda" else 1)
'@ | Set-Content -Path $probeFile -Encoding UTF8

& $venvPy $probeFile $root
$ok = ($LASTEXITCODE -eq 0)

Write-Host ''
if ($ok) {
    Write-Host '  完成，GPU 已啟用。直接跑 .\run.ps1 即可。' -ForegroundColor Green
    Write-Host ''
    Write-Host '  實測（RTX 3070 Ti，5 分鐘錄音）：' -ForegroundColor DarkGray
    Write-Host '    語音辨識  12.6s -> 0.9s   快 14 倍' -ForegroundColor DarkGray
    Write-Host '    講者分離  55.3s -> 91.7s  GPU 反而慢，所以維持 CPU' -ForegroundColor DarkGray
    Write-Host '    整體      60.7s -> 44.8s' -ForegroundColor DarkGray
} else {
    Write-Host '  GPU 尚未啟用，工具會自動使用 CPU（功能不受影響）。' -ForegroundColor Yellow
}
Write-Host ''
Write-Host '  強制用 CPU：            $env:SCRIBE_PROVIDER = "cpu"' -ForegroundColor DarkGray
Write-Host '  講者分離也用 GPU：      $env:SCRIBE_DIARIZE_PROVIDER = "cuda"' -ForegroundColor DarkGray
Write-Host ''
