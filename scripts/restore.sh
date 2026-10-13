#!/bin/sh
# 只恢复到全新目标，现有应用不自动切换；凭证留在权限受限的配置文件内。
set -eu
umask 077
cd "$(dirname "$0")/.."
: "${1:?需要备份目录}" "${2:?需要新数据库名}" "${3:?需要新Bucket名}"
MAINTENANCE_DIR=$(cd "$1" && pwd)
export MAINTENANCE_DIR
docker compose --profile tools run --rm --no-deps maintenance restore --database "$2" --bucket "$3"
printf '隔离恢复完成；切换配置在备份目录restore.compose.json，现有应用未切换。\n'
