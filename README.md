# 知行 · 企业智能助理

面向企业员工的浏览器工作台，用于集中处理企业知识、业务数据和工作记录。当前版本已完成界面与容器运行基础，业务能力将逐步接入。

## 当前功能

- **工作台界面**：提供对话工作区、历史会话区域、知识库管理、自动化任务流与个人中心的导航入口；支持桌面与小屏布局。
- **登录与注册页面预览**：提供账号密码表单、密码显隐与页面切换。当前尚未接入账号认证，登录和注册操作保持禁用。
- **运行状态面板**：实际检查 PostgreSQL、Redis、Milvus 和 MinIO 的连接状态，展示探针结果、耗时与基础事件。
- **基础 API**：提供进程存活、依赖就绪、统一错误格式和请求追踪编号；依赖不可用时明确返回未就绪。
- **容器化部署**：前端、API、后台进程及存储服务由 Docker Compose 统一编排，提供服务健康检查、内部网络与持久数据卷。
- **独立文件存储环境**：自动初始化应用与 Milvus 各自的 MinIO Bucket、账号和权限。

当前对话输入、模型回答、会话历史和知识操作尚未开放。后台进程目前执行依赖探测与心跳；基础事件暂存在进程内，尚不是永久业务审计。

## 启动方式

在项目根目录执行以下步骤。

### 1. 配置环境

将 `.env.example` 复制为 `.env`：

```bash
test -f .env || cp .env.example .env
```

首次部署时，编辑 `.env`，为以下四项分别设置随机密码或密钥：

- `POSTGRES_PASSWORD`
- `MINIO_ROOT_PASSWORD`
- `MILVUS_SECRET_KEY`
- `APP_MINIO_SECRET_KEY`

其余账号、Bucket 名和服务配置可以保留示例默认值。当前版本不调用文本模型，无需填写模型 API Key。已有 `.env` 和数据卷时保留原凭证，避免重新初始化密码导致连接失败。

### 2. 准备 MinIO 镜像

当前配置使用以下 MinIO 镜像，服务器与初始化任务共用它：

```bash
docker pull doristhirdpartydocker/minio:RELEASE.2024-11-07T00-52-20Z
```

Compose 设置为使用本地已有的 MinIO 镜像，因此需先拉取或加载。离线部署可使用镜像导出文件：

```bash
docker load -i minio-2024-11-07.tar
```

镜像导出文件不包含在代码仓库中；可在已有镜像的机器上生成：

```bash
docker image save -o minio-2024-11-07.tar doristhirdpartydocker/minio:RELEASE.2024-11-07T00-52-20Z
```

### 3. 构建并启动

```bash
docker compose up -d --build --wait --wait-timeout 300
```

启动后访问：

- [工作台](http://localhost:8080/)
- [登录页面预览](http://localhost:8080/login)
- [API 文档](http://localhost:8080/api/docs)

默认主入口绑定 `127.0.0.1:8080`，可通过 `.env` 中的 `WEB_PORT` 修改端口。其他服务使用 Compose 服务名在内部网络中连接。

常驻服务包括 `web`、`api`、`worker`、`postgres`、`redis`、`milvus`、`etcd` 和 `minio`。`minio-init` 是初始化任务，完成后显示 `Exited (0)` 属于正常状态。

- `web` 提供前端页面并转发 `/api` 请求。
- `api` 接收 HTTP 请求并执行基础接口。
- `worker` 是独立后台进程，后续承担 Agent 与数据处理任务。

### 4. 检查与维护

```bash
docker compose ps
docker compose logs --tail 100 api worker
curl -fsS http://127.0.0.1:8080/api/health/live
curl -fsS http://127.0.0.1:8080/api/health/ready
```

`live` 只证明 API 进程存活；`ready` 会实际访问四项依赖，全部连通才返回 HTTP 200，任一不可用返回 HTTP 503。

容器内运行后端测试：

```bash
docker compose run --rm --no-deps -v "$PWD/backend/tests:/tests:ro" api python -m unittest discover -s /tests -v
```

停止服务并保留数据：

```bash
docker compose down
```

更新代码后重新构建和启动：

```bash
docker compose up -d --build --wait --wait-timeout 300
```

PostgreSQL、Redis、etcd、MinIO 和 Milvus 使用独立命名卷。需要保留数据时，不使用 `docker compose down -v`。

## 未来待实现功能

- **账号与职员管理**：真实登录、注册、退出、演示账号、管理员分配部门、职级和业务权限。
- **真实模型会话**：流式回答、多轮上下文、会话历史、重命名、主备模型切换、重试与降级。
- **企业知识库**：文件提交、管理员审核、解析预览、入库状态、部门与职级权限控制。
- **混合知识检索**：向量与 BM25 召回、RRF 融合、语义重排及可追溯的来源引用。
- **业务数据查询**：Text to SQL、只读与数据范围校验、逐次人工确认及真实结果图表。
- **工作记录整理**：对话转知识、纯文本会议纪要、文稿预览和下载。
- **上下文与长期记忆**：可重建的短期缓存、上下文压缩、按用户隔离的自动后台记忆。
- **永久审计与任务恢复**：持久事件、管理员只读调试、后台任务恢复、备份与恢复验证。

自动化任务流当前仅保留导航入口，具体配置与执行能力留待后续版本。每次更新代码时同步维护本 README 中的当前功能、启动步骤及待实现功能，使说明与实际版本一致。
