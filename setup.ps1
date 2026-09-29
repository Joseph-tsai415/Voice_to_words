<#
  一次性安裝：建立 .venv、安裝套件、檢查模型。
  用法：  powershell -ExecutionPolicy Bypass -File setup.ps1
#>
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

Write-Host ''
Write-Host '  會議逐字稿工具 — 安裝' -ForegroundColor Cyan
Write-Host '  ----------------------------------------'

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
    Write-Host '  找不到 Python 3.9+，嘗試自動安裝…' -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
        $py = Find-Python
    }
    if (-not $py) {
        Write-Host '  自動安裝失敗。請到 https://www.python.org/downloads/ 手動安裝後再執行本腳本。' -ForegroundColor Red
        exit 1
    }
}
Write-Host "  Python： $(& $py --version)" -ForegroundColor Green

# --- 2. 虛擬環境 ------------------------------------------------------------
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host '  建立虛擬環境 .venv …'
    & $py -m venv (Join-Path $root '.venv')
}
if (-not (Test-Path $venvPy)) { Write-Host '  建立 .venv 失敗。' -ForegroundColor Red; exit 1 }

# --- 3. 套件 ---------------------------------------------------------------
Write-Host '  安裝相依套件（第一次約 1–3 分鐘）…'
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $root 'requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) { Write-Host '  套件安裝失敗。' -ForegroundColor Red; exit 1 }
Write-Host '  套件安裝完成' -ForegroundColor Green

# --- 4. 模型 ---------------------------------------------------------------
# 模型太大不進版控，第一次安裝時從上游抓。每個 Size 都是對過的實際位元組數，
# 下載完會驗證；大小不符就當作失敗，不留下一個壞掉的檔案。
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
    @{ Path = 'models\sense-voice\tokens.txt'
       Url  = 'https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt?download=true'
       Size = 315894 }
    @{ Path = 'models\sense-voice\model.onnx'
       Url  = 'https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.onnx?download=true'
       Size = 937617178 }
)

function Get-ModelFile($item) {
    $dest = Join-Path $root $item.Path
    if ((Test-Path $dest) -and (Get-Item $dest).Length -eq $item.Size) { return $true }

    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    $part = "$dest.part"
    $label = if ($item.Size -ge 1MB) { "$([math]::Round($item.Size / 1MB)) MB" }
             else { "$([math]::Round($item.Size / 1KB)) KB" }
    Write-Host "    下載 $($item.Path)（$label）…"

    # Invoke-WebRequest 的進度條會讓大檔慢上好幾倍，關掉。
    $prev = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest -Uri $item.Url -OutFile $part -UseBasicParsing -MaximumRedirection 10
    } catch {
        Write-Host "      失敗：$($_.Exception.Message)" -ForegroundColor Red
        if (Test-Path $part) { Remove-Item $part -Force }
        return $false
    } finally {
        $ProgressPreference = $prev
    }

    $got = (Get-Item $part).Length
    if ($got -ne $item.Size) {
        Write-Host "      大小不符：拿到 $got 位元組，應該是 $($item.Size)" -ForegroundColor Red
        Remove-Item $part -Force
        return $false
    }
    Move-Item -Force $part $dest
    return $true
}

Write-Host '  檢查模型檔案（第一次要下載約 1 GB，之後會跳過）…'
$failed = @()
foreach ($m in $models) {
    if (-not (Get-ModelFile $m)) { $failed += $m.Path }
}
if ($failed) {
    Write-Host '  以下模型沒下載成功：' -ForegroundColor Yellow
    $failed | ForEach-Object { Write-Host "    - $_" -ForegroundColor Yellow }
    Write-Host '  再執行一次 setup.ps1 會重抓這幾個，已完成的不會重來。' -ForegroundColor Yellow
} else {
    Write-Host '  模型檔案齊全' -ForegroundColor Green
}

Write-Host ''
Write-Host '  安裝完成。啟動方式：' -ForegroundColor Cyan
Write-Host '    .\run.ps1'
Write-Host ''
