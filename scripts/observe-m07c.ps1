[CmdletBinding()]
param(
    [ValidateRange(1,10)][int]$Tasks=10,
    [ValidateRange(30,1200)][int]$TimeoutSeconds=600,
    [ValidateRange(0,10)][int]$TaskDelaySeconds=2
)
$ErrorActionPreference='Stop'

# 从任意目录启动都定位到本项目；Docker内执行Python，宿主不需要安装测试依赖。
$taskRoot=Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $taskRoot
try {
    [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding=[Console]::OutputEncoding
    $taskServices=@(& docker compose ps --status running --services)
    if ($LASTEXITCODE -ne 0) { throw '无法连接Docker或读取Compose配置' }
    foreach($taskService in @('api','worker','sandbox-runner')) {
        if ($taskServices -notcontains $taskService) { throw "请先启动主服务：docker compose up -d --wait；缺少 $taskService" }
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $taskRoot 'verification\M07C') | Out-Null
    Write-Host "开始实时观测：$Tasks 条真实模型对话；终端即时输出队列、Worker、Runner和任务容器状态。"
    Write-Host '运行期间可用Ctrl+C停止；日志保存在verification/M07C/live-console-*.log。'
    & docker compose --profile tools run --rm --no-deps --user 0:0 --entrypoint python `
        -v /var/run/docker.sock:/var/run/docker.sock `
        -v "${taskRoot}/backend/tests:/tests:ro" -v "${taskRoot}/verification:/verification" `
        -e PYTHONPATH=/app:/tests -e PYTHONUNBUFFERED=1 db-admin `
        -u /tests/observe_m07c.py --tasks $Tasks --timeout $TimeoutSeconds --delay $TaskDelaySeconds
    if ($LASTEXITCODE -eq 130) { Write-Host '用户中止了本次观测，测试驱动已触发清理。'; exit 130 }
    if ($LASTEXITCODE -ne 0) { throw "并发观测未通过，退出码=$LASTEXITCODE；请查看上面的最后队列和错误信息" }
}
finally { Pop-Location }
