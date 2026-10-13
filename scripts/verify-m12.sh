#!/bin/sh
# 只执行M12新增审计定向测试；恢复/浏览器分别运行，不重复历史压力矩阵。
set -eu
cd "$(dirname "$0")/.."
mkdir -p verification/M12
docker compose --profile tools run --rm --no-deps --entrypoint python -v "$PWD/backend/tests:/tests:ro" -v "$PWD/verification:/verification" db-admin -m unittest discover -s /tests -p test_m12.py -v
