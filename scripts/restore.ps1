[CmdletBinding()]
param([Parameter(Mandatory)][string]$Directory,[Parameter(Mandatory)][string]$Database,[Parameter(Mandatory)][string]$Bucket)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$previous=$env:MAINTENANCE_DIR
$env:MAINTENANCE_DIR=(Resolve-Path -LiteralPath $Directory).Path
try {
    # 恢复工具自身拒绝已有库/Bucket；不停止或切换现有生产应用。
    & docker compose --profile tools run --rm --no-deps maintenance restore --database $Database --bucket $Bucket
    if($LASTEXITCODE -ne 0){throw '恢复未通过；保留新目标以便检查，不自动删除或切换应用'}
    Write-Host '隔离恢复完成，切换配置在备份目录restore.compose.json；当前应用尚未切换。'
} finally {$env:MAINTENANCE_DIR=$previous}
