param(
    [string]$StorageRoot = "E:\AetherCloudData",
    [string]$Host = "0.0.0.0",
    [int]$Port = 5000,
    [string]$PublicUrl = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $projectRoot ".venv"

if (-not (Test-Path $venvPath)) {
    python -m venv $venvPath
}

$pythonExe = Join-Path $venvPath "Scripts\python.exe"
$pipExe = Join-Path $venvPath "Scripts\pip.exe"

& $pipExe install -r (Join-Path $projectRoot "requirements.txt")

New-Item -ItemType Directory -Force -Path $StorageRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $StorageRoot "storage") | Out-Null

$env:AETHER_DB_PATH = Join-Path $StorageRoot "users.db"
$env:AETHER_STORAGE_DIR = Join-Path $StorageRoot "storage"
$env:AETHER_HOST = $Host
$env:AETHER_PORT = "$Port"
$env:AETHER_DEBUG = "0"

if ($PublicUrl) {
    $env:AETHER_TUNA_URL = $PublicUrl
}

& $pythonExe (Join-Path $projectRoot "AetherCloud.py")
