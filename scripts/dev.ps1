# Development mode script
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$ROOT = Split-Path -Parent $ROOT
Push-Location $ROOT
try {
    Write-Host "Starting zhongzhuan in dev mode..."
    python -m zhongzhuan
} finally {
    Pop-Location
}