[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'

function New-LocalSecret {
    <# 生成本地开发随机凭证；无参数，返回 32 字节随机数的十六进制文本。 #>
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

# 路径固定在项目根，已有环境配置保留，避免重置已初始化卷的密码。
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPath = Join-Path $projectRoot '.env'
if (Test-Path -LiteralPath $environmentPath) {
    Write-Host '.env 已存在，保留当前配置。'
    exit 0
}
$content = Get-Content -LiteralPath (Join-Path $projectRoot '.env.example') -Raw
$content = [regex]::Replace($content, 'replace-with-random-password', { New-LocalSecret })
[System.IO.File]::WriteAllText($environmentPath, $content, [System.Text.UTF8Encoding]::new($false))
Write-Host '已生成 .env 随机本地凭证（不回显）。'
