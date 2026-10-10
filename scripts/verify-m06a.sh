#!/bin/sh
# 使用独立数据、真实模型与真实搜索验收M06A，只回归受影响的网关和RAG守卫。
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
mkdir -p verification/M06A
for suite in test_m06a_unit.py test_m06a_integration.py; do
  docker compose --profile tools run --rm --no-deps --entrypoint python \
    -v "$project_root/backend/tests:/tests:ro" -v "$project_root/verification:/verification" db-admin \
    -m unittest discover -s /tests -p "$suite" -v > "verification/M06A/$suite.txt" 2>&1
done
docker compose --profile tools run --rm --no-deps --entrypoint python \
  -v "$project_root/backend/tests:/tests:ro" -e PYTHONPATH=/app:/tests db-admin \
  -m unittest test_m04.ChatTests.test_six_attempts_fail_terminal_and_audit_redacts \
  test_m04.ChatTests.test_partial_failure_resets_and_usage_counted_once \
  test_m04.ChatTests.test_cancel_queued_and_running_closes_stream \
  test_m06_agent_boundaries.AgentBoundaries.test_deleted_after_retrieval_never_enters_next_model \
  test_m06_agent_boundaries.AgentBoundaries.test_spoofed_tool_permissions_are_not_accepted \
  test_m06_agent_boundaries.AgentBoundaries.test_model_query_rewrite_cannot_remove_original_identifier \
  -v > verification/M06A/affected-regressions.txt 2>&1
printf '%s\n' 'M06A公开搜索、四种实际决策路径、权限与出网边界、必要回归通过。'
