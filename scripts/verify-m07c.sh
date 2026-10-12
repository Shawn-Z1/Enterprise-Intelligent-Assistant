#!/bin/sh
# 两个扩容目标与受影响确认/审核守卫；Docker socket只用于可信验收驱动，任务客体不能访问。
set -eu
cd "$(dirname "$0")/.."
mkdir -p verification/M07C
docker compose --profile tools run --rm --no-deps --user 0:0 --entrypoint python \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd)/backend/tests:/tests:ro" -v "$(pwd)/verification:/verification" \
  -e PYTHONPATH=/app:/tests db-admin -m unittest \
  test_m07c.Scaling.test_ten_jobs_single_and_multi_runner \
  test_m07c.Scaling.test_lifecycle_and_worker_handoff \
  test_m07c.RecoveryFence.test_expired_confirmed_run_claimed_once \
  test_m07b.FileFlow.test_changed_input_unknown_and_attempt_budget \
  test_m07.M07Flow.test_wait_approve_reject_isolation_and_exact_payload \
  test_answer_review.ReviewFlow.test_backend_rejects_false_approval -v
