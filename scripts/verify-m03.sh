#!/bin/sh
# 在容器内验证 M03 身份及资源隔离，再回归 M02/M01；输出不进入 Git。
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
mkdir -p verification/M03
docker compose --profile tools run --rm --no-deps --entrypoint python \
  -v "$project_root/backend/tests:/tests:ro" db-admin \
  -m unittest discover -s /tests -p test_short_ids.py -v > verification/M03/short-id-migration.txt 2>&1
docker compose --profile tools run --rm --no-deps --entrypoint python \
  -v "$project_root/backend/tests:/tests:ro" db-admin \
  -m unittest discover -s /tests -p test_m03.py -v > verification/M03/identity-tests.txt 2>&1
docker compose --profile tools run --rm --no-deps --entrypoint python \
  -v "$project_root/backend/tests:/tests:ro" db-admin \
  -m unittest discover -s /tests -p test_m02.py -v > verification/M03/database-regression.txt 2>&1
docker compose run --rm --no-deps -v "$project_root/backend/tests:/tests:ro" api \
  python -m unittest discover -s /tests -p test_foundation.py -v > verification/M03/foundation-regression.txt 2>&1
docker compose ps -a --format json > verification/M03/compose-ps.json
printf '%s\n' 'M03 身份、权限及资源隔离测试与基础回归通过。'
