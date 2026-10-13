# 知行 · 企业智能助理

面向企业员工的浏览器工作台，用于集中处理企业知识、业务数据和工作记录。当前M11版本增加全自动个人长期记忆：每轮注入本人偏好与项目目录，项目详情由独立工具按需混合检索。已具备Redis工作上下文与可靠压缩、多 worker/多 Runner 协同调度、共享资源额度和排空缩容，继续支持通用附件预处理/详细读取、自动沙箱计算/文件生成和持久排队、真实账号、职员权限管理、主备模型对话、独立模型答案审核、知识提交与审核、完整混合检索、公开搜索、敏感动作人工二次确认、受限Text to SQL及真实结果表格/图表、来源预览和永久审计。

M07B 已实现 TXT/MD/CSV/XLSX/DOCX/文字PDF、本轮新附件强制预处理、read_attachment 分页读取、自动代码执行、真实文件下载和 PG 沙箱排队。附件随用户消息发送，整个会话后续可继续读取原件；未发送草稿不进入模型。原 M08 对话转知识与专用纪要工具取消；会议整理通过通用能力完成。只有业务数据库 SQL 继续触发人工二次确认。M07C 已实现同一 Linux Docker 引擎上的多副本调度，正常满额排队后自动继续，未知执行结果不自动重跑。

## 当前功能

