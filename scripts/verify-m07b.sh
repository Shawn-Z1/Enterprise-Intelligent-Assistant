#!/bin/sh
# 六个本阶段目标方法和四个受影响守卫；真实模型只读取独立库的虚构文件，不重复旧压力矩阵。
set -eu
cd "$(dirname "$0")/.."
mkdir -p verification/M07B
docker compose --profile tools run --rm --no-deps --entrypoint python \
  -v "$(pwd)/backend/tests:/tests:ro" -v "$(pwd)/verification:/verification" \
  -e PYTHONPATH=/app:/tests db-admin -m unittest \
  test_m07b.Runtime \
  test_m07b.FileFlow.test_real_text_and_full_document \
  test_m07b.FileFlow.test_real_multi_csv_heatmap_and_conversion \
  test_m07b.FileFlow.test_queue_wait_and_cancel \
  test_m07b.FileFlow.test_changed_input_unknown_and_attempt_budget \
  test_m07.SqlRules \
  test_m07.M07Flow.test_wait_approve_reject_isolation_and_exact_payload \
  test_answer_review.ReviewContract \
  test_answer_review.ReviewFlow.test_backend_rejects_false_approval -v
