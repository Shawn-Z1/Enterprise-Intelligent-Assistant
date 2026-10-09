# 知行 · 企业智能助理

面向企业员工的浏览器工作台，用于集中处理企业知识、业务数据和工作记录。当前版本支持真实账号、职员权限管理、主备模型流式文字对话、历史会话和永久审计。企业知识与业务工具将在后续接入。

## 当前功能

- **工作台界面**：提供对话工作区、历史会话区域、知识库管理、自动化任务流与个人中心的导航入口；支持桌面与小屏布局。
- **聊天展示**：普通消息卡片不展示模型型号和 token 用量，相关信息由后端审计保存。输入框与发送/停止按钮分行留出间距；文字能力与回答核实提示统一放在底部状态栏，与运行状态入口同排。
- **真实文字对话**：后台调用模型并流式返回回答，支持多轮、停止回答、刷新恢复、本人历史打开与搜索、重命名。原消息和执行事件永久保存在 PostgreSQL，SSE 断线按序补发，不重新调用模型。
- **统一模型网关**：主备槽位各使用 URL、API Key、模型名接入。默认 DeepSeek 主模型、千问备用；适配公开思考参数及流式用量，业务代码不直接调用厂商接口。
- **失败与降级**：主模型总尝试最多三次，仍失败切备用，备用最多三次；凭证和参数错误直接停止该槽位的重试。包含退避、初始熔断与半开探测，全部失败明确结束，不把半截回复保存为完整回答。
- **上下文与用量**：模型实际收到本人已完成轮次和本次输入，保留原始请求、流式包及合并回复。每个槽位独立检查窗口和输出预留；超窗明确拒绝，自动压缩尚未接入。供应商用量标为 reported，无供应商用量时明确估算方法，失败时未知数值为空；流式累计用量不重复相加。
- **北京时间**：应用事件、接口日期、数据库连接及前端展示统一使用 `Asia/Shanghai`、`+08:00`；初始化脚本中的无时区时间也按北京时间解释。既有 Demo 数据的实际时刻和正文不变，供应商原始协议包保留原样。
- **真实登录与注册**：账号密码登录、零权限普通成员注册、密码显隐、退出和刷新恢复。会话保存在 PostgreSQL，浏览器使用 HttpOnly Cookie，写操作验证会话绑定的 CSRF。
- **个人中心**：展示可信账号、角色、部门、职级、岗位及业务资格，支持修改本人姓名和密码；修改密码会撤销所有旧会话。
- **管理员职员管理**：分配部门、1～10 职级、岗位与业务查询开关。角色和职级不能由注册或个人设置自选；普通成员没有管理菜单或管理接口权限。
- **权限与资源隔离**：每次请求重新读取当前身份，撤权后旧会话立即失去资格；本人会话与附件访问先检查所有者，不能通过猜测 ID 读取他人数据。
- **运行状态面板**：实际检查 PostgreSQL、Redis、Milvus 和 MinIO 的连接状态，展示探针结果、耗时与基础事件。
- **基础 API**：提供进程存活、依赖就绪、统一错误格式和请求追踪编号；依赖不可用时明确返回未就绪。
- **开发与部署**：生产环境由 Docker Compose 启动前端 Nginx、API、后台进程及存储；开发环境仅后端与存储容器化，前端执行 `npm run dev`，修改样式和组件即时热更新。
- **独立文件存储环境**：自动初始化应用与 Milvus 各自的 MinIO Bucket、账号和权限。
- **数据库迁移与模拟数据**：显式导入外置结构和种子数据，建立系统、运行、知识、记忆及审计的基础表，以及五张虚构业务表；重复导入不会重置账号权限、密码或原始记录。
- **短编号**：部门 `D1`～`D4`、用户 `U1`～`U5`、员工 `E1`～`E5`、考勤 `A1`～`A25`、审批 `P1`～`P3`、工单 `T1`～`T3`。新产生的实体、会话和追踪编号使用 16 字符随机字符串，含 96 位随机信息。所有关系字段和权限数组使用相同格式；认证令牌与密码哈希保留原有安全长度。
- **演示账号**：`admin` 为管理员；`zhangsan`、`lisi`、`wangwu`、`zhaoliu` 为成员，分别关联员工 ID、部门与职级。管理员密码来自首次导入时的 `INITIAL_ADMIN_PASSWORD`，四位成员密码来自 `INITIAL_DEMO_PASSWORD`。
- **业务只读连接**：使用独立数据库身份读取五张模拟业务表，不能读系统账号、会话、记忆或审计表，也不能写入业务数据；按员工身份的行列策略将在业务查询阶段接入。
- **永久审计底座**：PostgreSQL 追加保存请求与后台状态的 trace/span、原始 JSON 和用量来源，应用身份不能修改、删除或清空审计；业务变更可与审计共用事务，审计失败则整体回滚。