- **工作台界面**：提供对话工作区、历史会话区域、知识库管理、自动化任务流与个人中心的导航入口；支持桌面与小屏布局。
- **知识提交与审核**：支持 PDF、DOCX、TXT、Markdown 单份或批量提交，统一选择允许部门和最低职级，每份文件独立显示结果。普通成员待管理员审核，管理员确认后直接进入同一后台处理队列；审核者只能通过或驳回，不能暗改 ACL。
- **真实原件预览**：原件存储在应用专属 MinIO Bucket，后台隔离解析并保存不可变预览。PDF 支持逐页图片、文字和 50%–250% 缩放，DOCX 保留段落/表格顺序；TXT、Markdown 安全文本展示，不执行脚本或访问外部链接。每次预览、翻页和下载都重新鉴权。
- **知识权限与维护**：普通成员只管理本人材料。创建者不满足自己选择的 ACL 时只能看管理信息，不能读取正文；管理员查看全部材料。支持撤回、删除和受控重试，原件或 ACL 变化使批准失效，晚到后台任务不能重新发布删除的材料。
- **完整混合检索**：批准后的原件按页码/标题切片，使用真实中文 CPU Embedding 与 Milvus 向量、中文 BM25 两路召回，按切片编号去重后 RRF 融合，再由独立交叉编码器语义重排。默认两路各 20、重排最多 20、最终最多 5 个；低相关不凑数，编号和日期使用结构过滤。
- **自主知识工具**：LangGraph 运行模型→知识工具→观察→回答循环，文本模型自主选择是否检索。内部事实使用当前合法证据，普通问题可以直接回答；保存真实工具调用、两路候选、融合排名、重排和来源证据。无权限在召回前拒绝，无证据不能编造内部规定。
- **公开联网搜索（M06A）**：使用阿里云 IQS 国内极速版 `CNLiteBasic`，模型自主选择内部RAG、公开搜索、组合查询或直接回答。每次模型请求按当前身份过滤工具目录，未授权知识账号仍能公开搜索；重试/备用与工具执行重新核验资格。RAG与公开搜索独立调用，只有最终回答汇总证据；搜索服务与关键词模块只接收用户本次输入及可信身份/任务，不接收或比对RAG、SQL、附件正文及历史。主Agent候选仅存本地工具审计，不影响外发生成。输入和生成词保留轻量凭证/明显业务明细检查；混合任务提取明确公开子句，模糊或含私密载荷时保守拒绝。
- **公开来源预览**：返回网页标题、URL、可得页面日期、北京时间获取时间与内容类型，右侧只展示本人持久文本快照，不嵌网页/图片/脚本。区分搜索摘要、提供商正文片段及实际页面文本；没有读取页面时不称已读原文。每次访问来源重新鉴权，搜索失败/无结果/截断真实说明。
- **业务SQL与敏感动作确认（M07）**：有业务资格者可调用query_business_data，工具内部先生成/校验SQL，到真正查询前触发HumanSecondaryConfirmationHook。确认窗口展示冻结目的、数据库/schema、对象、实际范围、完整SQL/参数；只有允许本次操作才执行，关闭不算同意。通用Hook也能展示其他动作的目标/完整载荷，当前没有邮件/消息发送功能。
- **数据库范围与可视化**：只读账号仅能访问受保护的visible_*视图，底表权限撤销；有效执行能力必须来自已消费批准。成员员工/考勤仅本人，工单/审批再按部门职级和本人关联限制，管理员可查询允许的模拟全量但仍逐次确认。AST限定表列/函数/单条只读查询；默认10秒、最多200行，截断明确。结果来源按钮打开本人受鉴权的真实表格及柱/线SVG图表，不执行模型生成的前端脚本。
- **技术部共享代码提交记录**：新增 `code_submissions` 与受保护的 `visible_code_submissions` 视图。技术部中具备业务查询资格的员工（包括zhangsan）可读取全部作者和全部记录，不限本人、不附加职级门槛；管理员也可查。模拟数据包含2026-09-07～09-30的18个工作日、6名作者、4个仓库、207次提交，涵盖分支、版本、提交说明、改动量、PR、评审与构建状态。全表读取是权限上限，不要求每次查询全表；可用WHERE仅查本人或指定作者。实际执行SQL/参数随回执提供给主模型与审核，避免把本人结果误称全员。支持DATE/DATE_TRUNC日期聚合及按日期/作者/仓库筛选汇总并生成柱状图或折线图；查询仍须人工确认。代码平台作者不必都有本系统登录账号，统计全部作者时不联接仅本人可见的员工视图。
- **持久等待与恢复**：LangGraph在具体敏感操作边界interrupt，使用现有PG检查点表和异步适配器保存状态；等待释放worker槽，刷新恢复待确认窗口，默认30分钟有效。同意/拒绝回执幂等，批准消费与领取原子记录；已保存结果复用，结果未知不自动重发SQL，重新查询须新提问/确认。撤权、退出、取消或载荷变化使旧批准无效。等待时间不计入主动执行预算，主动时间跨恢复累计。
- **可核实来源**：回答附文件、页码或片段引用，点击打开右侧来源面板，并可逐页预览或下载原件。来源接口每次检查当前文档状态、批准、部门职级和版本；删除后旧引用停止正文访问，原聊天记录保持完整。
- **聊天展示**：智能体回复按Markdown渲染标题、加粗、列表、引用、代码块和表格，历史与分段回复使用同一渲染组件；用户输入仍保留原文，来源按钮保持可点击。原始HTML不执行，外部图片以链接展示，宽表格与代码块独立横向滚动。普通消息卡片不展示模型型号和 token 用量，相关信息由后端审计保存。输入框与发送/停止按钮分行留出间距；文字能力与回答核实提示统一放在底部状态栏，与运行状态入口同排。
- **真实文字对话**：后台调用模型，待发布正文先缓冲，核验后分段展示；支持多轮、停止回答、刷新恢复、本人历史打开与搜索、重命名。原消息和执行事件永久保存在 PostgreSQL，SSE 断线按序补发，不重新调用模型。
- **发布前答案审核**：LangGraph增加独立review/publish节点，所有待发布答案（含零工具直接回答）经过核验。后端按PG记录核对调用/结果配对、终态和引用；审核模型检查任务完成度、执行真实性、证据一致性与修正可行性。最多一次返工、再次审核；证据不足、二次驳回或审核不可用仅交付服务端资料摘录和限制，不发布未通过草稿。等待时展示真实执行/审核状态；这不能保证消除所有模型错误，也不能替代权限及人工二次确认。
- **统一模型网关**：主、备用和审核槽位各使用 URL、API Key、模型名接入。默认 DeepSeek 主模型、千问备用及独立千问审核；适配公开思考参数及流式用量，业务代码不直接调用厂商接口。
- **失败与降级**：主模型总尝试最多三次，仍失败切备用，备用最多三次；凭证和参数错误直接停止该槽位的重试。包含退避、初始熔断与半开探测，全部失败明确结束，不把半截回复保存为完整回答。
- **上下文与用量**：模型实际收到本人已完成轮次和本次输入，保留原始请求、流式包及合并回复。每个槽位独立检查窗口和输出预留；达到窗口75%时有界分段/增量压缩旧历史，近期默认保留四完整轮次；当前问题与工具配对保留，压缩后仍无法容纳则明确停止。供应商用量标为 reported，无供应商用量时明确估算方法，失败时未知数值为空；流式累计用量不重复相加。
- **模型分词预算**：DeepSeek Flash使用官方V4.1参考分词器，当前千问备用型号使用DashScope官方Qwen参考分词器；在本地计算文本及消息/工具预算，不每次联网计数，不下载推理权重。保留聊天格式预留与可配置安全余量（默认5%），本地请求总数标estimated，不能冒充云端完整模板或计费用量。未知型号才回退字节上界；Embedding/重排保留各自真实分词器，供应商实际usage优先，不改旧审计记录。
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

