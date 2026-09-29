<#
  啟動會議逐字稿工具。
  用法：
    .\run.ps1                 啟動網頁介面
    .\run.ps1 -Port 8000      指定連接埠
    .\run.ps1 models          列出辨識模型
    .\run.ps1 pull x-asr-zipformer-punct
    .\run.ps1 run meeting.m4a -o out.txt
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host '  尚未安裝，先執行 setup.ps1 …' -ForegroundColor Yellow
    & powershell -ExecutionPolicy Bypass -File (Join-Path $root 'setup.ps1')
    if (-not (Test-Path $venvPy)) { exit 1 }
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

& $venvPy -m app @Args
