[CmdletBinding()]
param([string]$Directory=('backups/'+(Get-Date -Format 'yyyyMMdd-HHmmss')))
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
# 仅停止本项目写入服务，并在finally恢复原先正在运行的服务。
$running=@(& docker compose ps --status running --services | Where-Object {$_ -in @('api','worker','sandbox-runner')} | Sort-Object -Unique)
if($LASTEXITCODE -ne 0){throw '无法读取当前Compose服务'}
$previous=$env:MAINTENANCE_DIR
$env:MAINTENANCE_DIR=if([IO.Path]::IsPathRooted($Directory)){[IO.Path]::GetFullPath($Directory)}else{[IO.Path]::GetFullPath((Join-Path $root $Directory))}
New-Item -ItemType Directory -Force $env:MAINTENANCE_DIR | Out-Null
try {
    if($running.Count){
        foreach($service in @('worker','sandbox-runner','api')){
            & docker compose stop --timeout 110 $service
            if($LASTEXITCODE -ne 0){throw "停止写入服务失败：$service"}
        }
    }
    & docker compose --profile tools run --rm --no-deps maintenance backup
    if($LASTEXITCODE -ne 0){throw '备份未完成，请检查目录，不能将不完整备份用于恢复'}
    Write-Host "备份完成：$env:MAINTENANCE_DIR"
} finally {
    if($running.Count){& docker compose up -d --wait @running;if($LASTEXITCODE -ne 0){Write-Warning '原服务恢复失败，请执行docker compose up -d --wait'}}
    $env:MAINTENANCE_DIR=$previous
}