新注册成员可以进行普通文字聊天并进入个人中心，默认无部门、职级、员工关联及业务权限。请线下联系管理员分配业务身份；系统没有提权申请入口。分配部门和职级后获得知识资格，业务查询还需单独开启。未授权工具不出现在模型可选目录；伪造调用仍由后端拒绝。已授权RAG执行实际检索，SQL仅在具体敏感查询之前取得本次确认后执行，确认不扩大用户数据范围。

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

可选策略见 `.env.example`：当前环境和样例单次输出最多8192 tokens，未配置时默认4096（兼容较小窗口）；读取超时45秒；任务预算240秒；连续三次故障熔断30秒；worker默认并发2。2026-10-12按官方说明核对 deepseek-flash、qwen3.7-flash（含2026-07-15快照）、qwen3.8-flash 的100万token窗口；`PRIMARY_CONTEXT_TOKENS` / `FALLBACK_CONTEXT_TOKENS` / `REVIEW_CONTEXT_TOKENS` 分别覆盖各槽位窗口，`0`使用型号目录，未知型号仍使用保守值（已知协议32768、其他兼容服务8192）。改用小窗口模型时必须同步窗口和输出预算。`MODEL_CONTEXT_SAFETY_RATIO=0.95`统一控制历史组装、工具循环和网关（含审核）的预算，输入/工具定义/输出预留合计不超过窗口95%；100万窗口、8192输出预留下，输入预算约941808 tokens。该余量是应用策略，不是供应商限制或精确计数保证。审核轨迹超过其窗口时明确交付审核限制，不截掉工具事实后声称已审完整轨迹。

文本预算按实际模型名选择分词器，不以字节数代替当前DeepSeek/千问的分词。DeepSeek词表约6MiB，版本/来源/哈希记录在 `backend/tokenizer_assets/manifest.json`，Docker构建时校验缓存或下载，运行时不联网；不安装Torch或GPU推理依赖。千问官方SDK的参考词表不保证与 `qwen3.8-flash` 云端完全一致，模型别名更新时需核对词表。请求JSON结构与服务端隐藏模板不同，完整请求预算仍为估算；每次请求审计保存预算值、方法及分词器标识，最终计费以实际usage为准。已核对型号使用上述窗口目录，M10已接入分段/增量压缩，原始消息和执行结果保持不变。

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

## 本机查看 MinIO 文件

Compose 将 MinIO Console 映射为 http://127.0.0.1:9001，仅本机可访问；9000 S3 数据接口仍位于容器内部网络。修改配置后运行 `docker compose up -d --no-deps minio` 应用映射，不重建镜像、不删除数据卷。

