"""审计展示名称与阶段归类，只读取持久事实，不改写原事件或补造执行步骤。"""

TOOL_NAMES={
    'search_knowledge':'检索企业知识库','search_web':'公开联网搜索','search_user_memory':'检索本人项目记忆',
    'query_business_data':'查询业务数据库','read_attachment':'详细读取附件','run_sandbox_code':'执行沙箱代码',
}
EVENT_NAMES={
    'knowledge.record_removed':'删除失效知识管理记录（审计保留）',
    'http.started':'API 接收请求','http.completed':'API 返回响应','http.stream_opened':'打开回答事件流','http.stream_closed':'关闭回答事件流',
    'chat.message_received':'接收并保存用户消息','run.queued':'任务进入等待队列','run.started':'Worker 开始处理',
    'run.completed':'发布回答并完成本轮','run.failed':'本轮执行失败','run.cancelled':'用户停止本轮','run.interrupted':'本轮执行中断',
    'run.waiting_sandbox':'等待附件处理或沙箱执行','run.waiting_confirmation':'等待本人确认敏感操作',
    'context.cache':'读取会话工作上下文','context.compressing':'开始压缩较早上下文','context.compressed':'上下文压缩完成',
    'memory.overview_injected':'注入本人偏好与项目目录','attachment.prepared':'附件预处理完成',
    'tool.request':'模型选择调用工具','tool.started':'开始执行工具','tool.observation':'工具结果返回主模型','tool.completed':'工具执行完成','tool.failed':'工具执行失败',
    'rag.embedding':'知识检索：向量编码','rag.reranker':'知识检索：语义重排','rag.completed':'企业知识检索完成','rag.retrieval_completed':'企业知识检索完成并保存召回证据',
    'web.request':'发起公开搜索请求','web.response':'公开搜索返回结果','web.completed':'公开搜索完成','web.failed':'公开搜索失败',
    'web.reused':'复用本轮已有搜索结果','web.page_read':'读取公开网页','web.page_rejected':'公开网页读取被拒绝','web.provider_response':'公开搜索 API 返回结果',
    'sandbox.queued':'沙箱任务进入等待队列','sandbox.claimed':'Runner 领取沙箱任务','sandbox.started':'开始沙箱操作','sandbox.completed':'沙箱执行完成并保存结果',
    'sandbox.failed':'沙箱执行失败','sandbox.cancelled':'沙箱任务取消','sandbox.unknown':'沙箱结果未知，不自动重跑','sandbox.waiting':'等待沙箱回执','sandbox.ready':'沙箱回执就绪，继续回答',
    'action.prepared':'生成敏感操作确认快照','action.decided':'保存本人确认决定','action.consumed':'消费批准，开始执行敏感操作',
    'action.completed':'敏感操作完成','action.failed':'敏感操作失败','action.unknown':'敏感操作结果未知','business.query_completed':'业务 SQL 查询完成并保存结果',
    'confirmation.required':'向本人展示二次确认','confirmation.decided':'本人确认已处理',
    'answer.review':'保存答案审核结论','answer.facts_rejected':'后端事实核验未通过','review.started':'开始核验答案与执行过程',
    'review.completed':'答案核验通过','review.revision':'按审核意见返工','review.limited':'交付可核实资料与限制说明',
    'memory.query_embedding':'私人项目检索：向量编码','memory.query_rerank':'私人项目检索：语义重排','memory.search_completed':'本人项目记忆检索完成','memory.retrieval':'本人项目记忆检索完成并保存召回证据',
    'memory.job_started':'开始后台记忆作业','memory.job_completed':'后台记忆作业完成','memory.job_failed':'后台记忆作业失败',
    'memory.no_signal':'长期记忆提取：无新增信息','memory.extracted':'长期记忆提取完成','memory.index_embedding':'私人记忆索引：向量编码',
    'memory.indexed':'私人记忆索引完成','memory.snapshot_published':'发布新的长期记忆快照','memory.consolidation_completed':'个人偏好整理完成','memory.consolidation_ignored':'个人偏好无需变更',
    'primary.retry':'主模型失败，准备重试','fallback.retry':'备用模型失败，准备重试','review.retry':'审核模型失败，准备重试',
    'model.fallback':'切换备用模型','model.budget_rejected':'请求超过模型上下文预算','model.retry':'模型调用失败，准备重试','model.circuit_open':'模型熔断，暂停调用','assistant.delta':'发布回答文本分片',
}


