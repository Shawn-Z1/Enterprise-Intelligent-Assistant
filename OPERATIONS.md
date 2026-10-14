# 部署与运维

首次安装见 [README](README.md#快速启动)。以下命令在项目根目录执行；操作已有数据前先备份。

## 部署与健康检查

| 环境 | 后端容器＋Vite | 全容器 |
| --- | --- | --- |
| PowerShell | `./scripts/deploy.ps1 -LocalFrontend` | `./scripts/deploy.ps1` |
| Linux | `sh scripts/deploy.sh --local-frontend` | `sh scripts/deploy.sh` |

本地前端另在 `frontend` 运行 `pnpm install --frozen-lockfile` 和 `npm run dev`，无需宿主 Python。PowerShell 5.1 若出现中文编码问题，使用 PowerShell 7；不要忽略失败继续启动。

```sh
docker compose config --quiet
docker compose ps
docker compose logs --tail=80 api worker sandbox-runner
docker compose logs --tail=80 models-init retrieval-models
```

`config --quiet` 仅验证配置。完整 `docker compose config` 会展开凭证，不要公开输出；日志和审计也可能含用户正文。

| 入口 | 默认地址 | 说明 |
| --- | --- | --- |
| 网页 | http://127.0.0.1:8080 | Vite 或生产 Nginx，二选一 |
| API 就绪 | http://127.0.0.1:8000/api/health/ready | 检查依赖，不调用付费模型 |
| MinIO 管理 | http://127.0.0.1:9001 | 使用 MinIO root 凭证 |
| PostgreSQL | 127.0.0.1:5432 | 管理工具本机连接 |

端口由 `DEV_PORT`、`WEB_PORT`、`API_HOST_PORT`、`MINIO_CONSOLE_PORT`、`POSTGRES_HOST_PORT` 配置。首次下载需要访问镜像源、包源、GitHub 和 Hugging Face。`MODEL_DOWNLOAD_ENDPOINT` 可改为部署方信任、可提供相同固定文件的 HTTPS 端点，仍校验 SHA-256。

## 初始化与升级

新安装加载 `database/seeds_release/`：一个 `admin` 登录身份，其余职员仅为无登录身份的虚构业务记录；知识库、对话、附件和个人记忆为空。

`schema_migrations` 校验结构文件哈希，`seed_receipts` 记录种子。已有 `short_ids/` 回执时沿用旧种子，不删除账号或重置权限；旧种子目录只为兼容，不是开发数据库导出。修改 `.env` 初始密码不会修改已创建账号，后续从个人中心改密。

更新代码前备份，然后重跑对应部署脚本。历史迁移和已应用种子不能直接改动；升级通过追加版本完成。日常 CSS/组件调整复用 Vite 热更新。

```sh
docker compose stop
docker compose up -d --wait
```

这是已初始化系统的停止/恢复，不删除数据。生产恢复加 `--profile production`；Vite 在其终端 Ctrl+C 停止。不要把 `docker compose down -v` 当日常停机，它会删除数据卷。

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
