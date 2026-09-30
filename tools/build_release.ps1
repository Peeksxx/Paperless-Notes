# Builds the local release: PyInstaller onedir, legal files and manifest, verification, portable zip,
# the Inno Setup installer when ISCC.exe is already installed, and SHA-256 sums. Nothing is downloaded,
# installed, signed, tagged or published. Output goes to build\ and dist\ only (both ignored by git).
# Usage: powershell -ExecutionPolicy Bypass -File tools\build_release.ps1 [-AllowDirty] [-SkipInstaller]
param([switch]$AllowDirty, [switch]$SkipInstaller)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$py = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$env:PYTHONDONTWRITEBYTECODE = "1"

function Invoke-Step([string]$Name, [scriptblock]$Command) {
    Write-Host "== $Name"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $Name"
        exit 1
    }
}

$dirty = @()
if ($AllowDirty) { $dirty = @("--allow-dirty") }
Invoke-Step "preflight" { & $py tools\release.py preflight @dirty }
Invoke-Step "check installer script" { & $py tools\release.py check-installer }
Invoke-Step "clean earlier output" { & $py tools\release.py clean }
Invoke-Step "pyinstaller onedir" {
    & $py -m PyInstaller --noconfirm --clean --log-level WARN --distpath dist --workpath build\pyinstaller packaging\paperless_notes.spec
}
Invoke-Step "stage legal files and manifest" { & $py tools\release.py stage }
Invoke-Step "verify the onedir tree" { & $py tools\release.py verify }
if ($AllowDirty) {
    Write-Host "Built from uncommitted changes: the portable zip and installer are not produced."
} else {
    Invoke-Step "portable zip" { & $py tools\release.py portable }
    $iscc = $null
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if (-not $iscc -and (Test-Path $candidate)) { $iscc = $candidate }
    }
    if ($SkipInstaller) {
        Write-Host "Installer skipped on request."
    } elseif (-not $iscc) {
        Write-Host "Inno Setup (ISCC.exe) is not installed: the installer was not built."
    } else {
        $version = & $py -c "import sys; sys.path.insert(0, 'src'); from paperless_notes.branding import VERSION; print(VERSION)"
        Invoke-Step "inno setup installer" {
            & $iscc "/DAppVersion=$version" "/DSourceDir=$((Resolve-Path 'dist\Paperless Notes').Path)" "/DOutputDir=$((Resolve-Path 'dist\release').Path)" packaging\installer.iss
        }
    }
    Invoke-Step "sha-256 sums" { & $py tools\release.py sums }
}
Invoke-Step "postflight" { & $py tools\release.py postflight }
Write-Host "Release build finished"
