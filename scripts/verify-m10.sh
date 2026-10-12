#!/bin/sh
# 真实PG/Redis和一条真实模型压缩路径；独立库/缓存自动清理，不重跑扩容/文件格式矩阵。
set -eu
cd "$(dirname "$0")/.."
mkdir -p verification/M10
docker compose --profile tools run --rm --no-deps --user 0:0 --entrypoint python \
  -v "$(pwd)/backend/tests:/tests:ro" -v "$(pwd)/verification:/verification" \
  -e PYTHONPATH=/app:/tests db-admin -m unittest test_m10 -v