使用 `.env` 的 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` 登录，在 Object Browser 浏览存储桶。应用桶由 `APP_MINIO_BUCKET` 指定，默认 `application-files`：`knowledge/` 保存知识原件，`analysis/` 保存会话附件，`sandbox/` 保存已生成产物。对象可能以编号命名，原始文件名/对象键对应关系见 PG 的附件/产物元数据，下载后按原格式打开；MinIO 不是可直接按 SQL 查看行列的数据库。Milvus 的内部存储桶保持独立。

## M07B 通用附件、自动执行与排队

会话支持 TXT/MD/CSV/XLSX/DOCX/可提取文字 PDF。待发送文件显示在上传入口上方，无复选框；发送后输入文字/文件一起清空，用户消息显示文件名称和大小。原件存入应用 MinIO 的 `analysis/{user_id}/{attachment_id}`，PostgreSQL 保存所有者、指纹及首次发送消息归属。后续每轮提供本会话已发送文件目录及原消息定位，Agent可按需读取/转换，不需要重新上传或勾选；未发送/移除的草稿不可进入模型。attachment_prepare 仅固定解析本条新发送文件，提供格式、长度/结构或列/行数、真实样例及覆盖范围；普通后续轮次直接进入主模型。read_attachment 按字符/页或工作表行列分页，宽表可继续读列；不截断单元格后声称完整。CSV URL与XLSX普通超链接作为字符串保留，解析器不访问目标；宏/公式执行、外部内容加载仍受限制。解析文字上限200万字符/PDF300页，详细文字每次12000字符、表格每次20行/20列，可继续读行列；超限明确提示。普通文字/会议整理可直接由模型返回，不独立注册纪要或CSV工具。

run_sandbox_code 自动完成临时计算、多文件联查、清洗、柱/线/热力图、文档转换/修改和实际文件生成，不再弹沙箱确认。输入原件只读，支持可为空的附件列表；结果可为文字、表格或 TXT/MD/CSV/XLSX/DOCX/PDF/PNG 下载。业务PG仍只通过 query_business_data、当前数据权限及本人确认访问。旧M07A批准/审计留存，不生成新的文件批准。

任务镜像构建时预装 NumPy/pandas/openpyxl/DuckDB/SQLGlot、Matplotlib/Seaborn/Pillow/中文字体、python-docx/pypdf、Pandoc/LibreOffice；可用 convert_document 完成 MD→DOCX→PDF。依赖只进入镜像，运行时断网、不安装包。每次预处理/读取/代码运行都是新普通Docker容器，非root、只读根目录/本任务输入、cap-drop=ALL、默认seccomp及no-new-privileges；2CPU/1GiB/64进程、60秒、输出256MiB、日志64KiB、产物20MiB。完成/失败/取消清理实例/临时输入，保留原件和持久回执。

PG sandbox_jobs 保存有限参数/文件引用、指纹、领取版本、Runner归属、取消和回执；不保存整份Base64文件。worker 提交后保存LangGraph检查点，以 waiting_sandbox 释放执行槽。Runner经独立令牌认证的内部API有额度才领取任务，API读取并复验MinIO输入；Runner不持有应用PG/MinIO凭证，任务容器也没有控制令牌。回执及真实产物保存后Agent自动恢复。正常并发满额等待，不要求重新发问；排队可取消、积压/最长等待有界，重复通知不多执行，结果未知不盲目重跑，代码尝试最多3次。

当前默认2个Runner、每实例1槽/全局2槽/每用户1槽；SANDBOX_RUNNER_REPLICAS、SANDBOX_RUNNER_CONCURRENCY、SANDBOX_GLOBAL_CONCURRENCY、SANDBOX_USER_CONCURRENCY、SANDBOX_QUEUE_LIMIT（默认50）、SANDBOX_WAIT_SECONDS（默认300）可配置。全局、实例与用户额度以PG为准，Runner本地Semaphore只作保护；参数改变后重创建相关服务。节点重启只清理本runner_id归属实例，跨主机部署未经实测，不据有限验收宣称无限扩容。

只有可信Runner挂载Docker socket；api/worker/任务容器无socket，模型不能选择镜像、宿主路径或网络。Runner控制接口只在内部网络，旧POST /jobs诊断入口仅供持控制令牌的验收驱动，正常Agent走PG队列；固定任务和资源模板相同。普通Docker共享宿主内核，本阶段不安装gVisor或使用Windows现有Python执行用户代码。

Linux服务器首次部署（宿主只需Docker和Compose；模型/搜索密钥在.env填写）：

```bash
sh scripts/init-env.sh
sh scripts/build-sandbox.sh
docker compose --profile production build
docker compose up -d postgres minio redis
docker compose --profile tools run --rm db-admin migrate
docker compose --profile tools run --rm db-admin seed
docker compose --profile production up -d --wait --wait-timeout 300
```

已有M07A数据升级应用新增011迁移，首次部署依序应用全部迁移，不修改旧迁移/种子或删除数据卷。已有.env缺少沙箱配置时补充SANDBOX_ENABLED=true、SANDBOX_RUNNER_URL=http://sandbox-runner:8091及独立SANDBOX_CONTROL_TOKEN；初始化脚本保留旧凭证。Windows开发使用相同容器镜像，前端继续在frontend执行npm run dev，不需要Ubuntu、gVisor或自定义宿主运行时。支持范围首先以本次实际linux/amd64验收为准，其他架构需构建对应镜像并验收。

验收入口 `sh scripts/verify-m07b.sh`，仅六个本阶段目标方法（含四条实际模型任务）和四个受影响SQL/审核守卫，报告在忽略的 `verification/M07B/`。另有一次本地Vite页面上传/后台自动领取/实际下载验收。M07A其他资源/隔离证据未受影响时引用原记录，不重复压力矩阵。

附件增量修复使用 `012_message_files.sql`，旧附件关联到首次实际发送消息，保留历史正文与引用。定向回归 `test_attachment_regression.LinkFiles.test_links_are_inert_strings` 和 `test_attachment_regression.ConversationFiles.test_sent_files_survive_other_turns` 验证超链接、未发送草稿不可见及真实模型跨轮转换原件；浏览器检查概况/全文/长执行输出展开后聊天与面板宽度稳定。

## M07C 多 worker、多 Runner 与排空缩容

版本0.7.3。worker共享Agent/知识队列，以随机worker_id和PG租约原子领取，两个队列有界交替；不绑定某个Runner。默认2个worker、每实例2槽。Runner以容器HOSTNAME取得独立runner_id，每次启动生成instance_id，注册心跳/镜像/解析器能力后才领取。沙箱全局/节点/单用户额度在PG同一事务中判断；满额保持queued，释放容量后自动继续，不返回“沙箱忙碌”来取消正常任务。

新增013迁移保存sandbox_nodes及资源释放状态。控制服务先独占节点身份，再仅清理本归属孤儿；节点失联标unknown，保留可能仍在占用的额度，直到原节点确认关闭或受控恢复入口实际清理。旧代次/旧租约回执不能覆盖终态，重复通知不生成第二份结果。业务SQL依然必须人工确认，多worker不会重新执行未知SQL。

同一Linux Docker引擎部署或升级：

```sh
docker compose --profile tools build api sandbox-runner sandbox-image
docker compose --profile tools run --rm db-admin migrate
docker compose up -d --wait
```

实例数由.env的WORKER_REPLICAS/SANDBOX_RUNNER_REPLICAS配置，或临时显式扩容：

```sh
docker compose up -d --no-deps --wait --scale worker=3 --scale sandbox-runner=3 worker sandbox-runner
```

增加Runner副本不会自动增加全局预算；需要更多实际任务并行时同步设置SANDBOX_GLOBAL_CONCURRENCY及SANDBOX_USER_CONCURRENCY，再重建相关服务配置。保持SANDBOX_RUNNER_ID留空，让每个副本使用自身身份；复制同一固定节点名会被注册接口拒绝。SANDBOX_RUNNER_URL保留用于可信诊断，实际任务调度及能力发现使用共享PG节点目录，不依靠DNS选择执行节点。

缩容发送SIGTERM后先停止领取，worker最多等待WORKER_SHUTDOWN_SECONDS（默认60秒），Runner最多等待SANDBOX_SHUTDOWN_SECONDS（默认70秒），继续为活动任务续租并保存结果；超时明确中断/未知并清理，不静默重放。Compose分别提供75/110秒停止宽限；增加等待参数时也要调大stop_grace_period。

```sh
docker compose up -d --no-deps --wait --scale worker=1 --scale sandbox-runner=1 worker sandbox-runner
```

节点崩溃先重启原控制器，由它复核/清理自己的孤儿。若原控制器已经被删除，用明确的旧runner_id运行一次恢复；该入口拒绝仍在心跳的活节点，只关闭旧归属，不领取任务、不重跑代码：

```sh
docker compose run --rm --no-deps -e SANDBOX_RUNNER_ID=旧节点编号 --entrypoint python sandbox-runner /app/recover_node.py
```

内部鉴权的/internal/sandbox/status提供有限节点/容量/队列指标，不包含代码/文件/凭证；审计UI留在M12。验收入口sh scripts/verify-m07c.sh：同批10个固定沙箱任务在双/单Runner分别执行，双节点峰值2执行/8排队、单节点峰值1执行/9排队，均10/10完成且结果一致。另验证两真实主/审核模型任务及跨worker接续、运行/排队取消、节点重启不误清理、过期节点恢复、旧回执和SQL租约守卫；测试使用独立PG与虚构文件，报告在verification/M07C/REPORT.md，不重复格式/压力矩阵。可信验收驱动为创建/重启临时副本持有Docker socket，普通api/worker与用户任务客体没有该socket。

Windows可以直接在项目根目录执行相同的Docker验收入口，不需要宿主安装Python测试依赖：

```powershell
docker compose --profile tools run --rm --no-deps --user 0:0 --entrypoint python -v /var/run/docker.sock:/var/run/docker.sock -v "${PWD}/backend/tests:/tests:ro" -v "${PWD}/verification:/verification" -e PYTHONPATH=/app:/tests db-admin -m unittest test_m07c.Scaling test_m07c.RecoveryFence.test_expired_confirmed_run_claimed_once test_m07b.FileFlow.test_changed_input_unknown_and_attempt_budget test_m07.M07Flow.test_wait_approve_reject_isolation_and_exact_payload test_answer_review.ReviewFlow.test_backend_rejects_false_approval -v
```

实测为Docker Desktop的Linux/x86_64引擎，20逻辑CPU、约5.79GiB引擎内存；每个实际任务仍限制2CPU/1GiB/64进程，批次容量设2。仅验证同一引擎上的有限副本，跨主机/其他架构/任意规模未验收。

### 在命令行实时观察并发

实时入口为scripts/observe-m07c.ps1，Python实现在backend/tests/observe_m07c.py，复用test_m07c.py的隔离数据库与真实容器基础设施。与固定代码的沙箱容量验收不同，此入口默认提交10条真实模型对话，通过2个真实worker、2个Runner执行，使用现有主/审核模型配置。

```powershell
# 在项目根目录打开PowerShell运行，输出立即刷新，不必等待结束。
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\observe-m07c.ps1

