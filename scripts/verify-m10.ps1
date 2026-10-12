# Windows定向验收入口；用户代码仍只在Docker沙箱，测试使用独立PG库和Redis命名空间。
$ErrorActionPreference = 'Stop'
# 只从脚本所在项目定位只读测试源和忽略的报告目录，不拼接删除或凭证命令。
$projectRoot = Split-Path -Parent $PSScriptRoot
$testsPath = Join-Path $projectRoot 'backend/tests'
$reportPath = Join-Path $projectRoot 'verification'
New-Item -ItemType Directory -Path (Join-Path $reportPath 'M10') -Force | Out-Null
Push-Location $projectRoot
try {
    & docker compose --profile tools run --rm --no-deps --user '0:0' --entrypoint python `
        -v "${testsPath}:/tests:ro" -v "${reportPath}:/verification" `
        -e 'PYTHONPATH=/app:/tests' db-admin -m unittest test_m10 -v
    if ($LASTEXITCODE -ne 0) { throw 'M10验收未通过，请查看上方输出及verification/M10报告。' }
} finally {
    Pop-Location
}
