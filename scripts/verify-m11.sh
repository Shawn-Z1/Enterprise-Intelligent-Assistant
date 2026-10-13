#!/bin/sh
# 有界定向验收，不重复已通过的扩容/文件格式压力矩阵。
set -eu
cd "$(dirname "$0")/.."
mkdir -p verification/M11
docker compose --profile tools run --rm --no-deps --user 0:0 --entrypoint python \
  -v "$(pwd)/backend/tests:/tests:ro" -v "$(pwd)/verification:/verification" \
  -e PYTHONPATH=/app:/tests db-admin -m unittest test_m11 -v
