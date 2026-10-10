# 知行 · 企业智能助理

面向企业员工的浏览器工作台，用于集中处理企业知识、业务数据和工作记录。当前版本支持真实账号、职员权限管理、主备模型对话、独立模型答案审核、知识提交与审核、完整混合检索、阿里云公开搜索、内部/公开组合引用预览和永久审计。业务数据库查询将在后续接入。

## 当前功能

- **工作台界面**：提供对话工作区、历史会话区域、知识库管理、自动化任务流与个人中心的导航入口；支持桌面与小屏布局。
- **知识提交与审核**：支持 PDF、DOCX、TXT、Markdown 单份或批量提交，统一选择允许部门和最低职级，每份文件独立显示结果。普通成员待管理员审核，管理员确认后直接进入同一后台处理队列；审核者只能通过或驳回，不能暗改 ACL。
- **真实原件预览**：原件存储在应用专属 MinIO Bucket，后台隔离解析并保存不可变预览。PDF 支持逐页图片、文字和 50%–250% 缩放，DOCX 保留段落/表格顺序；TXT、Markdown 安全文本展示，不执行脚本或访问外部链接。每次预览、翻页和下载都重新鉴权。
- **知识权限与维护**：普通成员只管理本人材料。创建者不满足自己选择的 ACL 时只能看管理信息，不能读取正文；管理员查看全部材料。支持撤回、删除和受控重试，原件或 ACL 变化使批准失效，晚到后台任务不能重新发布删除的材料。
- **完整混合检索**：批准后的原件按页码/标题切片，使用真实中文 CPU Embedding 与 Milvus 向量、中文 BM25 两路召回，按切片编号去重后 RRF 融合，再由独立交叉编码器语义重排。默认两路各 20、重排最多 20、最终最多 5 个；低相关不凑数，编号和日期使用结构过滤。
- **自主知识工具**：LangGraph 运行模型→知识工具→观察→回答循环，文本模型自主选择是否检索。内部事实使用当前合法证据，普通问题可以直接回答；保存真实工具调用、两路候选、融合排名、重排和来源证据。无权限在召回前拒绝，无证据不能编造内部规定。
- **公开联网搜索（M06A）**：使用阿里云 IQS 国内极速版 `CNLiteBasic`，模型自主选择内部RAG、公开搜索、组合查询或直接回答。每次模型请求按当前身份过滤工具目录，未授权知识账号仍能公开搜索；重试/备用与工具执行重新核验资格。RAG与公开搜索独立调用，只有最终回答汇总证据；搜索服务与关键词模块只接收用户本次输入及可信身份/任务，不接收或比对RAG、SQL、附件正文及历史。主Agent候选仅存本地工具审计，不影响外发生成。输入和生成词保留轻量凭证/明显业务明细检查；混合任务提取明确公开子句，模糊或含私密载荷时保守拒绝。
- **公开来源预览**：返回网页标题、URL、可得页面日期、北京时间获取时间与内容类型，右侧只展示本人持久文本快照，不嵌网页/图片/脚本。区分搜索摘要、提供商正文片段及实际页面文本；没有读取页面时不称已读原文。每次访问来源重新鉴权，搜索失败/无结果/截断真实说明。
- **可核实来源**：回答附文件、页码或片段引用，点击打开右侧来源面板，并可逐页预览或下载原件。来源接口每次检查当前文档状态、批准、部门职级和版本；删除后旧引用停止正文访问，原聊天记录保持完整。
- **聊天展示**：智能体回复按Markdown渲染标题、加粗、列表、引用、代码块和表格，历史与分段回复使用同一渲染组件；用户输入仍保留原文，来源按钮保持可点击。原始HTML不执行，外部图片以链接展示，宽表格与代码块独立横向滚动。普通消息卡片不展示模型型号和 token 用量，相关信息由后端审计保存。输入框与发送/停止按钮分行留出间距；文字能力与回答核实提示统一放在底部状态栏，与运行状态入口同排。
- **真实文字对话**：后台调用模型，待发布正文先缓冲，核验后分段展示；支持多轮、停止回答、刷新恢复、本人历史打开与搜索、重命名。原消息和执行事件永久保存在 PostgreSQL，SSE 断线按序补发，不重新调用模型。
- **发布前答案审核**：LangGraph增加独立review/publish节点，所有待发布答案（含零工具直接回答）经过核验。后端按PG记录核对调用/结果配对、终态和引用；审核模型检查任务完成度、执行真实性、证据一致性与修正可行性。最多一次返工、再次审核；证据不足、二次驳回或审核不可用仅交付服务端资料摘录和限制，不发布未通过草稿。等待时展示真实执行/审核状态；这不能保证消除所有模型错误，也不能替代权限及人工二次确认。
- **统一模型网关**：主、备用和审核槽位各使用 URL、API Key、模型名接入。默认 DeepSeek 主模型、千问备用及独立千问审核；适配公开思考参数及流式用量，业务代码不直接调用厂商接口。
- **失败与降级**：主模型总尝试最多三次，仍失败切备用，备用最多三次；凭证和参数错误直接停止该槽位的重试。包含退避、初始熔断与半开探测，全部失败明确结束，不把半截回复保存为完整回答。
- **上下文与用量**：模型实际收到本人已完成轮次和本次输入，保留原始请求、流式包及合并回复。每个槽位独立检查窗口和输出预留；超窗明确拒绝，自动压缩尚未接入。供应商用量标为 reported，无供应商用量时明确估算方法，失败时未知数值为空；流式累计用量不重复相加。
- **模型分词预算**：DeepSeek Flash使用官方V4.1参考分词器，当前千问备用型号使用DashScope官方Qwen参考分词器；在本地计算文本及消息/工具预算，不每次联网计数，不下载推理权重。保留聊天格式预留与20%安全余量，本地请求总数标estimated，不能冒充云端完整模板或计费用量。未知型号才回退字节上界；Embedding/重排保留各自真实分词器，供应商实际usage优先，不改旧审计记录。
- **北京时间**：应用事件、接口日期、数据库连接及前端展示统一使用 `Asia/Shanghai`、`+08:00`；初始化脚本中的无时区时间也按北京时间解释。既有 Demo 数据的实际时刻和正文不变，供应商原始协议包保留原样。
- **真实登录与注册**：账号密码登录、零权限普通成员注册、密码显隐、退出和刷新恢复。会话保存在 PostgreSQL，浏览器使用 HttpOnly Cookie，写操作验证会话绑定的 CSRF。
- **个人中心**：展示可信账号、角色、部门、职级、岗位及业务资格，支持修改本人姓名和密码；修改密码会撤销所有旧会话。
- **管理员职员管理**：分配部门、1～10 职级、岗位与业务查询开关。角色和职级不能由注册或个人设置自选；普通成员没有管理菜单或管理接口权限。
- **权限与资源隔离**：每次请求重新读取当前身份，撤权后旧会话立即失去资格；本人会话与附件访问先检查所有者，不能通过猜测 ID 读取他人数据。
- **运行状态面板**：实际检查 PostgreSQL、Redis、Milvus、MinIO 和 CPU 检索模型的连接状态，展示探针结果、耗时与基础事件。
- **基础 API**：提供进程存活、依赖就绪、统一错误格式和请求追踪编号；依赖不可用时明确返回未就绪。
- **开发与部署**：生产环境由 Docker Compose 启动前端 Nginx、API、后台进程及存储；开发环境仅后端与存储容器化，前端执行 `npm run dev`，修改样式和组件即时热更新。
- **独立文件存储环境**：自动初始化应用与 Milvus 各自的 MinIO Bucket、账号和权限。
- **数据库迁移与模拟数据**：显式导入外置结构和种子数据，建立系统、运行、知识、记忆及审计的基础表，以及五张虚构业务表；重复导入不会重置账号权限、密码或原始记录。
- **短编号**：部门 `D1`～`D4`、用户 `U1`～`U5`、员工 `E1`～`E5`、考勤 `A1`～`A25`、审批 `P1`～`P3`、工单 `T1`～`T3`。新产生的实体、会话和追踪编号使用 16 字符随机字符串，含 96 位随机信息。所有关系字段和权限数组使用相同格式；认证令牌与密码哈希保留原有安全长度。
- **演示账号**：`admin` 为管理员；`zhangsan`、`lisi`、`wangwu`、`zhaoliu` 为成员，分别关联员工 ID、部门与职级。管理员密码来自首次导入时的 `INITIAL_ADMIN_PASSWORD`，四位成员密码来自 `INITIAL_DEMO_PASSWORD`。
- **业务只读连接**：使用独立数据库身份读取五张模拟业务表，不能读系统账号、会话、记忆或审计表，也不能写入业务数据；按员工身份的行列策略将在业务查询阶段接入。
- **永久审计底座**：PostgreSQL 追加保存请求与后台状态的 trace/span、原始 JSON 和用量来源，应用身份不能修改、删除或清空审计；业务变更可与审计共用事务，审计失败则整体回滚。

