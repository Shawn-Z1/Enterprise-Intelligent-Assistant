<div align="center">

# 知行 · 企业智能助理

**能查知识、处理文件、查询业务数据，并保留个人工作记忆的企业 Agent。**

React · FastAPI · LangGraph · PostgreSQL · Redis · Milvus · MinIO · Docker

[功能与架构](#功能与架构) · [快速启动](#快速启动) · [使用指南](#使用指南) · [部署与运维](OPERATIONS.md)

</div>

知行将模型推理、工具执行、身份权限和持久化任务调度组合成一套可部署的系统。用户用自然语言提出任务，Agent 按需检索企业知识、查询公开互联网、读取附件或运行隔离代码，再通过执行记录校验和独立模型审核交付答案。

它的核心不止是聊天界面：长对话可以可靠压缩，个人记忆可以跨会话复用，工具结果有可核对的来源；耗时附件任务可以排队，并在执行完成后由空闲 Worker 接续处理。

## 产品截图

<!-- 将截图放入 assets/screenshots/，取消下面图片行注释即可展示。使用虚构资料，并遮挡密钥和真实个人/业务信息。 -->
<!-- ![知行企业智能助理](assets/screenshots/overview.png) -->

此处预留产品截图。建议展示对话与引用预览、知识库管理、会话审计三个界面，图片统一放在 [`assets/screenshots/`](assets/screenshots/)。

## 功能与架构

| 模块 | 用户能做什么 | 实现特点 |
| --- | --- | --- |
| Agent 对话 | 连续讨论、上传附件、查看引用和任务进度 | LangGraph 状态机；主模型规划和工具循环；备用模型切换；Markdown 回复 |
| 企业知识库 | 上传、审核、检索资料，预览和下载来源；分类管理生效与历史记录 | 真实 CPU 向量检索 + 中文 BM25，RRF 融合与重排；部门、职级及资料有效状态约束 |
| 公开搜索 | 查询外部信息，结合公开来源回答 | 阿里云 IQS `CNLiteBasic`；搜索摘要和网页正文分开标注；关键词生成与企业 RAG 分离 |
| 业务查询 | 自然语言查询员工、考勤、审批、工单和代码提交数据 | Text-to-SQL、SQL AST 校验、受控视图、只读数据库身份；实际 SQL 执行前人工二次确认 |
| 附件与沙箱 | 总结文档、多表分析、计算、绘图与常见文档转换 | 首次模型调用前强制预处理；按需详细读取；每任务一个临时 Docker 容器 |
| 短期上下文 | 保持当前会话连续性，处理较长对话 | PostgreSQL 保留原文，Redis 缓存工作上下文；按模型 token 预算触发可靠摘要压缩 |
| 长期记忆 | 新会话延续个人偏好和曾经讨论的项目 | 后台提取本人原话；语义整理新增/更新/忽略；偏好和目录注入，主题详情按需混合检索 |
| 身份与权限 | 注册、个人资料、密码管理；管理员分配部门、职级和业务资格 | 模型工具清单先过滤，执行和结果发布再次核验；会话、附件、记忆按用户隔离 |
| 调试与审计 | 按用户会话查看每轮问答、模型用量、工具调用和实际事件 | 请求/响应、token、耗时、来源与回执可追踪；事件原地展开，关联确认和后台作业 |
| 部署与恢复 | Compose 启动服务、扩容、备份和隔离恢复 | 外置迁移、幂等种子、固定模型文件哈希；独立维护镜像 |

### 有明确边界的 Agent 状态机

```mermaid
flowchart TD
    A[用户消息与附件] --> B[API 保存消息和运行任务]
    B --> C[Worker 领取任务并恢复检查点]
    C --> D{本次有新附件?}
    D -- 有 --> E[附件预处理]
    D -- 无 --> F[主模型规划]
    E --> F
    F -- 请求工具 --> G[工具执行与权限校验]
    G -- SQL 敏感动作 --> H[人工二次确认]
    H --> G
    G -- 附件解析或代码执行 --> I[沙箱队列与 Runner]
    I --> J[独立 Guest 执行并回收]
    J --> G
    G -- 结果与来源 --> F
    F -- 准备回答 --> K[后端事实校验与独立模型审核]
    K -- 首次需要修正 --> F
    K -- 通过或明确受限 --> L[发布前权限复核并交付]
    L --> M[异步提取和整理长期记忆]
```

队列等待与人工确认会持久化状态，恢复不依赖最初领取任务的进程内存。审核最多要求返工一次；审核不可用或再次未通过时给出明确的受限结果，不把草稿当作已核验答案。

### 持久化队列与并发

- **对话任务**：API 写入 PostgreSQL `app_runtime.runs`。Worker 通过事务锁、`SKIP LOCKED` 与租约竞争领取，使用持久化检查点恢复。
- **沙箱任务**：`app_runtime.sandbox_jobs` 保存待执行作业。Runner 经内部 API 领取和上报，API 校验租约、保存回执并唤醒对话任务。
- **等待释放槽位**：Worker 等待沙箱时保存状态并挂起；结果就绪后可由另一 Worker 接续。Runner 为每次作业创建独立 Guest。
- **忙时排队**：副本数、每实例并发、沙箱全局和每用户额度分别配置。在队列容量和等待时限内排队，前端显示等待状态；超出限额或期限明确报错。

默认 2 个 Worker、每个 2 个槽位；2 个 Runner、每个 1 个槽位；沙箱全局并发 2、每用户并发 1。多副本目前限**同一 Docker 引擎**，没有引入 Celery、RabbitMQ 或 Kafka。

### 上下文、记忆与混合检索

**短期上下文**包含当前会话的用户消息和 Agent 回复。Redis 是缓存，丢失后从 PostgreSQL 重建。默认在模型窗口 75% 处触发压缩，保留最近 4 轮，较早内容生成有界摘要；最终请求仍计算工具 schema、工具结果、输出预算和安全余量。原消息和压缩版本保留。

**长期记忆**分为个人信息/偏好和项目主题。偏好采用可扩展条目，后台模型结合已有条目判断新增、更新或忽略，保留来源和版本。每次请求注入有界的个人偏好及主题目录，需要细节时由模型主动调用 `search_user_memory`，不每轮读取所有项目全文。后台异步生效，刚结束对话后不保证立即可见。

企业 `search_knowledge` 与个人 `search_user_memory` 是独立工具，复用向量、BM25、RRF 与重排基础设施，各有权限过滤和数据空间。Milvus 是派生索引，PostgreSQL 的有效版本和权限仍是判断依据。个人记忆不能替代企业事实的授权查询。

### 文件处理与隔离执行

聊天附件支持 **TXT、MD、CSV、XLSX、DOCX、PDF**，单文件默认 20 MiB，同时受批量大小、解析时间和资源限制。

1. 原件存 MinIO，归属、元数据和会话关联存 PostgreSQL。
2. 有新附件时先经沙箱队列预处理，返回格式、结构、长度和有限样例；大文件不会直接全文塞入模型。
3. 模型按需详细读取指定文件或范围，或者生成代码交给沙箱计算；纯文本摘要可直接由模型完成。
4. 交付正文、图表和产物文件，产物进入 MinIO，在用户权限内预览、下载。

Guest 预装 Pandas、NumPy、DuckDB、OpenPyXL、Matplotlib、Seaborn、python-docx、pypdf、Pandoc、LibreOffice Writer 和中文字体。转换效果受原件布局和库能力限制；暂不支持扫描 PDF 的 OCR、图片理解及音视频处理。

Runner 是常驻容器控制服务，Guest 是预构建执行镜像。每次作业创建新容器：断网、输入只读、无应用凭证、限制 CPU/内存/进程，完成后回收。附件和沙箱处理不弹人工确认；当前敏感确认针对业务 SQL 查询。

## 技术栈

| 层次 | 技术 | 职责 |
| --- | --- | --- |
| 前端 | React 19、TypeScript、Vite、Lucide、React Markdown | 对话、管理、引用与产物预览 |
| API | Python 3.12、FastAPI、Pydantic、HTTPX | 身份权限、文件接口、任务接收和事件流 |
| Agent | LangGraph、自定义 PostgreSQL Checkpointer | 工具循环、挂起恢复、审核与发布 |
| 模型 | OpenAI 兼容 Chat Completions 接口 | 主模型、备用模型和审核模型分别配置 |
| 存储 | PostgreSQL 17、Redis 7.4、Milvus 2.5、MinIO | 事实与队列、缓存、检索索引、对象原件与产物 |
| 检索 | 中文 BGE、BGE Reranker、ONNX Runtime | 本地 CPU 向量和重排，与中文 BM25 混合召回 |
| 执行 | Docker Engine、Docker Compose | 服务编排与隔离任务容器 |
| SQL | SQLGlot、受保护视图、独立只读身份 | 限制自然语言生成 SQL 的可执行范围 |

## 快速启动

### 1. 准备环境

- 推荐 Linux **x86_64** + Docker Engine + Compose 插件；Windows 使用 Docker Desktop 的 Linux 容器模式。
- Vite 前端需 Node.js 22.12+（推荐 24）及 pnpm 10.14.0。全容器部署不需宿主 Python/Node.js。
- 建议从 **4 核 CPU、12 GiB 可用内存、20 GiB 可用磁盘**起步，再按文件大小和并发扩容；这是容量建议，不是性能承诺。
- 首次构建需要访问镜像源、Python/Node 包源、GitHub 分词器文件和 Hugging Face 固定模型文件；运行时需访问所配置的模型和搜索 API。

```sh
git clone https://github.com/Shawn-Z1/Enterprise-Intelligent-Assistant.git
cd Enterprise-Intelligent-Assistant
```

### 2. 生成凭证并配置 API

Windows PowerShell：

```powershell
./scripts/init-env.ps1
```

Linux：

```sh
sh scripts/init-env.sh
```

脚本从 `.env.example` 创建 `.env`，分别生成数据库、管理员、对象存储与沙箱随机密码，不回显；已有配置不重新生成。不要提交 `.env`。

编辑 `.env`：

| 配置 | 用途 | 要求 |
| --- | --- | --- |
| `PRIMARY_BASE_URL` / `PRIMARY_API_KEY` / `PRIMARY_MODEL` | 主模型，需支持工具调用 | 聊天必需 |
| `FALLBACK_BASE_URL` / `FALLBACK_API_KEY` / `FALLBACK_MODEL` | 主模型失败时备用 | 完整主备能力需配置 |
| `REVIEW_BASE_URL` / `REVIEW_API_KEY` / `REVIEW_MODEL` | 发布前独立模型审核 | 完整核验答案需配置 |
| `WEB_SEARCH_API_KEY` | 阿里云 IQS Key | 联网搜索需配置 |
| `INITIAL_ADMIN_PASSWORD` | 首次 `admin` 密码 | 脚本随机生成，在本机查看 |
| `PRIMARY_CONTEXT_TOKENS` / `FALLBACK_CONTEXT_TOKENS` / `REVIEW_CONTEXT_TOKENS` | 各模型上下文窗口 | 必须匹配所用实际型号 |

备用与审核可填相同 URL、Key、型号，但配置各自独立。示例型号需核对账号可用性；**不要将示例百万上下文用于小窗口模型**。`MODEL_OUTPUT_TOKENS` 是单次输出预算。模型与搜索调用可能产生费用。

### 3. 后端容器＋本地 Vite 前端

Windows PowerShell：

```powershell
./scripts/deploy.ps1 -LocalFrontend
cd frontend
pnpm install --frozen-lockfile
npm run dev
```

Linux：

```sh
sh scripts/deploy.sh --local-frontend
cd frontend
pnpm install --frozen-lockfile
npm run dev
```

部署入口依次构建后端、维护工具和 Guest，启动存储与 CPU 检索服务，执行迁移和种子，再启动 API、Worker、Runner。版本回执保证重复执行不重置账号或重复导入。

打开 **http://127.0.0.1:8080**，账号 **`admin`**，密码为本机 `.env` 的 `INITIAL_ADMIN_PASSWORD`。Vite 同源代理 `/api`，保留会话 Cookie、CSRF 和事件流。

### 4. 或者全部容器运行

在项目根目录执行：

```sh
sh scripts/deploy.sh
```

Windows 对应 `./scripts/deploy.ps1`。脚本启用 `production` profile，通过 Nginx 提供同一网页入口。Vite 和 Nginx 默认占用同一端口，二选一。

### 5. 检查服务

```sh
docker compose ps
docker compose logs --tail=80 api worker sandbox-runner
```

- API 就绪检查：http://127.0.0.1:8000/api/health/ready 。
- MinIO 控制台：http://127.0.0.1:9001 ，使用 `.env` 的 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`。
- 首次模型下载可能较慢，哈希验证通过后才能就绪。首次部署不能只运行 `docker compose up` 而跳过迁移与种子，请使用上述入口。

## 使用指南

### 新安装包含什么

| 内容 | 初始化行为 |
| --- | --- |
| 登录账号 | 仅一个 `admin` 管理员，不创建张三等成员账号 |
| 部门 | 管理、技术、人事、法务四个虚构部门 |
| 模拟业务 | `demo_business` 六张底表及受控视图，包含虚构员工、考勤、审批、工单和代码提交数据 |
| 知识库、聊天附件、会话、个人记忆 | 空，使用者自行添加 |
| 审计和版本回执 | 仅初始化产生的操作和迁移/种子记录，不导入开发环境历史 |

`demo_business` 是同一 PostgreSQL 中的独立 schema。示例员工非实际人员，除管理员外不关联登录账号；邮箱为 `example.invalid`，电话和提交记录均为虚构。仓库不包含开发环境数据库、MinIO 对象或备份。

新成员自行注册，由管理员分配部门和职级，业务工具资格单独开启。实际企业业务查询还需可信的用户/员工映射与相应受控视图，不能靠模型猜测身份。默认管理员可测试全部模拟数据。

### 首次体验路径

1. **对话**：询问“帮我列一个新员工入职清单”，观察审核与 Markdown 回复。
2. **知识库**：上传虚构制度文档，等待“生效中”，询问其中内容并点引用预览。普通成员提交需管理员审核。
3. **公开搜索**：配置 IQS Key 后查询公开信息，核对真实引用；未取得网页正文时只按实际摘要说明。
4. **SQL**：管理员询问“统计 2026 年 9 月 7 日至 30 日技术部所有人的代码提交次数，按人员汇总”，确认 SQL 后执行。固定样例在 2026 年 9～10 月，不使用“今天”查询。
5. **附件**：上传虚构 CSV/XLSX，统计并生成图表；或上传 MD/DOCX，整理并导出 PDF，在来源面板下载。
6. **记忆**：明确告诉 Agent 个人技术偏好，待后台提取完成后新建会话询问；项目详情按需检索。
7. **审计**：管理员选择会话和具体轮次，查看模型请求、用量、参数及实际工具回执。

### 扩容配置

修改 `.env` 后执行 `docker compose up -d --wait`：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `WORKER_REPLICAS` / `WORKER_CONCURRENCY` | 2 / 2 | Worker 数量 / 每实例任务片段并发 |
| `SANDBOX_RUNNER_REPLICAS` / `SANDBOX_RUNNER_CONCURRENCY` | 2 / 1 | Runner 数量 / 每实例 Guest 容量 |
| `SANDBOX_GLOBAL_CONCURRENCY` / `SANDBOX_USER_CONCURRENCY` | 2 / 1 | 全局 / 每用户沙箱执行上限 |
| `SANDBOX_QUEUE_LIMIT` / `SANDBOX_WAIT_SECONDS` | 50 / 300 | 队列容量 / 最长排队秒数 |
| `CONTEXT_COMPRESS_TRIGGER_RATIO` / `CONTEXT_KEEP_TURNS` | 0.75 / 4 | 压缩触发比例 / 保留轮次 |

多个 Runner 不要设置同一个 `SANDBOX_RUNNER_ID`，默认使用容器自身身份。扩容仍受宿主资源、数据库连接、供应商限流和全局额度约束。

## 部署边界与数据保护

- 默认仅向 `127.0.0.1` 暴露 Web、API、PostgreSQL 与 MinIO 控制台。公网服务通过 HTTPS 反向代理提供 Web，设置 `SESSION_COOKIE_SECURE=true`，正确传递可信协议和 Host，不公开存储和控制端口。
- Runner 持有 Docker socket，具备该引擎的容器控制能力。Guest 为普通 Docker，**未启用 gVisor/runsc**，不等同于虚拟机隔离；建议使用专用宿主或引擎。
- 企业 RAG、SQL 和个人记忆核验身份及有效版本。公开关键词不使用内部检索正文扩写，仅允许必要的本人粗略城市指代投影，并做轻量输入检查。
- 模型供应商会收到任务所需的上下文和工具结果；本地存储不代表全部推理在本地。按部署方的数据使用要求选择接口。
- `.env`、密钥、运行数据、备份、本机协作规则和阶段验收脚本不随发布目录分发。不要强制提交忽略项或公开真实审计正文。
- 适用性验证基于 Windows Docker Desktop 的 Linux/x86_64 容器。独立 Linux 物理服务器和 ARM 未逐项实测，不承诺任意平台、外部 API 或文件格式都无需适配。

升级、备份、恢复和问题排查见 [OPERATIONS.md](OPERATIONS.md)。

## 仓库结构

```text
├── frontend/                  # React、Vite、Nginx
├── backend/                   # API、Agent、队列、权限、记忆与维护
├── services/sandbox-runner/   # Runner 与临时 Guest 镜像
├── retrieval_models/         # 固定 CPU 检索模型下载与校验
├── database/
│   ├── migrations/           # 版本化结构迁移
│   ├── seeds_release/        # 一个管理员和虚构业务数据
│   └── seeds*/               # 旧种子与回执兼容文件
├── scripts/                  # 初始化、部署、构建、备份与恢复
├── assets/screenshots/       # 截图预留
├── .env.example
├── compose.yaml
├── OPERATIONS.md
└── README.md
```
