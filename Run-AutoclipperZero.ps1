#requires -Version 5.1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host 'AutoclipperZero is not installed yet. Run .\Setup.ps1 first.' -ForegroundColor Yellow
    exit 1
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host 'FFmpeg is not on PATH. Close and reopen PowerShell after Setup.ps1, or install FFmpeg Essentials.' -ForegroundColor Red
    exit 1
}

# Quick dependency check so a broken install is reported clearly instead of failing later in the log.
$ErrorActionPreference = 'Continue'
& $VenvPython -c "import numpy, av, faster_whisper, streamlink, yt_dlp" 2>$null
$DepsOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if (-not $DepsOk) {
    Write-Host 'Some Python packages are missing or broken. Run .\Setup.ps1 again.' -ForegroundColor Red
    exit 1
}

& $VenvPython (Join-Path $Root 'autoclipperzero.py')
if ($LASTEXITCODE -ne 0) {
    Write-Host ("AutoclipperZero exited with code {0}. Run Check setup inside the app." -f $LASTEXITCODE) -ForegroundColor Red
}
