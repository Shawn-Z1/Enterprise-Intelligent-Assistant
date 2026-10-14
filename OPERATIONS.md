# 部署与运维

首次安装见 [README](README.md#快速启动)。以下命令在项目根目录执行；操作已有数据前先备份。

## 部署与健康检查

| 环境 | 全容器部署 |
| --- | --- |
| PowerShell | `./scripts/deploy.ps1` |
| Linux | `sh scripts/deploy.sh` |

部署脚本自动启用 `production` profile，前端在镜像中构建并由 Nginx 提供网页，宿主无需 Python 或 Node.js。PowerShell 5.1 若出现中文编码问题，使用 PowerShell 7；不要忽略失败继续启动。

```sh
docker compose config --quiet
docker compose --profile production ps
docker compose logs --tail=80 api worker sandbox-runner
docker compose logs --tail=80 models-init retrieval-models
```

`config --quiet` 仅验证配置。完整 `docker compose config` 会展开凭证，不要公开输出；日志和审计也可能含用户正文。

| 入口 | 默认地址 | 说明 |
| --- | --- | --- |
| 网页 | http://127.0.0.1:8080 | Nginx 网页入口 |
| API 就绪 | http://127.0.0.1:8000/api/health/ready | 检查依赖，不调用付费模型 |
| MinIO 管理 | http://127.0.0.1:9001 | 使用 MinIO root 凭证 |
| PostgreSQL | `127.0.0.1:5432` | PostgreSQL 协议，用 Navicat、psql 等数据库客户端连接 |

网页、API、MinIO 管理台提供 HTTP 服务；PostgreSQL 使用专用数据库协议，地址不是网页 URL，不加 `http://`。数据库客户端分别填写主机、端口、数据库名、用户名和密码；支持连接串的客户端也可使用 `postgresql://`，不要把带密码的连接串公开。

端口由 `WEB_PORT`、`API_HOST_PORT`、`MINIO_CONSOLE_PORT`、`POSTGRES_HOST_PORT` 配置。首次下载需要访问镜像源、包源、GitHub 和 Hugging Face。`MODEL_DOWNLOAD_ENDPOINT` 可改为部署方信任、可提供相同固定文件的 HTTPS 端点，仍校验 SHA-256。

## 初始化与升级

新安装加载 `database/seeds_release/`：一个 `admin` 登录身份，其余职员仅为无登录身份的虚构业务记录；知识库、对话、附件和个人记忆为空。

`schema_migrations` 校验结构文件哈希，`seed_receipts` 记录种子。已有 `short_ids/` 回执时沿用旧种子，不删除账号或重置权限；旧种子目录只为兼容，不是开发数据库导出。修改 `.env` 初始密码不会修改已创建账号，后续从个人中心改密。

更新代码前备份，然后重跑对应部署脚本。历史迁移和已应用种子不能直接改动；升级通过追加版本完成。

```sh
docker compose stop
docker compose --profile production up -d --wait
```

这是已初始化系统的停止/恢复，不删除数据。不要把 `docker compose down -v` 当日常停机，它会删除数据卷。

## 扩容配置

修改 `.env` 后执行：

```sh
docker compose --profile production up -d --wait
```

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `WORKER_REPLICAS` / `WORKER_CONCURRENCY` | 2 / 2 | Worker 数量 / 每实例任务片段并发 |
| `SANDBOX_RUNNER_REPLICAS` / `SANDBOX_RUNNER_CONCURRENCY` | 2 / 1 | Runner 数量 / 每实例 Guest 容量 |
| `SANDBOX_GLOBAL_CONCURRENCY` / `SANDBOX_USER_CONCURRENCY` | 2 / 1 | 全局 / 每用户沙箱执行上限 |
| `SANDBOX_QUEUE_LIMIT` / `SANDBOX_WAIT_SECONDS` | 50 / 300 | 队列容量 / 最长排队秒数 |
| `CONTEXT_COMPRESS_TRIGGER_RATIO` / `CONTEXT_KEEP_TURNS` | 0.75 / 4 | 压缩触发比例 / 保留轮次 |

多个 Runner 不要设置同一个 `SANDBOX_RUNNER_ID`，默认使用容器自身身份。只增加 Runner 数量但不提高全局额度，不会提高实际沙箱并发上限。当前多副本限同一 Docker 引擎，扩容仍受宿主资源、数据库连接、供应商限流和全局额度约束。

上下文压缩比例和保留轮次影响模型输入预算，不直接增加执行容量；修改它们时应同时核对模型窗口与输出预算。

## 本地开发（维护者）

维护者日常前端开发仍可使用 Vite 热更新，不改变公开部署的全容器入口。先用 `./scripts/deploy.ps1 -LocalFrontend`（Windows）或 `sh scripts/deploy.sh --local-frontend`（Linux）启动后端，再在 `frontend` 执行 `pnpm install --frozen-lockfile` 和 `npm run dev`。此方式需要本机 Node.js 与 pnpm，开发端口由 `DEV_PORT` 控制。

Vite 与容器网页默认都占用 8080，不能同时占用同一端口；切换到本地开发前可用 `docker compose --profile production stop web` 停止网页容器。样式修改直接热更新，不反复重建 `web` 镜像；Vite 在其终端用 Ctrl+C 停止。

## 公网访问

默认只绑定本机。使用 HTTPS 反向代理转发到生产 Web 的 `127.0.0.1:8080`，保留原始 Host，关闭事件流缓冲并支持长连接。HTTPS 下设置 `SESSION_COOKIE_SECURE=true`，确保后端取得可信、正确的原始协议和 Host；按实际代理链验证 Cookie、登录、上传和 SSE 后再开放。

不要直接公开存储端口、MinIO 控制台或 Runner。Runner 持有 Docker socket，建议使用专用宿主/引擎；当前不启用 gVisor。域名、证书、防火墙及可信代理链由部署方配置，Compose 不自动申请 TLS。

## 备份

先让进行中的任务结束。脚本停止本项目 Worker、Runner、API，导出 PostgreSQL 17 自定义档案及整个应用 Bucket，保存表数量与对象 SHA；成功或失败后尝试恢复原服务。Redis、Milvus 不做物理备份，恢复时重建。

```sh
docker compose build api
docker compose --profile tools build maintenance
sh scripts/backup.sh backups/manual-backup
```

Windows：`./scripts/backup.ps1 -Directory backups/manual-backup`。目录必须为空，不覆盖旧备份。有 `manifest.json` 且以下校验通过才是完整备份：

```sh
MAINTENANCE_DIR=./backups/manual-backup docker compose --profile tools run --rm --no-deps maintenance validate
```

PowerShell 先设置 `$env:MAINTENANCE_DIR='./backups/manual-backup'`，再运行相同的 `docker compose ... maintenance validate`。

备份含正文、密码哈希和审计，需受控保存、按需加密及异地备份，不得上传 Git。SHA 校验完整性，不证明外部备份可信。

## 恢复到全新目标

```sh
sh scripts/restore.sh backups/manual-backup zhixing_restored application-restored
```

Windows：

```powershell
./scripts/restore.ps1 -Directory backups/manual-backup -Database zhixing_restored -Bucket application-restored
```

已有数据库/Bucket 拒绝覆盖。先校验 SHA，再建新目标、恢复数据、核验数量和对象内容、重建独立索引。旧登录、SQL 批准和未完成任务不会自动恢复为可执行；历史保留。索引重建需原检索模型空间匹配。

只有成功生成 `restore-receipt.json` 才能切换。`restore.compose.json` 含新凭证，不得提交。失败保留新目标供排查，不启动半恢复环境。

在新目标验证检索、预览与下载后，维护窗口内人工切换：

```sh
docker compose stop worker
docker compose stop sandbox-runner
docker compose stop api
docker compose -f compose.yaml -f backups/manual-backup/restore.compose.json up -d --wait api worker sandbox-runner
```

后续迁移/备份同步 `.env` 的数据库、Bucket、应用凭证、集合前缀，或显式传入相同目标。新执行卷不代表原宿主旧 Guest 已清理，仍需原 Runner 或管理员确认。

## 排查要点

- **容器健康但模型失败**：健康接口不验证付费 API。核对主/审核模型 URL、Key、型号和限额，并查该轮审计。
- **没有知识或记忆**：新库本就为空。知识要上传、处理并审核；长期记忆异步提取，无稳定信号可以不写入。
- **网页正文读取失败**：搜索摘要与网页抓取是不同步骤，网络、DNS 和网站拒绝都可能影响正文；不能冒充已读全文。
- **沙箱排队或不可用**：核对 Runner、Guest 镜像、并发额度、队列容量和等待时限，不改为在宿主运行代码。
- **上下文超限**：核对实际模型窗口、输出预算和工具结果。20 MiB 文件上限不是模型可接收全文的保证。
- **成员业务行不可读**：工具资格与员工行权限分开，注册用户不自动关联虚构员工；实际部署需可信身份映射。
- **下载/构建失败**：检查容器内网络和各下载源；宿主联网不代表容器联网。保留数据，修复后重跑部署入口。