知识处理状态包括待审核、批准待处理、解析中、待索引、向量化中、索引中、可检索、失败、删除中和已失效。只有真实双路索引完成并复检批准后才显示“可检索”；后台会为旧版已批准的“待索引”文档补排索引任务。删除立即停止读取及召回，后台清理该文档的 Milvus 实体并失效派生切片，保留原件、解析、审核和聊天档案；清理失败支持显式重试。管理员审计查询页面仍待后续开放。

新注册成员可以进行普通文字聊天并进入个人中心，默认无部门、职级、员工关联及业务权限。请线下联系管理员分配业务身份；系统没有提权申请入口。分配部门和职级后获得知识资格，业务查询还需单独开启。尚未授权的 RAG/SQL 请求返回 403；已授权 RAG 执行实际检索，SQL 仍返回未开放状态，不会提前执行业务查询。

## 启动方式

在项目根目录执行以下步骤。

知识上传默认一次最多 10 份、每份 20 MiB、批次 50 MiB；可在 `.env` 设置 `KNOWLEDGE_MAX_FILES`、`KNOWLEDGE_MAX_FILE_BYTES` 和 `KNOWLEDGE_MAX_BATCH_BYTES`。解析子进程默认 30 秒超时，PDF 最多 300 页、正文最多 100 万字符、页面渲染限制约 200 万像素。可配置超时 `KNOWLEDGE_PARSE_TIMEOUT_SECONDS`。扫描 PDF/纯图片文档暂不支持 OCR，受密码保护、损坏、危险 XML 或非支持格式会明确失败，不生成空知识。