当前支持普通文字聊天，知识操作与业务 SQL 尚未开放，不会声称已经读取企业资料或执行查询。附件提供本人读取鉴权基础接口，上传尚未开放。后台进程领取文本任务并执行模型调用，同时进行依赖探测和心跳。运行状态面板显示进程内最近 100 条基础事件，对话内显示本次真实执行记录；永久审计另存 PostgreSQL，管理员只读查询页面尚未开放。

新注册成员可以进行普通文字聊天并进入个人中心，默认无部门、职级、员工关联及业务权限。请线下联系管理员分配业务身份；系统没有提权申请入口。分配部门和职级后获得知识资格，业务查询还需单独开启。尚未授权的 RAG/SQL 请求返回 403，已授权请求当前返回未开放状态，不会提前检索或执行 SQL。

## 启动方式

在项目根目录执行以下步骤。

### 1. 配置环境

将 `.env.example` 复制为 `.env`：

```bash
test -f .env || cp .env.example .env
```

首次部署时，编辑 `.env`，为以下八项分别设置至少 12 位的随机密码或密钥，替换示例占位值：

- `POSTGRES_PASSWORD`
- `APP_DB_PASSWORD`
- `BUSINESS_DB_PASSWORD`
- `INITIAL_ADMIN_PASSWORD`
- `INITIAL_DEMO_PASSWORD`
- `MINIO_ROOT_PASSWORD`
- `MILVUS_SECRET_KEY`
- `APP_MINIO_SECRET_KEY`

其余账号、Bucket 名和服务配置可以保留示例默认值。`INITIAL_ADMIN_PASSWORD` 用于首次创建管理员；`INITIAL_DEMO_PASSWORD` 用于四个演示成员，数据库中只保存独立随机盐的 Argon2id 哈希。重复导入不会改动已有账号密码。

会话默认有效期为 24 小时，`SESSION_HOURS` 可配置。当前本机 HTTP 入口使用 `SESSION_COOKIE_SECURE=false`；经 HTTPS 提供服务时应设为 `true`。默认同一来源账号每分钟最多 20 次登录尝试，超过返回 429；可通过 `LOGIN_LIMIT_PER_MINUTE` 配置。

文字对话需要配置主备模型槽位，每个槽位只需以下三项：

```dotenv
PRIMARY_BASE_URL=https://api.deepseek.com
PRIMARY_API_KEY=your-primary-api-key
PRIMARY_MODEL=deepseek-flash
FALLBACK_BASE_URL=https://your-workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
FALLBACK_API_KEY=your-fallback-api-key
FALLBACK_MODEL=qwen-plus
```

千问地址和密钥必须属于同一工作空间，使用服务商实际提供的地址。更换兼容模型只修改对应三项，执行 `docker compose up -d --force-recreate api worker` 重新加载环境，无需改业务代码或重新构建镜像。未配置、凭证错误或模型故障都会明确失败。

可选策略见 `.env.example`：输出默认最多 2048 tokens；读取超时 45 秒；任务预算 240 秒；连续三次故障熔断 30 秒；worker 默认并发 2。上下文使用保守能力目录下限，已知协议默认 32768、其他兼容服务默认 8192；可通过 `PRIMARY_CONTEXT_TOKENS` / `FALLBACK_CONTEXT_TOKENS` 指定服务实际支持的窗口，`0` 表示使用目录默认值。估算包含完整请求与输出预留，并留 20% 安全余量。

已有 `.env` 和数据卷时保留原凭证；从旧版升级时仅补齐新增配置，不重置旧配置。api/worker 使用应用、业务及模型凭证，数据库管理员及初始账号密码只交给显式管理任务。模型调用会产生服务商实际费用。

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

### 3. 启动容器后端与存储

```bash
docker compose build
docker compose stop api worker
docker compose up -d postgres
docker compose --profile tools run --rm db-admin migrate
docker compose --profile tools run --rm db-admin seed
docker compose up -d --wait --wait-timeout 300
```

### 4. 开发：后端容器 + 本地前端

默认 `docker compose up -d --wait --wait-timeout 300` 只启动 API、worker 和存储，不启动 `web`。API 仅发布本机 `127.0.0.1:8000`，通过 `.env` 的 `API_HOST_PORT` 修改。

前端恢复原有 pnpm 依赖锁和容器构建方式。首次准备依赖仍使用项目锁定的 pnpm；`npm run dev` 调用同一个 Vite 脚本，与依赖安装工具无冲突：

```bash
cd frontend
pnpm install --frozen-lockfile
npm run dev
```

