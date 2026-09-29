<#
  啟動會議逐字稿工具。

  這是唯一一個你需要記得的指令。第一次執行會自動安裝（建立 .venv、裝套件、
  有顯示卡就開 GPU、下載模型），之後就直接啟動。

  用法：
    .\run.ps1                 啟動網頁介面
    .\run.ps1 -Port 8000      指定連接埠
    .\run.ps1 -Setup          強制重跑一次安裝檢查
    .\run.ps1 models          列出辨識模型
    .\run.ps1 pull x-asr-zipformer-punct
    .\run.ps1 run meeting.m4a -o out.txt
#>
param(
    [switch]$Setup,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

$venvPy = Join-Path $root '.venv\Scripts\python.exe'
$stamp = Join-Path $root '.venv\.scribe-ready'

# Set the console to UTF-8 *before* anything prints - setup.ps1 runs below and
# its Chinese would come out as mojibake otherwise, which is precisely the
# first-run experience this launcher exists to make painless.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# 安裝好了沒？setup.ps1 跑完才會寫下 stamp。第一行是 requirements.txt 的雜湊，
# 套件清單改過就重跑；中途被 Ctrl-C 打斷時 stamp 不存在，下次啟動自動補完。
#
# 顯示卡是**每次啟動由程式自己實測**的（PROVIDER 預設 auto，沒有任何硬碟快取），
# 所以筆電的獨顯開開關關不需要重裝，這裡也刻意不去管它現在開著沒有。
# 唯一的例外：安裝當時整台機器都沒有顯示卡，裝的是 CPU 版 wheel，那之後永遠
# 用不到 GPU。所以這裡只補這一種情況，而且只做 cpu -> gpu，不會反過來。
function Test-Ready {
    if (-not (Test-Path $venvPy)) { return $false }
    if (-not (Test-Path $stamp)) { return $false }

    $lines = @(Get-Content $stamp -ErrorAction SilentlyContinue)
    $want = (Get-FileHash (Join-Path $root 'requirements.txt') -Algorithm SHA256).Hash
    if ($lines.Count -lt 1 -or $lines[0].Trim() -ne $want) { return $false }

    if (($lines -join "`n") -match 'wheel=cpu') {
        $nv = Get-PnpDevice -Class Display -ErrorAction SilentlyContinue |
              Where-Object { $_.InstanceId -match 'VEN_10DE' }
        if ($nv -or (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
            Write-Host '  偵測到 NVIDIA 顯示卡，但目前裝的是 CPU 版，補裝 GPU 版…' -ForegroundColor Cyan
            return $false
        }
    }
    return $true
}

if ($Setup -or -not (Test-Ready)) {
    if (-not (Test-Path $venvPy)) {
        Write-Host '  第一次執行，先安裝（約 5–10 分鐘，大部分時間在下載模型）…' -ForegroundColor Cyan
    } else {
        Write-Host '  環境有變動，補跑安裝檢查…' -ForegroundColor Cyan
    }
    & powershell -ExecutionPolicy Bypass -File (Join-Path $root 'setup.ps1')
    if ($LASTEXITCODE -ne 0) {
        Write-Host '  安裝沒有完成，請看上面的訊息。' -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path $venvPy)) { exit 1 }
}

& $venvPy -m app @Args