切片总上限默认 600 字符，包含继承标题、标点和换行，可由 `RAG_CHUNK_SIZE` 在 100～600 范围内调整。规范 Markdown 与 DOCX 按标题树处理：一级章节不超限则保持整章，超过上限才按二级细分，二级仍超限再按三级，依此类推；每片开头写出完整祖先标题。Word 读取实际大纲级别与样式，缺少样式时结合规范编号重建层级。数据库与 Milvus 保存 `heading_path`（当前章节祖先链）和 `section_paths`（片内各小节路径），不会把兄弟节串成错误的父子链，也不把空标题单独入库。

PDF/无规范标题的全文按空行、换行、句末标点、分号、逗号、冒号、空格的优先级递归处理：相邻短块先合并，只有超长块降级拆分，最后才用字符长度兜底；不再固定重复 50 字正文。跨 PDF 页合并时保存全部 `page_numbers`，引用显示真实页范围并能预览各页。旧版切片保留为过期历史，索引策略版本为 2；已有有效批准文档自动排重建，原件、原审批和旧解析不覆盖，已撤回或删除条目不重新激活。

两路候选、重排和最终上限分别由 `RAG_RECALL_LIMIT`、`RAG_RERANK_LIMIT`、`RAG_FINAL_LIMIT` 控制；RRF 参数 `RAG_RRF_K` 默认 60，计算倒数排名和，不相加原始分数；语义阈值 `RAG_MIN_SCORE` 默认 0.3，是当前样例验证的过滤阈值，需按实际企业资料继续评估。

