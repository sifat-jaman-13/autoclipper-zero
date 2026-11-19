#requires -Version 5.1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

Write-Host '=============================================' -ForegroundColor Cyan
Write-Host ' AutoclipperZero setup for Windows PowerShell 5.1' -ForegroundColor Cyan
Write-Host '=============================================' -ForegroundColor Cyan

# The user's verified Python path is checked first. Do not depend on the
# Python Install Manager or the legacy py.exe launcher.
$PythonCandidates = @()
if ($env:LOCALAPPDATA) {
    $PythonCandidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe')
    $PythonCandidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python315\python.exe')
    $PythonCandidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe')
    $PythonCandidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    $PythonCandidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')

    $PythonRoot = Join-Path $env:LOCALAPPDATA 'Programs\Python'
    if (Test-Path -LiteralPath $PythonRoot) {
        $InstalledFolders = Get-ChildItem -LiteralPath $PythonRoot -Directory -ErrorAction SilentlyContinue
        foreach ($Folder in $InstalledFolders) {
            $PythonCandidates += (Join-Path $Folder.FullName 'python.exe')
        }
    }
}

# Also inspect PATH for a real python.exe. Windows Store aliases will fail the
# interpreter probe and will simply be skipped.
$PythonCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($PythonCommand -and $PythonCommand.Source) {
    $PythonCandidates += $PythonCommand.Source
}

$PythonExe = $null
$PythonVersion = $null
$ProbeCode = "import sys,struct; print(str(sys.version_info[0])+'.'+str(sys.version_info[1])+'.'+str(sys.version_info[2])+'|'+str(struct.calcsize('P')*8))"

foreach ($Candidate in ($PythonCandidates | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { continue }

    try {
        $ProbeOutput = & $Candidate -c $ProbeCode 2>$null
        $ProbeExitCode = $LASTEXITCODE
        if ($ProbeExitCode -ne 0 -or -not $ProbeOutput) { continue }

        $ProbeText = [string]($ProbeOutput | Select-Object -Last 1)
        $ProbeText = $ProbeText.Trim()
        if ($ProbeText -match '^(\d+)\.(\d+)\.(\d+)\|(\d+)$') {
            $Major = [int]$Matches[1]
            $Minor = [int]$Matches[2]
            $Patch = [int]$Matches[3]
            $Bits = [int]$Matches[4]

            if ($Major -eq 3 -and $Minor -ge 11 -and $Minor -le 15 -and $Bits -eq 64) {
                $PythonExe = $Candidate
                $PythonVersion = '{0}.{1}.{2}' -f $Major, $Minor, $Patch
                break
            }
        }
    }
    catch {
        continue
    }
}

if (-not $PythonExe) {
    Write-Host 'A compatible 64-bit Python 3.11 to 3.15 was not detected.' -ForegroundColor Red
    Write-Host 'AutoclipperZero checked the usual per-user Python folders and PATH.' -ForegroundColor Yellow
    Write-Host 'Your previously verified interpreter should be at:' -ForegroundColor Yellow
    if ($env:LOCALAPPDATA) {
        Write-Host (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe') -ForegroundColor White
    }
    Write-Host 'Run this diagnostic command and share its output:' -ForegroundColor Yellow
    Write-Host '& "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe" -c "import sys,struct; print(sys.version, struct.calcsize(''P'')*8)"' -ForegroundColor Gray
    exit 1
}

Write-Host ("Using Python {0}: {1}" -f $PythonVersion, $PythonExe) -ForegroundColor Green

$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Write-Host 'Creating a private Python environment...' -ForegroundColor Yellow
    & $PythonExe -m venv (Join-Path $Root '.venv')
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create the Python virtual environment.'
    }
}

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw 'Virtual environment creation returned success, but .venv\Scripts\python.exe was not found.'
}

Write-Host 'Installing AutoclipperZero dependencies (Streamlink, yt-dlp, faster-whisper)...' -ForegroundColor Yellow
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Could not upgrade pip.' }
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }


# Verify the packages really import. This catches version clashes immediately.
Write-Host 'Checking the installed packages...' -ForegroundColor Yellow
$ErrorActionPreference = 'Continue'
$CheckOutput = & $VenvPython -c "import numpy, av, faster_whisper, streamlink, yt_dlp; print('ok')" 2>&1
$CheckExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($CheckExit -ne 0) {
    Write-Host 'A package failed to import:' -ForegroundColor Red
    Write-Host ($CheckOutput | Out-String) -ForegroundColor Gray
    Write-Host 'If this mentions av or ctranslate2 and you are on a very new Python, install Python 3.12 or 3.13 and run Setup again.' -ForegroundColor Yellow
    exit 1
}
Write-Host 'Packages OK.' -ForegroundColor Green

# Optional: NVIDIA CUDA libraries so Whisper can transcribe on the GPU (much faster than CPU).
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $Answer = Read-Host 'NVIDIA GPU detected. Install the CUDA libraries for GPU transcription (about 1 GB download)? [y/N]'
    if ($Answer -match '^(y|yes)$') {
        & $VenvPython -m pip install --disable-pip-version-check nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'CUDA libraries could not be installed. AutoclipperZero will use the CPU instead.' -ForegroundColor Yellow
        }
        else {
            Write-Host 'CUDA libraries installed. The app tests the GPU on first use and falls back to CPU if it does not work.' -ForegroundColor Green
        }
    }
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host 'FFmpeg is missing. Installing the free Gyan FFmpeg Essentials package...' -ForegroundColor Yellow
        & winget install --id Gyan.FFmpeg.Essentials --exact --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'winget did not complete FFmpeg installation. Follow the manual FFmpeg instructions in README.md.' -ForegroundColor Yellow
        }
        else {
            Write-Host 'FFmpeg installation completed. Close this PowerShell window and open a new one before launching AutoclipperZero.' -ForegroundColor Green
        }
    }
    else {
        Write-Host 'FFmpeg is missing and winget is unavailable.' -ForegroundColor Red
        Write-Host 'Install a Windows FFmpeg build and add its bin folder to PATH. See README.md.' -ForegroundColor Yellow
    }
}
else {
    Write-Host 'FFmpeg found.' -ForegroundColor Green
}

Write-Host ''
Write-Host 'Setup completed.' -ForegroundColor Green
Write-Host 'In a new PowerShell window, run: .\Run-AutoclipperZero.ps1' -ForegroundColor Cyan
