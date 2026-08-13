$ErrorActionPreference = "Stop"

$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ConfigRoot = Join-Path $env:APPDATA "knowcoder-mcp"
$ConfigPath = Join-Path $ConfigRoot "config.py"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install it from https://docs.astral.sh/uv/"
}

uv tool install --force --python 3.12 $SourceRoot
New-Item -ItemType Directory -Force -Path $ConfigRoot | Out-Null
if (-not (Test-Path $ConfigPath)) {
    Copy-Item (Join-Path $SourceRoot "config.py.example") $ConfigPath
}

Write-Host "Installed knowcoder-mcp."
Write-Host "Edit $ConfigPath, then run: knowcoder-mcp doctor"
