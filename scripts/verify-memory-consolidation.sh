#!/bin/sh
# 真实PG与少量真实整理模型，临时库及缓存自动清理。
set -eu
cd "$(dirname "$0")/.."
mkdir -p verification/M11
docker compose --profile tools run --rm --no-deps --user 0:0 --entrypoint python \
  -v "$(pwd)/backend/tests:/tests:ro" -v "$(pwd)/verification:/verification" \
  -e PYTHONPATH=/app:/tests db-admin -m unittest test_memory_consolidation -v