需要 Node.js 20.19+ 或 22.12+、pnpm 10.14。依赖已安装时直接运行 `npm run dev`。开发入口 [工作台](http://localhost:8080/)、[登录页面](http://localhost:8080/login)、[API 文档](http://localhost:8080/api/docs)，由 `.env` 的 `DEV_PORT` 调整。保存 CSS/组件即可热更新，不需要构建或启动前端容器。停止本地前端按 Ctrl+C。

开发请求经 Vite 的 `/api` 同源代理访问容器 API，保留原 Host 和 SSE 流，后端 Origin、CSRF、所有者检查继续有效。前端只读取开发/API 两个端口字段，不注入数据库密码或模型密钥。

如果之前已经启动过生产前端，切回开发时先停止它，避免同端口冲突：

```bash
docker compose stop web
docker compose up -d --wait --wait-timeout 300
```

### 5. 生产：完整容器部署

前端容器通过 `production` profile 保留，不需要安装宿主 Nginx或单独发布静态目录。先停止同端口的本地开发服务，再在项目根目录执行：

```bash
docker compose --profile production up -d --build --wait --wait-timeout 300
```

该命令包含 `web`，前端 Dockerfile 用锁定依赖构建静态资源，交给容器里的 Nginx 托管。生产入口默认 `127.0.0.1:8080`，由 `WEB_PORT` 修改；Nginx 通过内部服务名 `api:8000` 转发 `/api`，保留 SPA 路由回退和 SSE 无缓冲。

默认开发与生产入口都用 8080，通常分别运行。如果需要同时查看两种版本，将 `DEV_PORT` 调成其他端口；API 代理仍根据 `API_HOST_PORT` 配置连接。

原有 `npm run build` 仍可在本地验证构建，`npm run preview` 可临时预览，正式交付继续使用生产前端容器。

常驻后端容器包括 `api`、`worker`、`postgres`、`redis`、`milvus`、`etcd` 和 `minio`；生产额外启动 `web`。`minio-init` 完成后显示 `Exited (0)` 正常，`db-admin` 仅执行显式迁移和导入，不是常驻服务。

`database/` 只读挂载给管理任务，不写入镜像，也不在应用启动时自动执行。迁移与种子有指纹回执；重复执行跳过已完成内容，改变已应用文件会拒绝导入，应新增文件表达变化。当前导入 `database/seeds_short_ids/`，旧版种子保留用于历史升级验证；已有账号不重置密码或授权。审计的关系字段使用短编号，原始 JSON 保留当时记录的编号与正文。

- 开发 Vite 或生产 `web` 提供同源页面与 `/api` 代理。
- `api` 接收 HTTP 请求，执行认证、权限和模型任务接口。
- `worker` 领取持久文本任务并调用模型，同一会话同时只允许一条回答；重复发送标识不会生成重复任务。

### 使用 Navicat 或其他数据库客户端

新建 PostgreSQL 连接，填写以下配置：

- 主机：`127.0.0.1`
- 端口：`5432`，或 `.env` 中配置的 `POSTGRES_HOST_PORT`
- 初始数据库：`zhixing`，或 `.env` 中的 `POSTGRES_DB`
- 用户名：`zhixing`，或 `.env` 中的 `POSTGRES_USER`
- 密码：`.env` 中的 `POSTGRES_PASSWORD`，与网页登录密码分别配置

上述管理员连接能查看全部 schema。展开 `demo_business` 查看部门、员工、考勤、审批、工单五张业务表；`app_system` 保存账号、权限和会话，`app_audit` 保存审计记录。只查看模拟业务数据时，使用 `BUSINESS_DB_USER` 和 `BUSINESS_DB_PASSWORD`，该身份只能读取五张业务表。

端口仅绑定本机，不向其他机器开放。如果本机 `5432` 已被占用，可配置 `POSTGRES_HOST_PORT=15432`，执行 `docker compose up -d postgres` 后，用客户端连接 `127.0.0.1:15432`。容器内部继续使用 `postgres:5432`。

网页登录账号的初始密码在 `.env` 的 `INITIAL_ADMIN_PASSWORD`（`admin`）和 `INITIAL_DEMO_PASSWORD`（四位演示成员）。`db-admin seed` 读取这些字段，调用 `backend/app/passwords.py` 的 Argon2id 哈希函数，然后将结果写入 `app_system.users.password_hash`；SQL 和账号清单都不保存明文密码。新注册用户的密码由注册接口按同样方式哈希存储。改密码后，以数据库里的新哈希为准，初始环境变量不会自动覆盖它。

已有数据库升级前建议备份：

```bash
mkdir -p backups
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "backups/zhixing-$(TZ=Asia/Shanghai date +%Y%m%d-%H%M%S).dump"
```

保留 `.env` 和数据卷，按前面的构建、迁移、导入及启动命令升级，不删除数据卷。备份、环境凭证和本地验收记录均不进入 Git。

### 6. 检查与维护

```bash
docker compose ps
docker compose logs --tail 100 api worker
curl -fsS http://127.0.0.1:8080/api/health/live
curl -fsS http://127.0.0.1:8080/api/health/ready
```

`live` 只证明 API 进程存活；`ready` 会实际访问四项依赖，全部连通才返回 HTTP 200，任一不可用返回 HTTP 503。

容器内运行后端测试：

```bash
docker compose run --rm --no-deps -v "$PWD/backend/tests:/tests:ro" api python -m unittest discover -s /tests -p test_foundation.py -v
docker compose --profile tools run --rm --no-deps --entrypoint python -v "$PWD/backend/tests:/tests:ro" db-admin -m unittest discover -s /tests -p test_m02.py -v
docker compose --profile tools run --rm --no-deps --entrypoint python -v "$PWD/backend/tests:/tests:ro" db-admin -m unittest discover -s /tests -p test_m03.py -v
docker compose --profile tools run --rm --no-deps --entrypoint python -v "$PWD/backend/tests:/tests:ro" db-admin -m unittest discover -s /tests -p test_short_ids.py -v
```

完整身份与基础回归验收：

```bash
sh scripts/verify-m03.sh
```

第四阶段完整验收（包含真实主备模型调用，使用 `.env` 配置）：

```bash
sh scripts/verify-m04.sh
```

该验收使用独立 PostgreSQL 测试库，验证真实多轮对话、三参数换模型、主模型故障后的实际备用回答，以及确定性 3+3 失败、熔断、取消、SSE 补发、幂等发送、上下文预算与审计保护。测试结束删除独立库，本地报告放在忽略的 `verification/`。浏览器验收会话作为普通原始记录保留，避免为清理测试而删除不可变历史。

会话任务与事件独立于浏览器连接。关闭页面不会停止后台任务，重新打开会话会恢复状态；停止按钮会请求后台终止模型流。容器停止或租约丢失时，任务明确结束为中断，不自动重复可能已经发出的请求。完整 Agent 恢复与检查点接管在后续阶段实现。

身份测试使用真实独立 PostgreSQL 测试库和真实 MinIO 对象，验证注册零权限、管理员赋权/撤权、旧会话复检、CSRF、过期与禁用、本人会话/附件所有权、实际下载和审计失败回滚。测试结束仅清理本次创建的库和对象，不改动演示账号。

完整数据库与持久化验收：

```bash
sh scripts/verify-m02.sh
```

该脚本会创建并清理独立测试数据库，验证账号关联、幂等导入、只读隔离、审计不可变、用量来源和审计失败回滚，然后重启 PostgreSQL、重建 api/worker，检查原账号、历史计数及审计标记保留。运行期间服务会短暂不可用。结果写入本机 `verification/M02/`，该目录不纳入 Git。

停止容器服务并保留数据（本地前端终端另按 Ctrl+C）：

```bash
docker compose down
```

更新后端代码后重新构建和启动（生产运行时给命令加 `--profile production`）：

```bash
docker compose build
docker compose stop api worker
docker compose up -d postgres
docker compose --profile tools run --rm db-admin migrate
docker compose --profile tools run --rm db-admin seed
docker compose up -d --wait --wait-timeout 300
```

更新前端代码时，开发环境由 Vite 热更新；生产环境执行 `docker compose --profile production build web`，然后 `docker compose --profile production up -d --no-deps web`。

PostgreSQL、Redis、etcd、MinIO 和 Milvus 使用独立命名卷。需要保留数据时，不使用 `docker compose down -v`。

## 未来待实现功能

- **企业知识库**：文件提交、管理员审核、解析预览、入库状态、部门与职级权限控制。
- **混合知识检索**：向量与 BM25 召回、RRF 融合、语义重排及可追溯的来源引用。
- **业务数据查询**：Text to SQL、只读与数据范围校验、逐次人工确认及真实结果图表。
- **工作记录整理**：对话转知识、纯文本会议纪要、文稿预览和下载。
- **上下文与长期记忆**：可重建的短期缓存、上下文压缩、按用户隔离的自动后台记忆。
- **审计工作台与任务恢复**：基于已建立的永久审计，实现管理员只读调试、后台任务恢复、备份与恢复验证。

自动化任务流当前仅保留导航入口，具体配置与执行能力留待后续版本。每次更新代码时同步维护本 README 中的当前功能、启动步骤及待实现功能，使说明与实际版本一致。
