# M11定向验收：真实PG/Redis/Milvus与少量真实模型；独立库/索引/缓存自动清理。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$testsPath = Join-Path $projectRoot 'backend/tests'
$reportPath = Join-Path $projectRoot 'verification'
New-Item -ItemType Directory -Path (Join-Path $reportPath 'M11') -Force | Out-Null
Push-Location $projectRoot
try {
    & docker compose --profile tools run --rm --no-deps --user '0:0' --entrypoint python `
        -v "${testsPath}:/tests:ro" -v "${reportPath}:/verification" `
        -e 'PYTHONPATH=/app:/tests' db-admin -m unittest test_m11 -v
    if ($LASTEXITCODE -ne 0) { throw 'M11验收未通过，请查看上方输出及verification/M11。' }
} finally { Pop-Location }
