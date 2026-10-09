#!/bin/sh
# 串行验证共享数据库角色，避免不同测试库并行 ALTER ROLE 发生目录写入冲突。
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
mkdir -p verification/M04
for suite in test_m04.py test_m02.py test_m03.py test_short_ids.py; do
  docker compose --profile tools run --rm --no-deps --entrypoint python \
    -v "$project_root/backend/tests:/tests:ro" db-admin \
    -m unittest discover -s /tests -p "$suite" -v > "verification/M04/$suite.txt" 2>&1
done
docker compose --profile tools run --rm --no-deps --entrypoint python -e RUN_REAL_MODELS=1 \
  -v "$project_root/backend/tests:/tests:ro" db-admin \
  -m unittest discover -s /tests -p test_m04_real.py -v > verification/M04/real-model-tests.txt 2>&1
docker compose run --rm --no-deps -v "$project_root/backend/tests:/tests:ro" api \
  python -m unittest discover -s /tests -p test_foundation.py -v > verification/M04/foundation-regression.txt 2>&1
docker compose ps -a --format json > verification/M04/compose-ps.json
printf '%s\n' 'M04 真实主备模型、身份、数据库与基础回归通过。'
