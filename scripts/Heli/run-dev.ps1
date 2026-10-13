$ErrorActionPreference = "Stop"

$ProjectRoot = "N:\Ai\N.E.K.O"
$DataRoot    = "N:\Ai\N.E.K.O-data"

# Отдельное хранилище source-версии.
$env:NEKO_STORAGE_SELECTED_ROOT = $DataRoot
$env:NEKO_STORAGE_ANCHOR_ROOT   = $DataRoot

# Не подключаем source-сборку к Steamworks.
$env:NEKO_DISABLE_STEAMWORKS = "1"

# Опционально: отключаем telemetry.
$env:DO_NOT_TRACK = "1"

Set-Location $ProjectRoot

Write-Host "=== Heli N.E.K.O Dev ==="
Write-Host "Project: $ProjectRoot"
Write-Host "Data:    $DataRoot"
Write-Host "Steam:   DISABLED"
Write-Host ""

uv run python launcher.py
