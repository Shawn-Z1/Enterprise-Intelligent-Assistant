"""可信工具目录只声明已实现能力，按当前身份过滤模型可见列表。"""
import copy

# 动作类型与自动允许策略来自服务端，不接受模型指定免确认。
TOOL_REGISTRY = {
    'search_knowledge': {'action_type':'knowledge.read','confirmation':'automatic','function':{
        'name':'search_knowledge','description':'查询本企业当前授权的内部知识。公司制度、流程、报销标准等内部事实必须取得合法证据。',
        'parameters':{'type':'object','properties':{'query':{'type':'string','description':'具体的内部检索问题，保留编号和日期'}},'required':['query'],'additionalProperties':False}}},
    'search_web': {'action_type':'public.search','confirmation':'automatic','function':{
        'name':'search_web','description':'查询公开互联网的最新信息、官方资料或公开产品价格。不能查询企业内部制度或外发内部正文、密钥和业务明细。复合问题可与search_knowledge分别调用；实际外发词仅取自用户本次公开任务。',
        'parameters':{'type':'object','properties':{'query':{'type':'string','description':'公开检索意图候选，只保存本地调用记录；实际搜索词由用户本次公开任务独立生成，不采用内部检索结果或历史扩写'}},'required':['query'],'additionalProperties':False}}}
}
TOOL_REGISTRY['query_business_data']={'action_type':'business.query','confirmation':'sensitive_boundary','function':{
    'name':'query_business_data','description':'查询当前授权的模拟业务数据：员工、考勤、符合角色的工单/审批，以及技术部共享的全部人员代码提交记录。代码记录可按日期/作者/仓库统计并展示柱状图或折线图，不限本人。工具先生成并校验SQL，真正查询之前触发人工二次确认Hook。用户拒绝后本轮不重复请求，不能自行批准。',
    'parameters':{'type':'object','properties':{'query':{'type':'string','description':'具体业务查询问题，含口径、时间范围和需要的图表；SQL由独立准备模块在本人允许目录生成'}},'required':['query'],'additionalProperties':False}}}

TOOL_REGISTRY['read_attachment']={'action_type':'attachment.read','confirmation':'automatic','function':{
 'name':'read_attachment','description':'详细读取当前会话中本人已发送的附件，包括较早消息的文件；编号从会话附件目录取得。TXT/MD/DOCX/PDF按字符offset分页（limit默认12000），PDF可指定page；CSV/XLSX按真实table和行offset读取，每次最多20行/20列；表编号前缀从目录table_prefix取得，也可不指定table来读取首表。宽表可用column_offset/column_limit继续读列。概况不等于全文，完整总结需覆盖正文并核对has_more/next_offset。统计/转换可直接让沙箱处理原件，无需把所有行发给模型。文件URL/普通超链接只当字符串，不联网访问。不能上传图片/音频或识别扫描PDF。',
 'parameters':{'type':'object','properties':{'attachment_id':{'type':'string'},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':12000},'page':{'type':'integer','minimum':1},'table':{'type':'string'},'column_offset':{'type':'integer','minimum':0},'column_limit':{'type':'integer','minimum':1,'maximum':20}},'required':['attachment_id'],'additionalProperties':False}}}
TOOL_REGISTRY['run_sandbox_code']={'action_type':'sandbox.execute','confirmation':'automatic','function':{
 'name':'run_sandbox_code','description':'自动在独立无网络Docker任务容器执行Python或文件DuckDB只读SQL，不需要人工确认。可计算、多CSV/XLSX联查、绘图、转换/修改/生成文件；最多3次代码尝试。附件概况已由系统预处理，详读用read_attachment。预装np/pd、openpyxl、DuckDB、Matplotlib/Seaborn/Pillow、python-docx/pypdf、Pandoc/LibreOffice及中文字体。tables是真实表名到DataFrame的映射；files是附件ID到只读Path的映射；OUTPUT_DIR是/output。emit_result(rows,chart可选,description)自动生成结果CSV及柱/线图；也可自己写OUTPUT_DIR顶层TXT/MD/CSV/XLSX/DOCX/PDF/PNG，或emit_text返回计算文字。convert_document(files[附件ID],target="pdf"或"docx",name="报告")支持MD/TXT转DOCX、DOCX转PDF、MD转PDF，实际产物由回执收集；不用emit_result也能交付文件。可用Matplotlib/Seaborn生成热力图并保存PNG，需设置中文字体Noto Sans CJK JP。不执行宏、不安装依赖、不访问业务库或其他用户文件，原件不可修改。下载用Markdown链接[文件名](/api/sandbox/artifacts/编号)，编号必须来自返回artifacts的artifact_id，不只显示裸路径。正常满额会排队，不能要求用户重新发送或假称已执行。',
 'parameters':{'type':'object','properties':{'attachment_ids':{'type':'array','items':{'type':'string'}},'language':{'type':'string','enum':['python','sql']},'code':{'type':'string'},'purpose':{'type':'string'},'chart':{'type':'object'}},'required':['attachment_ids','language','code','purpose'],'additionalProperties':False}}}


def available_tools(context, web_enabled: bool, sandbox_enabled: bool=False) -> list[dict]:
    """接收当前可信身份与搜索实现状态，返回最小可调用schema；调用执行阶段仍须复验。"""
    names = (['search_knowledge'] if context.can_use_knowledge else []) + (['search_web'] if web_enabled else []) + (['query_business_data'] if context.can_query_business else [])+(['read_attachment','run_sandbox_code'] if sandbox_enabled else [])
    return [{'type':'function','function':copy.deepcopy(TOOL_REGISTRY[name]['function'])} for name in names]


def model_observation(output: dict, name: str) -> dict:
    """接收可信工具结果及工具名，返回模型实际可见的受限观察；执行回填和事实审核使用同一投影。"""
    observed = copy.deepcopy(output)
    if name=='query_business_data' and 'rows' in observed:
        observed['model_rows_truncated']=len(observed['rows'])>20
        observed['rows']=observed['rows'][:20]
        # 执行ID和权限版本供后端追踪，不是用户答案内容；模型只需要来源标号、范围及真实结果。
        for key in ('result_id','action_id','duration_ms'):observed.pop(key,None)
        observed['citations']=[{key:citation.get(key) for key in ('type','label','title','content')} for citation in observed.get('citations',[])]
    if name in ('run_sandbox_code','read_attachment','attachment_prepare'):
        observed.pop('attempt_id',None);observed.pop('result_id',None)
        observed['citations']=[{**row,'content':row.get('content','')[:650]} for row in observed.get('citations',[])]
        if 'receipt' in observed:observed['receipt']={key:value[:4000] if isinstance(value,str) else value for key,value in observed['receipt'].items() if key!='result'}
        if 'result' in observed:
            if 'stdout' in observed['result']:observed['result']['stdout']=observed['result']['stdout'][:4000]
            observed['result']['rows']=observed['result'].get('rows',[])[:20]
            observed['result']['model_rows_truncated']=len(output.get('result',{}).get('rows',[]))>20
    if name=='search_web':
        observed['citations'] = [{key:citation.get(key) for key in ('type','label','source_id','title','website','published_at','fetched_at','content_kind','page_read')} | {
            'content':('同一来源正文已在前一次工具观察提供' if output.get('reused_in_run') else citation['content'][:650]),
            'model_excerpt_truncated':len(citation['content'])>650} for citation in output['citations']]
    return observed
