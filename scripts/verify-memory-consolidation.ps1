# 仅验收个人偏好语义整理，不重复项目检索/文件/沙箱压力矩阵。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$testsPath = Join-Path $projectRoot 'backend/tests'
$reportPath = Join-Path $projectRoot 'verification'
New-Item -ItemType Directory -Path (Join-Path $reportPath 'M11') -Force | Out-Null
Push-Location $projectRoot
try {
    & docker compose --profile tools run --rm --no-deps --user '0:0' --entrypoint python `
        -v "${testsPath}:/tests:ro" -v "${reportPath}:/verification" `
        -e 'PYTHONPATH=/app:/tests' db-admin -m unittest test_memory_consolidation -v
    if ($LASTEXITCODE -ne 0) { throw '个人记忆语义整理验收失败，详情见verification/M11。' }
} finally { Pop-Location }
