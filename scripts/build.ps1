# Build script: PyInstaller + Inno Setup
param(
    [switch]$Release,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$ROOT = Split-Path -Parent $ROOT

Push-Location $ROOT

try {
    Write-Host "=== Building zhongzhuan ==="

    # Clean
    if (Test-Path dist) { Remove-Item -Recurse -Force dist }
    if (Test-Path build) { Remove-Item -Recurse -Force build }

    # PyInstaller
    Write-Host "Running PyInstaller..."
    python -m PyInstaller `
        --onefile `
        --name zhongzhuan `
        --add-data "src/zhongzhuan/web;zhongzhuan/web" `
        --hidden-import aiohttp `
        --hidden-import httpx `
        --hidden-import yaml `
        --hidden-import loguru `
        --clean `
        --noconfirm `
        src/zhongzhuan/__main__.py

    Write-Host "Build complete: dist/zhongzhuan.exe"
    $exe = Get-Item dist/zhongzhuan.exe
    Write-Host "Size: $([math]::Round($exe.Length / 1MB, 1)) MB"

    if ($Release) {
        Write-Host "Running Inno Setup..."
        & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" scripts/installer.iss
        Write-Host "Installer: dist/Zhongzhuan-Setup.exe"
    }
}
finally {
    Pop-Location
}