def model_phase(role,prompt='',has_calls=False):
    """接收已保存的模型职责/提示词开头与工具调用标志，返回可读阶段；未知职责不假定执行效果。"""
    role=role or ''
    if role.startswith('memory_extract'):return '后台长期记忆提取'
    if role.startswith('memory_consolid'):return '后台个人偏好整理'
    if role=='review':return '答案审核'
    if prompt.startswith('你是对话资料压缩器') or '上下文压缩' in prompt:return '上下文压缩'
    if 'SQL' in prompt and not ('智能助理' in prompt or '工具' in prompt):return '业务 SQL 方案生成'
    if role=='local_retrieval':return '本地检索模型'
    return ('备用模型' if role=='fallback' else '主模型')+('规划并选择工具' if has_calls else '生成回复')


def event_view(row):
    """接收有界事件元数据，补展示标题/摘要/主流程或后台类别，原始JSON留在按需详情接口。"""
    value=dict(row);kind=value['event_type'];role=value.pop('model_role',None) or value.pop('slot',None) or ''
    prompt=value.pop('prompt','') or '';calls=value.pop('has_calls',False)
    title=EVENT_NAMES.get(kind)
    if kind.startswith('web.query_model.'):
        title='公开搜索关键词模型'+{'request':'请求','response':'返回','chunk':'原始流式分片','failed':'调用失败'}.get(kind.rsplit('.',1)[-1],'调用记录')
    elif kind.startswith('model.') and kind.rsplit('.',1)[-1] in ('request','response','chunk','failed','interrupted'):
        phase=model_phase(role,prompt,calls)
        title=phase+'：'+{'request':'发送请求','response':'返回结果','chunk':'原始流式分片','failed':'调用失败','interrupted':'调用中断'}[kind.rsplit('.',1)[-1]]
    operation=value.pop('operation',None)
    if kind.startswith('sandbox.') and operation in ('prepare','read','execute'):
        action={'prepare':'附件预处理','read':'附件详细读取','execute':'代码执行'}[operation]
        title='沙箱'+action+'：'+{'queued':'进入等待队列','claimed':'Runner 领取任务','completed':'完成并保存结果','failed':'执行失败','cancelled':'已取消','unknown':'结果未知'}.get(kind.rsplit('.',1)[-1],'操作记录')
    background=(kind.startswith('memory.') and kind not in ('memory.overview_injected','memory.query_embedding','memory.query_rerank','memory.search_completed','memory.retrieval','memory.source_read')) or role.startswith('memory_')
    notes=[]
    for key in ('worker_id','runner_id'):
        if value.get(key):notes.append(str(value.pop(key)))
    tool=value.pop('tool_name',None)
    if tool:notes.append(TOOL_NAMES.get(tool,tool))
    outcomes={'approved':'本人同意','rejected':'本人拒绝','invalidated':'批准已失效','expired':'已过期','approve':'审核通过','revise':'需要返工','limited':'受限交付','unavailable':'当前不可用'}
    warning=any(value.get(key) in ('rejected','invalidated','expired','revise','limited','unavailable') for key in ('decision','verdict'))
    for key in ('message','decision','verdict'):
        if value.get(key):
            item=str(value.pop(key));notes.append(outcomes.get(item,item))
    value.update(title=title or '审计记录：'+kind,summary=' · '.join(notes)[:200],group='background' if background else 'execution',warning=warning)
    return value
