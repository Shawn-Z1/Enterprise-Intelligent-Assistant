#!/bin/sh
# Linux首次部署/升级的显式入口；密钥由初始化配置，不在终端打印.env。
set -eu
cd "$(dirname "$0")/.."
test -f .env || { printf '请先执行sh scripts/init-env.sh，并填写模型API配置\n'; exit 1; }
case "${1:-}" in
  ''|--local-frontend) ;;
  *) printf '用法：sh scripts/deploy.sh [--local-frontend]\n'; exit 1 ;;
esac
docker compose config --quiet
docker compose build api
docker compose --profile tools build maintenance sandbox-image
docker compose up -d --wait postgres redis minio minio-init models-init retrieval-models etcd milvus
docker compose --profile tools run --rm db-admin migrate
docker compose --profile tools run --rm db-admin seed
if [ "${1:-}" = '--local-frontend' ]; then
  docker compose up -d --build --wait
  printf '后端已就绪；请在frontend目录执行pnpm install --frozen-lockfile和npm run dev。\n'
else
  docker compose --profile production up -d --build --wait
fi
