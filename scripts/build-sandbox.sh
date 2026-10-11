#!/bin/sh
# 构建固定普通Docker任务镜像，不安装任何宿主运行时、不更改Docker引擎配置。
set -eu
cd "$(dirname "$0")/.."
docker compose --profile tools build sandbox-image sandbox-runner