模型固定为 [中文 BGE small v1.5 的量化 ONNX](https://huggingface.co/Xenova/bge-small-zh-v1.5) 与 [BGE reranker base 的量化 ONNX](https://huggingface.co/Xenova/bge-reranker-base)，使用 CPUExecutionProvider、两个 CPU 线程，不安装 GPU 或 PyTorch。模型提交与每个文件的 SHA-256 在 `retrieval_models/model_lock.json` 锁定；首次启动 `models-init` 下载并校验到独立权重卷，后续复用缓存。可用 `MODEL_DOWNLOAD_ENDPOINT` 指定 HTTPS 镜像端点，文件哈希仍必须一致。权重与分词器合计约 306 MiB，不纳入 Git 或服务镜像。Embedding 空间记录模型提交、量化、CLS 池化和归一化协议，不同空间使用不同集合；更换模型不能直接查询旧空间，需要单独重建索引。

模型单窗口为 512 tokens；600 字切片超过窗口时完整处理全部窗口，不静默丢弃尾部。Embedding 聚合各窗口单位向量后再归一化，重排取各正文窗口的最高相关度。该协议使用独立的 `windowmean-v2` 空间，旧空间向量不混用。重排问题超过 256 tokens 时明确要求缩短问题。

本机先前对 20 份 360 字符片段实测，向量化约 0.48 秒、20 候选重排约 1.57～1.84 秒，常驻及峰值约 800 MiB；这是单窗口基线，不作为当前600字多窗口处理的性能承诺。完整检索还包括原件/PG核验、Milvus和文本模型，长文本及其他机器应重新测量。

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

文字对话需要配置主、备用和独立审核模型槽位，每个槽位只需以下三项：

```dotenv
PRIMARY_BASE_URL=https://api.deepseek.com
PRIMARY_API_KEY=your-primary-api-key
PRIMARY_MODEL=deepseek-flash
FALLBACK_BASE_URL=https://your-workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
FALLBACK_API_KEY=your-fallback-api-key
FALLBACK_MODEL=qwen-plus
REVIEW_BASE_URL=https://your-workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
REVIEW_API_KEY=your-review-api-key
REVIEW_MODEL=qwen-plus
```

千问地址和密钥必须属于同一工作空间，使用服务商实际提供的地址。更换兼容模型只修改对应三项，执行 `docker compose up -d --force-recreate api worker` 重新加载环境，无需改业务代码或重新构建镜像。未配置、凭证错误或模型故障都会明确失败。

本地REVIEW三项已独立复制当前千问备用配置，以后修改FALLBACK不会影响REVIEW，反之亦然；不使用环境变量动态回退。审核模型只需输出文本JSON，不调用工具。审核请求包含本轮可见轨迹、授权证据及后端真实工具记录，由既有外部文本网关发送，模型选择须满足企业资料处理要求；搜索关键词仍独立于内部证据。主备保持原3+3尝试，审核只用REVIEW槽位，瞬时故障最多两次，不切到主模型自审。审核失败会明确显示未完成核验；增加一次模型调用和可能的一次返工，仍共用原run时限及工具预算。

M06A 已接入阿里云 IQS 国内极速版（LiteBasic，接口值为 `CNLiteBasic`）。在 `.env` 的 `WEB_SEARCH_API_KEY` 填写 IQS 搜索服务的独立 API Key，搜索服务与主备文本模型分别配置。提供商和引擎首期固定为以下值；变更环境后重创建 `api worker` 加载，不重建数据库或模型权重。

```dotenv
WEB_SEARCH_PROVIDER=aliyun_iqs
WEB_SEARCH_BASE_URL=https://cloud-iqs.aliyuncs.com
WEB_SEARCH_ENGINE_TYPE=CNLiteBasic
WEB_SEARCH_API_KEY=
```

默认单次最多5个来源、尝试读取3个页面，搜索/单页总超时12秒、响应最多2MiB、页面文本快照最多2500字符，用户每分钟最多10次实际搜索，可通过 `.env.example` 的 `WEB_SEARCH_*` 限额调整。送入主回答模型的公开正文每来源最多650字符，保留截断标识和原快照。只在同一run内复用相同公开任务结果，不做跨用户或跨会话搜索缓存。关键词提炼会额外调用统一文本网关并记录用量。

网页读取在连接时固定已校验公网IP，每次重定向复检，不使用环境代理/Cookie。若本机代理将域名解析到 `198.18.*` 等非公网地址，直接读取会被拒绝，仍可使用提供商返回片段，界面会明确区分；不会放宽内网保护。未知日期保持空，模型和提供商不能保证每个问题都有足够证据；搜索不是任意语义秘密的自动识别系统。

可选策略见 `.env.example`：输出默认最多 2048 tokens；读取超时 45 秒；任务预算 240 秒；连续三次故障熔断 30 秒；worker 默认并发 2。上下文使用保守能力目录下限，已知协议默认 32768、其他兼容服务默认 8192；可通过 `PRIMARY_CONTEXT_TOKENS` / `FALLBACK_CONTEXT_TOKENS` / `REVIEW_CONTEXT_TOKENS` 分别指定服务实际支持的窗口，`0` 表示使用目录默认值。估算包含完整请求与输出预留，并留 20% 安全余量。审核轨迹不靠模型自述摘要，超过审核槽位窗口则明确交付审核限制，不截掉工具事实后声称已审完整轨迹。

文本预算按实际模型名选择分词器，不以字节数代替当前DeepSeek/千问的分词。DeepSeek词表约6MiB，版本/来源/哈希记录在 `backend/tokenizer_assets/manifest.json`，Docker构建时校验缓存或下载，运行时不联网；不安装Torch或GPU推理依赖。千问官方SDK的参考词表不保证与 `qwen3.8-flash` 云端完全一致，模型别名更新时需核对词表。请求JSON结构与服务端隐藏模板不同，完整请求预算仍为估算；每次请求审计保存预算值、方法及分词器标识，最终计费以实际usage为准。该改动不实现M10压缩，也不自动扩大上下文窗口。

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

默认 `docker compose up -d --wait --wait-timeout 300` 启动 API、worker、存储和 CPU 检索模型，不启动 `web`。首次下载权重较慢时将等待预算改为 `--wait-timeout 900`。API 仅发布本机 `127.0.0.1:8000`，通过 `.env` 的 `API_HOST_PORT` 修改；检索模型没有宿主发布端口。

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

常驻后端容器包括 `api`、`worker`、`postgres`、`redis`、`milvus`、`etcd`、`minio` 和 `retrieval-models`；生产额外启动 `web`。`minio-init`、`models-init` 完成后显示 `Exited (0)` 正常；`db-admin` 仅执行显式迁移和导入，不是常驻服务。检索服务使用只读权重卷，默认限制两个 CPU、3 GiB 内存。

`database/` 只读挂载给管理任务，不写入镜像，也不在应用启动时自动执行。迁移与种子有指纹回执；重复执行跳过已完成内容，改变已应用文件会拒绝导入，应新增文件表达变化。当前导入 `database/seeds_short_ids/`，旧版种子保留用于历史升级验证；已有账号不重置密码或授权。审计的关系字段使用短编号，原始 JSON 保留当时记录的编号与正文。

- 开发 Vite 或生产 `web` 提供同源页面与 `/api` 代理。
- `api` 接收 HTTP 请求，执行认证、权限和模型任务接口。
- `worker` 领取持久文本任务并调用模型，同一会话同时只允许一条回答；重复发送标识不会生成重复任务。
- `retrieval-models` 只执行中文向量与语义重排，不持有数据库、MinIO 或文本模型凭证；API/worker 经容器网络调用它。

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

`live` 只证明 API 进程存活；`ready` 会实际访问五项依赖，全部连通才返回 HTTP 200，任一不可用返回 HTTP 503。

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

第五阶段文件、审核及权限的完整回归：

```bash
sh scripts/verify-m05.sh
```

该验收使用独立 PostgreSQL、真实 MinIO、四种虚构原件与隔离解析进程，验证批准前无正式文档/切片、批准后待索引、ACL、原件变更、删除、失败回滚，并回归聊天和身份。浏览器在本地 `npm run dev` 上验收，不启动前端容器。

第六阶段完整验收（包含实际主备模型调用与服务商费用）：

```bash
sh scripts/verify-m06.sh
```

该脚本串行验证真实 ONNX CPU 模型、Milvus 向量/BM25、RRF、语义重排、当前 ACL、删除/旧索引排除、原件与空间版本、索引失败重试，以及实际 DeepSeek/千问的自主工具调用、普通直答和无证据回答，再回归文件、聊天、身份与数据库。测试库和集合只清理本次资源，报告放在忽略的 `verification/M06/`。浏览器使用本地 `npm run dev` 验证引用定位、PDF 原件、小屏和删除后失效，不启动前端容器。

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

M06A验收入口：`sh scripts/verify-m06a.sh` 执行4项边界测试、相关集成测试组及6项受影响回归；集成组覆盖真实RAG/公开/组合/直接回答、实际千问备用、故障状态，以及两路输入隔离和关键词生成后的轻量检查，使用独立测试库及检索集合，结果保存在本机 `verification/M06A/`。日常小改只运行受影响的目标用例，不重复整套M01～M06测试。浏览器脚本为 `scripts/verify-browser-m06a.ps1`，支持通过 `-Session <名称> -ReuseLoggedInSession` 复用已登录的隔离agent-browser会话；验收结束关闭本会话，登录配置由其创建者清理。

- **沙箱文件与代码分析**：会话上传 CSV/XLSX，在按任务隔离的沙箱里检查文件、运行模型生成的 Python/文件数据 SQL，返回真实统计、图表和产物；无应用凭证、无企业/外网访问，不能绕过业务数据库的确认和权限。当前尚未实现。
- **人工二次确认 Hook**：在工具执行器中实现执行前人工二次确认 Hook，共用动作快照、本人确认API、等待恢复及前端组件。SQL逐次查询、沙箱生成代码execute和对话知识提交按固定策略复用；模型不能跳过确认，用户同意不能扩大数据权限或沙箱权限。未来邮件/消息可展示收件人和完整待发内容后确认，首期不实现发送工具。
- **业务数据权限**：已有工具目录过滤和知识/附件资格校验；M07再落实业务SQL表行列和本人数据约束。业务SQL当前未实现，仅有资格入口和数据库只读/系统表隔离，不能声称已实现本人行权限。人工二次确认Hook契约名为HumanSecondaryConfirmationHook，在模型选择工具后、工具或内部高危动作执行前触发，不代替权限检查。
- **业务数据查询**：Text to SQL、只读与数据范围校验、经人工二次确认 Hook逐次确认及真实结果图表。
- **工作记录整理**：对话转知识、纯文本会议纪要、文稿预览和下载。
- **上下文与长期记忆**：可重建的短期缓存、上下文压缩、按用户隔离的自动后台记忆。
- **审计工作台与任务恢复**：基于已建立的永久审计，实现管理员只读调试、后台任务恢复、备份与恢复验证。

发布前答案审核的针对性验收（4个契约/故障用例及1个真实模型用例，真实用例含复合检索和普通计算两轮）：

```powershell
docker compose --profile tools run --rm --no-deps --entrypoint python -v "${PWD}/backend/tests:/tests:ro" -v "${PWD}/verification:/verification" -e PYTHONPATH=/app:/tests db-admin -m unittest test_answer_review -v
```

真实用例仅使用独立测试库内明确虚构的制度及公开IQS资料，不读取主库。结果保存在 `verification/answer-review/`；失败夹具覆盖返工上限、引用误放行、审核配置/鉴权/协议/重试和取消，不能当作真实模型成功证据。日常局部修改选择受影响的方法执行。

M06A及发布前独立答案审核已实现；后续实施顺序为 M07 人工二次确认 Hook与业务SQL → M07A 沙箱分析 → M08 对话转知识 → M09 文本纪要 → M10 上下文压缩 → M11 自动记忆 → M12 审计与交付回归。新增能力复用现有工具循环、worker、审核入库和文件预览；基础权限、结果限额、执行预算及失败状态随各阶段实现。

沙箱须在实际支持隔离后端的目标节点验收，Python及一种文件SQL引擎运行在无网络、无应用凭证的任务实例内。固定文件inspect自动检查，生成代码每次execute经人工二次确认 Hook展示文件/完整代码后确认，修改代码重新确认。所有待确认动作释放worker槽；执行后结果未知不自动重跑，再执行须新确认。知识提交复用一次文稿/ACL确认，管理员审核独立保留。文本纪要将支持本人会话资料附件，无需先发布知识库；摘要/长期记忆不能成为撤权后的企业正文入口。最终恢复验证采用维护窗口备份PostgreSQL和应用文件、重建派生索引，不新增独立调度或运维平台。这些均为后续交付约束，当前尚未实现。

自动化任务流当前仅保留导航入口，具体配置与执行能力留待后续版本。每次更新代码时同步维护本 README 中的当前功能、启动步骤及待实现功能，使说明与实际版本一致。