# 先看两条任务，或放慢客体执行以观察排队（每次执行额外等待5秒）。
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\observe-m07c.ps1 -Tasks 2
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\observe-m07c.ps1 -Tasks 10 -TaskDelaySeconds 5
```

运行前主Compose服务需已启动。无需宿主Python依赖，脚本在Docker中执行；此模式真实调用配置的主/审核模型。日志显示A01等对话编号、J01等沙箱任务编号、真实Worker1/2身份、共享队列提交、Runner1/2领取、任务容器创建/运行/实际销毁、主/审核模型阶段、当前Agent/沙箱等待及执行列表，最后下载真实CSV核对数值并汇总。领取/恢复从PG审计读取，排队列表从PG状态读取，容器从Docker读取；不打印凭证、代码、文件正文或隐藏思考。启动心跳身份未绑定时先显示实际worker ID，绑定后输出对应名称；领取审计会补读，不猜测归属。

Worker不会直接选定Runner；输出明确展示“worker→共享沙箱队列→空闲Runner领取”。同一次对话可能产生附件预处理、详细读取和代码执行多个J任务，不把10条对话误称为仅10次容器执行。只在队列变化时打印列表，Ctrl+C会停止本次测试并清理临时环境，正式数据与服务保留。日志保存为verification/M07C/live-console-时间.log，数值及峰值证据保存为verification/M07C/run_demo.json。

## 未来待实现功能

新阶段详见本地 docs/PRD.md、docs/DEVELOPMENT_PLAN.md 和 docs/ATTACHMENT_RUNTIME_DESIGN.md。文档按现有约定不随仓库分发；本节同时保存可随仓库阅读的实施摘要。

M06A验收入口：`sh scripts/verify-m06a.sh` 执行4项边界测试、相关集成测试组及6项受影响回归；集成组覆盖真实RAG/公开/组合/直接回答、实际千问备用、故障状态，以及两路输入隔离和关键词生成后的轻量检查，使用独立测试库及检索集合，结果保存在本机 `verification/M06A/`。日常小改只运行受影响的目标用例，不重复整套M01～M06测试。浏览器脚本为 `scripts/verify-browser-m06a.ps1`，支持通过 `-Session <名称> -ReuseLoggedInSession` 复用已登录的隔离agent-browser会话；验收结束关闭本会话，登录配置由其创建者清理。

- **后续隔离增强**：当前沙箱采用普通Docker容器，共享宿主内核；gVisor/runsc或微虚拟机属于未来可选增强，本阶段不安装、不要求，不将普通容器宣称为同等强隔离。
- **后续敏感动作扩展**：未来邮件/消息等真实外部发送可使用 Hook 展示目标和完整待发内容，本轮不实现发送工具。本人确认不能扩大权限，自动沙箱执行也不能访问真实业务数据库。
- **上下文与长期记忆**：可重建的短期缓存、上下文压缩、按用户隔离的自动后台记忆。
- **审计工作台与任务恢复**：基于已建立的永久审计，实现管理员只读调试、后台任务恢复、备份与恢复验证。

发布前答案审核的针对性验收（4个契约/故障用例及1个真实模型用例，真实用例含复合检索和普通计算两轮）：

```powershell
docker compose --profile tools run --rm --no-deps --entrypoint python -v "${PWD}/backend/tests:/tests:ro" -v "${PWD}/verification:/verification" -e PYTHONPATH=/app:/tests db-admin -m unittest test_answer_review -v
```

真实用例仅使用独立测试库内明确虚构的制度及公开IQS资料，不读取主库。结果保存在 `verification/answer-review/`；失败夹具覆盖返工上限、引用误放行、审核配置/鉴权/协议/重试和取消，不能当作真实模型成功证据。日常局部修改选择受影响的方法执行。

M11 已实现，后续为 M12 审计与交付回归。原 M08/M09 阶段已删除，不复用原编号；自动任务流仍待后续版本。

M07验收入口：`sh scripts/verify-m07.sh`，仅运行5个本阶段目标组、1个实际模型/PG用例及2个受影响的审核/RAG守卫。真实用例只使用独立测试库内外置虚构种子，输出在本地 `verification/M07/`。升级现有部署先执行 `docker compose --profile tools run --rm db-admin migrate` 应用008/009增量；新增代码提交模拟数据需执行 `docker compose --profile tools run --rm db-admin seed`（只导入未应用的005种子，已应用种子跳过），再更新api/worker；不修改旧迁移/种子、不重建数据卷。

当前文件代码自动执行，业务SQL确认保留；首次附件概况与详细读取、真实执行和产物均参与事实/独立模型审核。publish仅作最终权限/引用/交付检查，不再因无RAG引用覆盖有效计算/转换答案。Word首期指DOCX，PDF只提取文字，不接图片/音视频/OCR、旧DOC、宏/加密文件；复杂PDF表格或排版不能保证完全还原。应用/编排版本0.11.2，沙箱客体/Runner沿用0.7.2/0.7.3；本地验证使用Docker Desktop的Linux amd64引擎。

自动化任务流当前仅保留导航入口，具体配置与执行能力留待后续版本。每次更新代码时同步维护本 README 中的当前功能、启动步骤及待实现功能，使说明与实际版本一致。

### 大附件与模型上下文的边界

20MiB是单附件上传字节上限，不是全文可输入模型的保证，DOCX/XLSX/PDF解包或抽取后可能显著增大。当前固定解析器限制文本200万字符、PDF300页、表格100万单元格或20万行；超过解析限制会说明失败，不自动截尾后声称已读完。首次预处理：文本不超过1800字符提供全文，长文本提供长度/结构及前1400字符；表格提供工作表、列类型/缺失量及前三行样例。大量工作表的概况仍可能变大，目前没有全局概况token裁剪。详细工具分页读取文本最多12000字符或表格20行/20列；代码可在沙箱直接处理原件，只把有界结果和产物回执传给模型。M10可压缩工作上下文和已取得的观察正文，但不等于自动读取超大文档全文；没有全文检索索引或整份文件分块递归总结工具。压缩后当前问题/协议仍超窗会明确停止，不保证任意20MiB文档的完整总结。

## M10 Redis工作上下文与可靠压缩

消息/任务/工具原件/确认与LangGraph检查点仍以PostgreSQL为准。每轮先读取当前用户/会话的消息计数、末序号及权限版本；Redis工作副本版本一致才命中，失效/不可达回PG重建，不缓存身份、批准或执行资格。缓存按数据库/用户/会话/权限隔离，TTL默认1800秒，单副本最多2MiB；超过大小跳过缓存。Redis总缓存上限256MiB、allkeys-lru淘汰，容器512MiB；清空缓存不丢历史。大历史超过缓存额度时仍会读PG，不承诺所有请求都更快。

完整消息/工具定义与输出预留达到窗口75%时压缩，最终安全线仍95%。默认目标保留最近四完整轮次，可按剩余窗口减少；当前问题不压缩，旧内部知识/业务派生轮次整轮排除，旧附件只保留指代，事实重新读取。摘要以facts/constraints/decisions/unfinished/caveats结构生成：姓名/日期/金额/约束/待办必须忠实，所有片段完成且格式/预算合格才在context_summaries追加版本。原消息编号、来源指纹、权限版本、模型与分段数可审计；可验证旧前缀时增量处理新增旧轮次，跨Worker从PG/Redis复用。摘要不是无损编码，也不作为新授权或执行事实。

长附件概况及已完成工具观察可生成供主模型使用的压缩视图，调用ID/参数/供应商必需字段保留，原观察和图状态不改；后端另外保留真实产物/附件/覆盖定位，摘要不能把样例写成已读全文。待确认动作及排队任务按原PG/checkpoint接续，不从摘要恢复或重复执行。审核仍核对原始轨迹和完整持久事实，只压缩旧对话；审核事实本身超窗则明确受限交付。摘要失败/片段超过16段/当前输入无法容纳时明确停止，不发布半份摘要。

参数：CONTEXT_COMPRESS_TRIGGER_RATIO=0.75、CONTEXT_KEEP_TURNS=4、CONTEXT_SUMMARY_TOKENS=2048、CONTEXT_COMPRESSION_MAX_CHUNKS=16、CONTEXT_CACHE_TTL_SECONDS=1800、CONTEXT_CACHE_MAX_BYTES=2097152。百万窗口通常在完整预算约75万token时触发，不要求用户手动新建对话。

更新方式：docker compose build api；docker compose --profile tools run --rm db-admin migrate；docker compose up -d --no-deps --wait redis api worker。前端继续本地npm run dev，不启动production容器。

验收入口：Windows运行 `powershell -ExecutionPolicy Bypass -File .\scripts\verify-m10.ps1`，Linux运行 `sh scripts/verify-m10.sh`。只验证真实PG/Redis版本命中/失效/故障降级/隔离、长历史与长观察预算/工具配对/小备用窗口/摘要失败不发布，以及一条真实DeepSeek压缩→回答→独立千问审核。报告放verification/M10，独立库与缓存结束后清理。

### 模型前缀缓存与输出截断

固定系统规则保留首部，当前时间和动态附件目录移到本轮用户消息末尾，避免它们在短系统前缀处打断较早的长历史。同一run重试/跨Worker接续沿用PG创建时间。权限过滤、摘要更新和动态工具目录仍可能改变前缀，供应商缓存是best-effort，不保证100%命中；Redis工作缓存与供应商KVCache不是同一层。当前输出8192是主动max_tokens上限，不是模型能力上限或实际必耗量；达到长度限制时主Agent明确失败并保留响应审计，不发布截断助手消息，不自动续写。

## M11 自动个人长期记忆

0.11.2个人记忆整理：保留动态key-value，候选提取后让主备模型读取本人全部画像/偏好，生成add/update/ignore。update可以只改value、重命名key，或明确合并多个旧key；没有预设偏好类别白名单。后端校验旧key、目标冲突、来源、重复操作和版本；锁外调用模型，短锁提交，版本变化最多重新整理三次。忽略重复不增加有效快照，但保存确认水位避免晚到旧任务改回。017迁移新增不可变app_memory.consolidations回执及state.personal_watermarks；旧候选/快照保留，整理失败队列有限重试。城市与回复语言的程序投影随原话来源保留，key改名也不会破坏附近搜索和默认语言。

修正版本0.11.1：明确自述画像字段开放（如体重/身高/技能，保存单位和来源时间）；具体生成作品的语言/功能也作为任务主题。代码数组下标不作为引用，重新生成代码不要求保存过原代码。实际搜索词以工具回执query为准；本地指代可补本人城市，其他画像/内部正文不外发；返工交付原任务。016增量迁移允许按提取策略版本有界补提取，旧候选和快照不修改。反馈回归入口backend/tests/test_m11_feedback.py。

每个成功回复与后台extract作业在同一PG事务保存。既有Worker轮换领取聊天、知识和记忆任务，共用并发额度；模型只从新增本人原话提取带连续证据的画像、稳定偏好和项目事实，模型读取全部现有个人记忆提出新增/更新/忽略方案，后端按真实消息顺序和版本提交；项目主题沿用原聚合流程。无有效信号只保存提取回执，后台不生成聊天回复、不递归分析自己的输出；暂时要求、转述/引用、附件/网页/代码结果及企业RAG/SQL正文不作为个人提取输入。提取主备模型继续走统一网关，模型请求、响应、usage及生成/索引/召回均可追踪。

PG的app_memory.extractions/snapshots永久追加，state保存本人有效snapshot_id/version和indexed_snapshot_id；topics用跨版本稳定topic_key（工具参数topic_id）组织目录与详情，chunks记录派生索引定位。Redis键为zhixing:memory:<数据库摘要>:<user_id>:<snapshot_id>，值为UTF-8 JSON，只含完整有界画像/偏好及主题目录，默认TTL1800秒；先查PG有效版本，缓存丢失/故障就回PG重建。默认画像/偏好预算4096参考token、目录2048，目录超量明确标记；有效画像/偏好各最多32键、主题最多1000，超过容量后台保留旧有效版本并记录失败。

每次请求自动附带偏好和主题目录（编号、名称/别名、一句简介），放当前用户消息尾部保护固定前缀。当前要求优先于旧偏好；目录不等于已读取详情。主模型按需调用search_user_memory(query,topic_id可选)，当前对话足够时不调用。它与企业search_knowledge是两项独立工具，共用CPU向量、中文BM25双路、RRF和真实重排代码；私人Milvus集合按本人/有效快照/空间预过滤，正文返回前和发布前再回PG核验，管理员也不能查他人个人记忆。企业事实仍必须用当前授权RAG/业务SQL。公开关键词仅接收本次原始公开任务；所在地/附近指代允许后端最小投影本人粗略城市，不接收其他画像或项目/RAG/SQL正文。个人来源引用可在右侧只读预览；没有记忆管理、CRUD或保存确认/开关。

索引排队/失败不影响已经完成的主回答，概要仍可使用。详情暂不可用明确说明，不能谎称没有记忆；重试最多3次，租约到期可恢复，旧任务不能切换新的有效指针，旧向量可清理而PG版本保留。新记忆异步生效，马上新建对话不保证已经完成；升级前历史不批量回填，避免无界后台费用。

升级现有环境：

```powershell
docker compose build api
docker compose --profile tools run --rm db-admin migrate
docker compose up -d --wait
```

日常前端仍在frontend目录运行npm run dev，不启动production前端容器。验收入口：Windows运行powershell -ExecutionPolicy Bypass -File .\scripts\verify-m11.ps1，Linux运行sh scripts/verify-m11.sh。两组定向测试使用独立PG/Redis/Milvus：乱序/幂等/无信号/租约与缓存故障/隔离，以及两账号真实模型提取→真实私人混合检索→跨会话偏好→主模型工具调用→独立审核，报告保存verification/M11。

个人语义整理定向验收：Windows运行powershell -ExecutionPolicy Bypass -File .\scripts\verify-memory-consolidation.ps1，Linux运行sh scripts/verify-memory-consolidation.sh。来源原话提取与整理模型职责分别标记memory_extract_*、memory_consolidate_*，请求/结果及usage独立可追踪；模型失败不改当前有效记忆。
