#!/bin/sh
# 在维护窗口停止本项目写入服务，备份完成或失败后恢复原先运行的服务。
set -eu
umask 077
cd "$(dirname "$0")/.."
directory=${1:-"backups/$(date +%Y%m%d-%H%M%S)"}
mkdir -p "$directory"
MAINTENANCE_DIR=$(cd "$directory" && pwd)
export MAINTENANCE_DIR
services=$(docker compose ps --status running --services)
running=$(printf '%s\n' "$services" | awk '/^(api|worker|sandbox-runner)$/ {print}' | sort -u)
resume() {
    # 服务名称来自固定允许清单，不从用户输入构建命令。
    if [ -n "$running" ]; then docker compose up -d --wait $running; fi
}
trap resume EXIT
if [ -n "$running" ]; then
    docker compose stop --timeout 110 worker
    docker compose stop --timeout 110 sandbox-runner
    docker compose stop --timeout 110 api
fi
docker compose --profile tools run --rm --no-deps maintenance backup
printf '备份完成：%s\n' "$MAINTENANCE_DIR"
