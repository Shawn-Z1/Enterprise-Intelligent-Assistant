[CmdletBinding()]
param([switch]$LocalFrontend)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

function Invoke-Compose {
    <# 接收 Compose 参数数组，执行部署步骤；子进程失败即终止，避免继续启动半初始化环境。 #>
    param([string[]]$ComposeArguments)
    & docker compose @ComposeArguments
    if ($LASTEXITCODE -ne 0) { throw '部署步骤失败，请检查上方错误后重试；已有数据不会被清除。' }
}

if (-not (Test-Path -LiteralPath '.env')) { throw '请先运行 scripts/init-env.ps1 并填写模型 API 配置。' }
Invoke-Compose @('config', '--quiet')
Invoke-Compose @('build', 'api')
Invoke-Compose @('--profile', 'tools', 'build', 'maintenance', 'sandbox-image')
Invoke-Compose @('up', '-d', '--wait', 'postgres', 'redis', 'minio', 'minio-init', 'models-init', 'retrieval-models', 'etcd', 'milvus')
Invoke-Compose @('--profile', 'tools', 'run', '--rm', 'db-admin', 'migrate')
Invoke-Compose @('--profile', 'tools', 'run', '--rm', 'db-admin', 'seed')
if ($LocalFrontend) {
    Invoke-Compose @('up', '-d', '--build', '--wait')
    Write-Host '后端已就绪；请在 frontend 目录执行 pnpm install --frozen-lockfile 和 npm run dev。'
} else {
    Invoke-Compose @('--profile', 'production', 'up', '-d', '--build', '--wait')
}
