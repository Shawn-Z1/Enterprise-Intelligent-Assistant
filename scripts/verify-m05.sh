#!/bin/sh
# 先验证真实原件、审核和权限，再串行回归共享角色相关测试；前端由本地 npm run dev 调试。
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
mkdir -p verification/M05
for suite in test_m05.py test_m04.py test_m03.py test_m02.py test_short_ids.py; do
  docker compose --profile tools run --rm --no-deps --entrypoint python \
    -v "$project_root/backend/tests:/tests:ro" db-admin \
    -m unittest discover -s /tests -p "$suite" -v > "verification/M05/$suite.txt" 2>&1
done
docker compose run --rm --no-deps -v "$project_root/backend/tests:/tests:ro" api \
  python -m unittest discover -s /tests -p test_foundation.py -v > verification/M05/foundation-regression.txt 2>&1
docker compose ps -a --format json > verification/M05/compose-ps.json
printf '%s\n' 'M05 原件、审批、预览、权限和基础回归通过。'
