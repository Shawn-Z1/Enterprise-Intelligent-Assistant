#!/bin/sh
# 运行真实数据库验收和持久化重启检查，产物仅保存在本机忽略目录。
set -eu
# 项目根固定来自脚本路径，所有测试挂载和输出均限制在当前项目。
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
mkdir -p verification/M02

docker compose --profile tools run --rm --no-deps --entrypoint python \
  -v "$project_root/backend/tests:/tests:ro" db-admin \
  -m unittest discover -s /tests -p test_m02.py -v > verification/M02/database-tests.txt 2>&1
docker compose run --rm --no-deps -e PYTHONPATH=/app -v "$project_root/backend/tests:/tests:ro" api \
  python -m unittest discover -s /tests -p test_foundation.py -v > verification/M02/foundation-tests.txt 2>&1
docker compose run --rm --no-deps -e PYTHONPATH=/app -e VERIFICATION_API_URL=http://api:8000 -v "$project_root/backend/tests:/tests:ro" api \
  python /tests/verify_m02_runtime.py prepare > verification/M02/persistence-before.json

# 重启 PG 并重建应用容器，保留命名卷，随后读取同一永久审计标记。
docker compose restart postgres
docker compose up -d --force-recreate api worker --wait --wait-timeout 180
docker compose run --rm --no-deps -e PYTHONPATH=/app -e VERIFICATION_API_URL=http://api:8000 -v "$project_root/backend/tests:/tests:ro" \
  -v "$project_root/verification:/verification:ro" api \
  python /tests/verify_m02_runtime.py check --state-file /verification/M02/persistence-before.json \
  > verification/M02/persistence-after.json
docker compose ps -a --format json > verification/M02/compose-ps.json
printf '%s\n' 'M02 真实数据库、接口和重启验收通过，证据位于 verification/M02/'
