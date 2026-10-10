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

def available_tools(context, web_enabled: bool) -> list[dict]:
    """接收当前可信身份与搜索实现状态，返回最小可调用schema；调用执行阶段仍须复验。"""
    names = (['search_knowledge'] if context.can_use_knowledge else []) + (['search_web'] if web_enabled else [])
    return [{'type':'function','function':copy.deepcopy(TOOL_REGISTRY[name]['function'])} for name in names]


def model_observation(output: dict, name: str) -> dict:
    """接收可信工具结果及工具名，返回模型实际可见的受限观察；执行回填和事实审核使用同一投影。"""
    observed = copy.deepcopy(output)
    if name=='search_web':
        observed['citations'] = [{key:citation.get(key) for key in ('type','label','source_id','title','website','published_at','fetched_at','content_kind','page_read')} | {
            'content':('同一来源正文已在前一次工具观察提供' if output.get('reused_in_run') else citation['content'][:650]),
            'model_excerpt_truncated':len(citation['content'])>650} for citation in output['citations']]
    return observed
