$ErrorActionPreference = "Stop"

$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ConfigRoot = Join-Path $env:APPDATA "knowcoder-mcp"
$ConfigPath = Join-Path $ConfigRoot "config.py"
$PackageIndex = if ($env:KNOWCODER_PACKAGE_INDEX) { $env:KNOWCODER_PACKAGE_INDEX } else { "https://pypi.org/simple" }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install it from https://docs.astral.sh/uv/"
}

$env:UV_DEFAULT_INDEX = $PackageIndex
uv tool install --force --python 3.12 $SourceRoot
$ToolRoot = Join-Path (uv tool dir) "knowcoder-mcp"
$ToolScripts = Join-Path $ToolRoot "Scripts"
& (Join-Path $ToolScripts "playwright.exe") install chromium
& (Join-Path $ToolScripts "crawl4ai-doctor.exe")
New-Item -ItemType Directory -Force -Path $ConfigRoot | Out-Null
if (-not (Test-Path $ConfigPath)) {
    Copy-Item (Join-Path $SourceRoot "config.py.example") $ConfigPath
}
& (Join-Path $ToolScripts "knowcoder-mcp.exe") doctor --local

Write-Host "Installed knowcoder-mcp."
Write-Host "Edit $ConfigPath, then run: knowcoder-mcp doctor"
