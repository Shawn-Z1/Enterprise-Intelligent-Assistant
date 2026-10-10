#!/bin/sh
# M07仅验自身门槛及两个受影响守卫；使用独立数据库和外置虚构种子，真实模型用例单列。
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
mkdir -p verification/M07
docker compose --profile tools run --rm --no-deps --entrypoint python \
  -v "$project_root/backend/tests:/tests:ro" -v "$project_root/verification:/verification" -e PYTHONPATH=/app:/tests db-admin \
  -m unittest test_m07.SqlRules test_m07.M07Flow -v > verification/M07/boundaries.txt 2>&1
docker compose --profile tools run --rm --no-deps --entrypoint python \
  -v "$project_root/backend/tests:/tests:ro" -v "$project_root/verification:/verification" -e PYTHONPATH=/app:/tests db-admin \
  -m unittest test_m07.RealM07 -v > verification/M07/real-test.txt 2>&1
docker compose --profile tools run --rm --no-deps --entrypoint python \
  -v "$project_root/backend/tests:/tests:ro" -v "$project_root/verification:/verification" -e PYTHONPATH=/app:/tests db-admin \
  -m unittest test_answer_review.ReviewFlow.test_missing_tool_repairs_without_publishing_draft \
  test_m06_agent_boundaries.AgentBoundaries.test_deleted_after_retrieval_never_enters_next_model -v > verification/M07/affected.txt 2>&1
