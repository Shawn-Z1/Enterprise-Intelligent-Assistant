[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force verification/M12 | Out-Null
& docker compose --profile tools run --rm --no-deps --entrypoint python -v "${PWD}/backend/tests:/tests:ro" -v "${PWD}/verification:/verification" db-admin -m unittest discover -s /tests -p test_m12.py -v
if($LASTEXITCODE -ne 0){throw '审计定向验收失败'}
