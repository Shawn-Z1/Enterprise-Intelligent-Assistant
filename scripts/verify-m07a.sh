#!/bin/sh
# 仅运行M07A的目标组和一个受影响SQL守卫；真实模型使用隔离库虚构文件。
set -eu
cd "$(dirname "$0")/.."
mkdir -p verification/M07A
docker compose --profile tools run --rm --no-deps --entrypoint python \
  -v "$(pwd)/backend/tests:/tests:ro" -v "$(pwd)/verification:/verification" \
  -e PYTHONPATH=/app:/tests db-admin -m unittest \
  test_m07a.Runtime test_m07a.FileFlow.test_confirmation_reject_and_owner_boundary \
  test_m07a.FileFlow.test_real_csv_python_and_xlsx_sql \
  test_m07a.FileFlow.test_three_execute_budget_and_unknown_replay \
  test_m07a.FileFlow.test_changed_input_and_account_disable \
  test_m07.SqlRules -v
