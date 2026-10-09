[CmdletBinding()]
param([string]$BaseUrl = 'http://127.0.0.1:8080', [switch]$IncludeFaults)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$outputDirectory = Join-Path $projectRoot 'verification/M01'
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
# 所有验收结果均来自真实主入口或 Docker，失败立即中止并保留已完成证据。
$results = [System.Collections.Generic.List[object]]::new()

function Save-Evidence {
    <# 将当前检查结果落盘；无参数，无返回值，不输出环境密钥。 #>
    $results | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $outputDirectory 'checks.json') -Encoding utf8
}

function Assert-Check {
    <# 参数为检查名称、条件和证据；记录结果，失败则抛出错误。 #>
    param([string]$Name, [bool]$Passed, [object]$Evidence)
    $results.Add([pscustomobject]@{ name=$Name; passed=$Passed; timestamp=[DateTimeOffset]::Now.ToOffset([TimeSpan]::FromHours(8)).ToString('o'); evidence=$Evidence })
    Save-Evidence
    if (-not $Passed) { throw "验收失败：$Name" }
    Write-Host "通过：$Name"
}

function Get-Api {
    <# 参数为 API 路径；通过开发 Vite 或生产 web 同源入口请求，返回状态、追踪头和 JSON。 #>
    param([string]$Path)
    $response = Invoke-WebRequest -NoProxy -Uri "$BaseUrl$Path" -SkipHttpErrorCheck -TimeoutSec 15
    return [pscustomobject]@{ status=[int]$response.StatusCode; trace=$response.Headers['X-Trace-ID']; body=($response.Content | ConvertFrom-Json) }
}

function Wait-Ready {
    <# 无参数轮询真实就绪接口；最多 120 秒，返回就绪响应，否则失败。 #>
    $deadline = [DateTimeOffset]::Now.ToOffset([TimeSpan]::FromHours(8)).AddSeconds(120)
    do {
        try { $response = Get-Api '/api/health/ready'; if ($response.status -eq 200) { return $response } } catch { }
        Start-Sleep -Seconds 2
    } while ([DateTimeOffset]::Now.ToOffset([TimeSpan]::FromHours(8)) -lt $deadline)
    throw '依赖恢复后未能在 120 秒内就绪'
}

Push-Location $projectRoot
try {
    $live = Get-Api '/api/health/live'
    Assert-Check '主入口同源代理与 API 存活' ($live.status -eq 200 -and $live.body.status -eq 'alive') $live
    $ready = Wait-Ready
    Assert-Check '四项真实依赖就绪' ($ready.body.status -eq 'ready' -and @($ready.body.dependencies.psobject.Properties).Count -eq 4) $ready
    foreach ($dependency in $ready.body.dependencies.psobject.Properties) {
        Assert-Check "$($dependency.Name) 使用 Compose 服务名" ($dependency.Value.target -match "^$($dependency.Name):") $dependency.Value
    }
    $events = Get-Api '/api/events'
    Assert-Check '追踪事件及存储边界真实' ($events.status -eq 200 -and -not $events.body.durable -and @($events.body.events | Where-Object { $_.trace_id -eq $ready.body.trace_id }).Count -ge 1) $events
    $errorEnvelope = Get-Api '/api/events?limit=101'
    Assert-Check '统一错误格式及参数边界' ($errorEnvelope.status -eq 422 -and $errorEnvelope.body.error.code -eq 'VALIDATION_ERROR' -and $errorEnvelope.body.trace_id) $errorEnvelope
    if ($IncludeFaults) {
        foreach ($service in @('postgres', 'redis', 'milvus', 'minio')) {
            try {
                docker compose stop $service | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "停止 $service 失败" }
                $failed = Get-Api '/api/health/ready'
                Assert-Check "停止 $service 后明确 503" ($failed.status -eq 503 -and $failed.body.status -eq 'not_ready' -and $failed.body.dependencies.$service.status -eq 'unavailable') $failed
                $stillLive = Get-Api '/api/health/live'
                Assert-Check "$service 故障时 API 仍存活" ($stillLive.status -eq 200) $stillLive
            } finally {
                docker compose start $service | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "恢复 $service 失败，请手动 docker compose start $service" }
            }
            $restored = Wait-Ready
            Assert-Check "$service 恢复后重新就绪" ($restored.status -eq 200) $restored
        }
    }
    docker compose ps --format json | Set-Content -LiteralPath (Join-Path $outputDirectory 'compose-ps.json') -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw '容器状态证据采集失败' }
    Write-Host 'M01 接口验收完成，结果见 verification/M01/checks.json'
} finally { Save-Evidence; Pop-Location }
