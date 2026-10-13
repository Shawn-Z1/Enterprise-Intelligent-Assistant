"""管理员只读审计查询、分页、步骤和受控JSON导出；不执行任何模型/工具/SQL业务动作。"""
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from app.auth import current_context, auth_error
from app.audit import redact_secrets
from app.ids import ShortId
from app.permissions import AuthContext
from app.audit_presentation import event_view,model_phase,EVENT_NAMES

router=APIRouter(prefix='/api/admin/audit',tags=['只读审计'])
# 事件详情与导出均有限额，数据库保存的完整原始JSON不截断或覆盖。
DETAIL_BYTES=256*1024
EXPORT_BYTES=16*1024*1024


async def audit_context(request: Request,context: AuthContext=Depends(current_context)):
    """接收当前请求及可信身份，仅允许现任管理员读取；拒绝也写审计，身份不从请求参数取得。"""
    if not context.is_admin:
        await request.app.state.audit.append(request.state.trace_id,'audit.permission_denied',{},user_id=context.user_id)
        raise auth_error(403,'ADMIN_REQUIRED','仅系统管理员可以读取审计记录')
    return context


async def recheck(request):
    """接收请求，在取出敏感正文或导出后再次读取当前管理员资格，返回无值。"""
    await audit_context(request,await current_context(request))


def scrub(value,secrets):
    """接收原记录及当前配置密钥，遮蔽凭证/能力哈希和常见内嵌凭证；只改变返回视图。"""
    value=redact_secrets(value,secrets)
    blocked={'csrftokenhash','executiontokenhash','tokenhash','authsessionid','executiontoken','controltoken','databasepassword'}
    if isinstance(value,dict):return {key:'[REDACTED]' if re.sub(r'[^a-z0-9]','',str(key).lower()) in blocked else scrub(item,secrets) for key,item in value.items()}
    if isinstance(value,list):return [scrub(item,secrets) for item in value]
    if isinstance(value,str):
        value=re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/-]{8,}',r'\1[REDACTED]',value)
        value=re.sub(r'''(?i)((?:api[_-]?key|password|secret[_-]?key)\s*[:=]\s*["']?)[^\s"',;]{6,}''',r'\1[REDACTED]',value)
    return value


async def record_read(request,context,kind,data):
    """接收已授权读取动作及范围，追加本次查询审计，避免在原任务trace内递归追加。"""
    await recheck(request)
    await request.app.state.audit.append(request.state.trace_id,kind,data,user_id=context.user_id)


BASE='''FROM app_audit.traces t LEFT JOIN app_runtime.runs r ON r.trace_id=t.trace_id
 LEFT JOIN LATERAL (SELECT user_id,conversation_id FROM app_audit.events e WHERE e.trace_id=t.trace_id AND e.user_id IS NOT NULL ORDER BY event_id LIMIT 1) actor ON true
 LEFT JOIN app_system.users u ON u.user_id=COALESCE(r.user_id,t.user_id,actor.user_id)
 LEFT JOIN LATERAL (SELECT count(*) AS events_count,count(*) FILTER(WHERE event_type ~ '(failed|rejected|interrupted|denied|unavailable|unknown)$') AS failures,
    max(event_id) AS last_event_id,max(created_at) AS last_event_at FROM app_audit.events e WHERE e.trace_id=t.trace_id) ev ON true'''
META='''t.trace_id,t.created_at,t.service,r.run_id,COALESCE(r.user_id,t.user_id,actor.user_id) AS user_id,
 u.username,u.display_name,COALESCE(r.conversation_id,t.conversation_id,actor.conversation_id) AS conversation_id,
 COALESCE(r.status,'recorded') AS status,ev.events_count,ev.failures,ev.last_event_id,ev.last_event_at'''


async def metadata(conn,trace_id):
    """接收连接及任务trace，返回当前任务归属/状态和事件计数，缺失统一404。"""
    row=await conn.fetchrow('SELECT '+META+' '+BASE+' WHERE t.trace_id=$1',trace_id)
    if row is None:raise auth_error(404,'NOT_FOUND','审计记录不存在')
    return dict(row)


@router.get('/traces')
async def traces(request: Request,context: AuthContext=Depends(audit_context),user: str=Query('',max_length=80),
                 conversation: str=Query('',max_length=16),trace: str=Query('',max_length=16),
                 since: datetime | None=None,until: datetime | None=None,status: Literal['','failed','completed','active']='',
                 tasks_only: bool=True,page: int=Query(1,ge=1,le=10000),limit: int=Query(20,ge=1,le=50)):
    """接收用户/会话/trace/时间/状态与分页，参数化筛选并返回真实元数据；默认隐藏纯HTTP查询噪声。"""
    filters=[];params=[]
    def add(condition,value):
        """接收服务器固定条件和参数，生成位置绑定，不把客户端文字拼成SQL。"""
        params.append(value);filters.append(condition.replace('?',f'${len(params)}'))
    if tasks_only:filters.append("(r.run_id IS NOT NULL OR EXISTS(SELECT 1 FROM app_audit.events z WHERE z.trace_id=t.trace_id AND z.event_type ~ '^(knowledge|memory)\\.'))")
    if user:add("(u.user_id=? OR u.username ILIKE '%' || ? || '%' OR u.display_name ILIKE '%' || ? || '%')",user)
    if conversation:add('COALESCE(r.conversation_id,t.conversation_id,actor.conversation_id)=?',conversation)
    if trace:add('t.trace_id=?',trace)
    zone=timezone(timedelta(hours=8))
    if since:since=since.replace(tzinfo=zone) if since.tzinfo is None else since;add('t.created_at>=?',since)
    if until:until=until.replace(tzinfo=zone) if until.tzinfo is None else until;add('t.created_at<=?',until)
    if since and until and since>until:raise auth_error(422,'INVALID_RANGE','开始时间不能晚于结束时间')
    if status=='failed':filters.append("(ev.failures>0 OR r.status IN ('failed','interrupted') OR EXISTS(SELECT 1 FROM app_memory.jobs j WHERE j.trace_id=t.trace_id AND j.status='failed'))")
    elif status=='completed':filters.append("r.status='completed'")
    elif status=='active':filters.append("r.status IN ('pending','running','waiting_sandbox','waiting_confirmation')")
    where=' WHERE '+' AND '.join(filters) if filters else ''
    async with request.app.state.pool.acquire() as conn:
        total=await conn.fetchval('SELECT count(*) '+BASE+where,*params)
        rows=await conn.fetch('SELECT '+META+' '+BASE+where+f' ORDER BY t.created_at DESC,t.trace_id DESC LIMIT ${len(params)+1} OFFSET ${len(params)+2}',*params,limit,(page-1)*limit)
    await record_read(request,context,'audit.traces_read',{'page':page,'limit':limit,'count':len(rows)})
    return {'items':[dict(row) for row in rows],'page':page,'limit':limit,'total':total}


@router.get('/traces/{trace_id}')
async def trace_detail(trace_id: ShortId,request: Request,context: AuthContext=Depends(audit_context)):
    """接收trace，返回步骤树元数据、去重模型用量和后台/工具关联；大正文按事件单独读取。"""
    async with request.app.state.pool.acquire() as conn:
        meta=await metadata(conn,trace_id)
        spans=await conn.fetch('''SELECT s.*,count(e.event_id) AS events_count FROM app_audit.spans s LEFT JOIN app_audit.events e USING(trace_id,span_id)
            WHERE s.trace_id=$1 GROUP BY s.span_id ORDER BY s.created_at,s.span_id LIMIT 1000''',trace_id)
        # 每个模型尝试只采用最后一次完成响应，累计流式usage包不重复相加。
        usage=await conn.fetch('''SELECT DISTINCT ON(e.span_id) e.span_id,COALESCE(m.model_role,e.raw_json->>'slot','local_retrieval') AS role,
            e.model,e.usage_source,e.input_tokens,e.output_tokens,e.total_tokens,e.duration_ms,e.usage_method,
            COALESCE(e.raw_json->'usage'->'prompt_tokens_details'->'cached_tokens',e.raw_json->'usage'->'prompt_cache_hit_tokens') AS cached_tokens
            FROM app_audit.events e LEFT JOIN app_runtime.model_attempts m ON m.attempt_id=e.span_id
            WHERE e.trace_id=$1 AND (e.event_type LIKE '%model.response' OR e.event_type IN ('rag.embedding','rag.reranker','memory.query_embedding','memory.query_rerank','memory.index_embedding'))
            ORDER BY e.span_id,e.event_id DESC LIMIT 500''',trace_id)
        models=await conn.fetch('SELECT attempt_id,model_role,model,status,created_at FROM app_runtime.model_attempts WHERE trace_id=$1 ORDER BY created_at LIMIT 500',trace_id)
        memory=await conn.fetch('SELECT job_id,job_type,status,attempts,snapshot_id,created_at,error_json FROM app_memory.jobs WHERE trace_id=$1 ORDER BY created_at LIMIT 100',trace_id)
        knowledge=await conn.fetch('SELECT job_id,job_type,status,attempts,document_id,created_at,error_json FROM app_knowledge.jobs WHERE trace_id=$1 ORDER BY created_at LIMIT 100',trace_id)
        messages=[];tools=[];sandboxes=[];actions=[]
        if meta['run_id']:
            messages=await conn.fetch('''SELECT message_id,role,left(content,12000) AS content,length(content)>12000 AS truncated FROM app_runtime.messages WHERE run_id=$1 ORDER BY message_order''',meta['run_id'])
            tools=await conn.fetch('SELECT call_id,tool_name,status,provider_call_id,created_at FROM app_runtime.tool_calls WHERE run_id=$1 ORDER BY created_at',meta['run_id'])
            sandboxes=await conn.fetch('SELECT job_id,operation,status,runner_id,node_instance_id AS instance_id,created_at FROM app_runtime.sandbox_jobs WHERE run_id=$1 ORDER BY created_at LIMIT 100',meta['run_id'])
            actions=await conn.fetch('SELECT approval_id,action_type,decision,expires_at,created_at FROM app_runtime.query_approvals WHERE run_id=$1 ORDER BY created_at',meta['run_id'])
    usage_rows=[dict(row) for row in usage]
    responded={row['span_id'] for row in usage_rows}
    # 失败/中断尝试没有最终usage响应时保持未知，不能显示为零成本或从流式累计包猜测。
    usage_rows.extend({'span_id':row['attempt_id'],'role':row['model_role'],'model':row['model'],'usage_source':'unavailable',
        'input_tokens':None,'output_tokens':None,'total_tokens':None,'duration_ms':None,'cached_tokens':None,'usage_method':None}
        for row in models if row['attempt_id'] not in responded)
    value={'trace':meta,'spans':[dict(row) for row in spans],'usage':usage_rows,
        'related':{key:[dict(row) for row in rows] for key,rows in {'models':models,'memory_jobs':memory,'knowledge_jobs':knowledge,'messages':messages,'tools':tools,'sandbox_jobs':sandboxes,'confirmations':actions}.items()},
        'limits':{'spans':1000,'models':500,'jobs':100},'background_pending':any(row['status'] in ('pending','running') for row in memory+knowledge)}
    await record_read(request,context,'audit.trace_read',{'trace_id':trace_id})
    return scrub(value,request.app.state.audit.secrets)


@router.get('/traces/{trace_id}/events')
async def events(trace_id: ShortId,request: Request,context: AuthContext=Depends(audit_context),after: int=Query(0,ge=0),
                 span: str=Query('',max_length=16),chunks: bool=False,limit: int=Query(50,ge=1,le=100)):
    """接收trace及事件游标，按真实追加顺序分页；默认折叠流式包，不能误称未查询的事件不存在。"""
    async with request.app.state.pool.acquire() as conn:
        await metadata(conn,trace_id)
        rows=await conn.fetch('''SELECT event_id,span_id,event_type,created_at,usage_source,model,duration_ms,octet_length(raw_json::text) AS bytes
            FROM app_audit.events WHERE trace_id=$1 AND event_id>$2 AND ($3::text='' OR span_id=$3)
            AND ($4 OR event_type NOT LIKE '%model.chunk') ORDER BY event_id LIMIT $5''',trace_id,after,span,chunks,limit+1)
    more=len(rows)>limit;items=rows[:limit]
    await record_read(request,context,'audit.events_read',{'trace_id':trace_id,'after':after,'count':len(items)})
    return {'items':[dict(row) for row in items],'has_more':more,'next_after':items[-1]['event_id'] if more else None,'chunks_hidden':not chunks}


@router.get('/events/{event_id}')
async def event_detail(event_id: int,request: Request,context: AuthContext=Depends(audit_context)):
    """接收事件ID，返回大小受限的安全JSON视图；超限正文仍可通过单事件导出，不覆盖原记录。"""
    async with request.app.state.pool.acquire() as conn:
        row=await conn.fetchrow('''SELECT event_id,trace_id,event_type,created_at,octet_length(raw_json::text) AS bytes,
            CASE WHEN octet_length(raw_json::text)<=$2 THEN raw_json END AS data FROM app_audit.events WHERE event_id=$1''',event_id,DETAIL_BYTES)
    if not row:raise auth_error(404,'NOT_FOUND','审计事件不存在')
    await record_read(request,context,'audit.event_read',{'event_id':event_id})
    return scrub(dict(row)|{'too_large':row['data'] is None},request.app.state.audit.secrets)


async def export_rows(request,context,trace_id=None,event_id=None,after=0,limit=1000):
    """接收已授权导出范围，先核对字节额度再读取完整本页；返回带完整性/游标说明的下载响应。"""
    async with request.app.state.pool.acquire() as conn:
        rows=await conn.fetch('''SELECT event_id,octet_length(raw_json::text) AS bytes FROM app_audit.events
            WHERE ($1::varchar IS NULL OR trace_id=$1) AND ($2::bigint IS NULL OR event_id=$2) AND event_id>$3 ORDER BY event_id LIMIT $4''',trace_id,event_id,after,limit+1)
        if not rows:raise auth_error(404,'NOT_FOUND','没有可导出的记录')
        more=len(rows)>limit;selected=rows[:limit]
        if sum(row['bytes'] for row in selected)>EXPORT_BYTES:raise auth_error(413,'EXPORT_LIMIT','本页正文超过16MiB，请减少条数或导出单事件')
        records=await conn.fetch('SELECT * FROM app_audit.events WHERE event_id=ANY($1::bigint[]) ORDER BY event_id',[row['event_id'] for row in selected])
    payload={'trace_id':trace_id,'complete':after==0 and not more,'as_of_event_id':rows[-1]['event_id'],'next_after':selected[-1]['event_id'] if more else None,
        'events':[dict(row) for row in records],'note':'仅导出查询时已保存事件的凭证遮蔽视图；后台仍可能追加；complete=false时不能当作整个任务档案'}
    await record_read(request,context,'audit.exported',{'trace_id':trace_id,'event_id':event_id,'count':len(records),'complete':payload['complete']})
    body=json.dumps(scrub(payload,request.app.state.audit.secrets),ensure_ascii=False,default=str)
    if len(body.encode())>EXPORT_BYTES:raise auth_error(413,'EXPORT_LIMIT','导出大小超过限额，请减少条数')
    return Response(body,media_type='application/json',headers={'Content-Disposition':f'attachment; filename="audit-{trace_id or event_id}-{after}.json"','X-Content-Type-Options':'nosniff'})


@router.get('/traces/{trace_id}/export')
async def trace_export(trace_id: ShortId,request: Request,context: AuthContext=Depends(audit_context),after: int=Query(0,ge=0),limit: int=Query(1000,ge=1,le=1000)):
    """接收trace及分页范围，仅下载已保存且遮蔽凭证的JSON，不执行记录中的任何代码。"""
    return await export_rows(request,context,trace_id=trace_id,after=after,limit=limit)


@router.get('/events/{event_id}/export')
async def event_export(event_id: int,request: Request,context: AuthContext=Depends(audit_context)):
    """接收单事件ID，导出其完整安全JSON；模型超长上下文可分事件检查，不突破16MiB额度。"""
    return await export_rows(request,context,event_id=event_id,limit=1)


CONVERSATION_BASE='''FROM app_runtime.conversations c JOIN app_system.users u USING(user_id)
 LEFT JOIN app_system.departments d ON d.department_id=u.department_id
 LEFT JOIN LATERAL (SELECT count(*) AS turns_count FROM app_runtime.messages m WHERE m.conversation_id=c.conversation_id AND m.role='user') turns ON true
 LEFT JOIN LATERAL (SELECT count(*) FILTER(WHERE status IN ('failed','interrupted')) AS failures,
 count(*) FILTER(WHERE status IN ('pending','running','waiting_sandbox','waiting_confirmation')) AS active_count,
 max(updated_at) AS last_run_at FROM app_runtime.runs r WHERE r.conversation_id=c.conversation_id) runs ON true'''
CONVERSATION_META='''c.conversation_id,c.title,c.created_at,GREATEST(c.updated_at,runs.last_run_at) AS updated_at,
 u.user_id,u.username,u.display_name,d.name AS department_name,turns.turns_count,runs.failures,runs.active_count'''


async def conversation_meta(conn,conversation_id):
    """接收连接和完整会话编号，返回该会话可信归属与问答轮数；不把系统trace伪装成会话。"""
    row=await conn.fetchrow('SELECT '+CONVERSATION_META+' '+CONVERSATION_BASE+' WHERE c.conversation_id=$1',conversation_id)
    if not row:raise auth_error(404,'NOT_FOUND','会话不存在')
    return dict(row)


@router.get('/conversations')
async def conversations(request: Request,context: AuthContext=Depends(audit_context),user: str=Query('',max_length=80),
                        search: str=Query('',max_length=200),since: datetime | None=None,until: datetime | None=None,
                        status: Literal['','failed','active']='',page: int=Query(1,ge=1,le=10000),limit: int=Query(7,ge=1,le=12)):
    """接收用户/会话标题与时间筛选，分页列出所有用户真实会话；系统日志和同会话多run不新增列表条目。"""
    filters=[];params=[]
    def add(condition,value):
        """接收固定筛选和客户端值，以位置参数绑定，返回无值，防止筛选内容改变SQL结构。"""
        params.append(value);filters.append(condition.replace('?',f'${len(params)}'))
    if user:add("(u.user_id=? OR u.username ILIKE '%'||?||'%' OR u.display_name ILIKE '%'||?||'%')",user)
    if search:add("(c.title ILIKE '%'||?||'%' OR c.conversation_id=?)",search)
    zone=timezone(timedelta(hours=8))
    if since:since=since.replace(tzinfo=zone) if since.tzinfo is None else since;add('GREATEST(c.updated_at,runs.last_run_at)>=?',since)
    if until:until=until.replace(tzinfo=zone) if until.tzinfo is None else until;add('GREATEST(c.updated_at,runs.last_run_at)<=?',until)
    if since and until and since>until:raise auth_error(422,'INVALID_RANGE','开始时间不能晚于结束时间')
    if status=='failed':filters.append('runs.failures>0')
    if status=='active':filters.append('runs.active_count>0')
    where=' WHERE '+' AND '.join(filters) if filters else ''
    async with request.app.state.pool.acquire() as conn:
        total=await conn.fetchval('SELECT count(*) '+CONVERSATION_BASE+where,*params)
        rows=await conn.fetch('SELECT '+CONVERSATION_META+' '+CONVERSATION_BASE+where+f' ORDER BY GREATEST(c.updated_at,runs.last_run_at) DESC,c.conversation_id DESC LIMIT ${len(params)+1} OFFSET ${len(params)+2}',*params,limit,(page-1)*limit)
    await record_read(request,context,'audit.conversations_read',{'page':page,'limit':limit,'count':len(rows)})
    return {'items':[dict(row) for row in rows],'total':total,'page':page,'limit':limit}


@router.get('/conversations/{conversation_id}/turns')
async def conversation_turns(conversation_id: ShortId,request: Request,context: AuthContext=Depends(audit_context),
                             after: int=Query(0,ge=0),limit: int=Query(20,ge=1,le=50)):
    """接收会话和消息顺序游标，按用户提问配对本人run回复，兼容旧无run记录；失败/待答保留真实状态。"""
    async with request.app.state.pool.acquire() as conn:
        meta=await conversation_meta(conn,conversation_id)
        rows=await conn.fetch('''WITH questions AS (
            SELECT m.*,row_number() OVER(ORDER BY message_order) AS turn_number,
                lead(message_order,1,9223372036854775807) OVER(ORDER BY message_order) AS next_question
            FROM app_runtime.messages m WHERE conversation_id=$1 AND role='user')
            SELECT q.message_id,q.message_order,q.turn_number,q.created_at,q.run_id,left(q.content,12000) AS content,length(q.content)>12000 AS truncated,
              r.trace_id,r.status,r.state_json->'error' AS error,
              a.message_id AS reply_id,a.created_at AS reply_at,left(a.content,12000) AS reply_content,length(a.content)>12000 AS reply_truncated
            FROM questions q LEFT JOIN app_runtime.runs r ON r.run_id=q.run_id AND r.conversation_id=q.conversation_id
            LEFT JOIN LATERAL (SELECT * FROM app_runtime.messages a WHERE a.conversation_id=q.conversation_id AND a.role='assistant'
                AND ((q.run_id IS NOT NULL AND a.run_id=q.run_id) OR (q.run_id IS NULL AND a.message_order>q.message_order AND a.message_order<q.next_question))
                ORDER BY a.message_order LIMIT 1) a ON true
            WHERE q.message_order>$2 ORDER BY q.message_order LIMIT $3''',conversation_id,after,limit+1)
        selected=rows[:limit];ids=[row['message_id'] for row in selected]
        attachments=await conn.fetch('SELECT message_id,attachment_id,filename,format,size_bytes FROM app_runtime.analysis_attachments WHERE message_id=ANY($1::varchar[]) AND NOT draft_removed ORDER BY created_at',ids)
    items=[]
    for row in selected:
        items.append({'turn_number':row['turn_number'],'message_order':row['message_order'],'run_id':row['run_id'],'trace_id':row['trace_id'],
            'status':row['status'] or ('completed' if row['reply_id'] else 'recorded'),'error':row['error'],
            'user_message':{'message_id':row['message_id'],'content':row['content'],'truncated':row['truncated'],'created_at':row['created_at'],
                'attachments':[dict(file) for file in attachments if file['message_id']==row['message_id']]},
            'assistant_message':{'message_id':row['reply_id'],'content':row['reply_content'],'truncated':row['reply_truncated'],'created_at':row['reply_at']} if row['reply_id'] else None})
    await record_read(request,context,'audit.conversation_read',{'conversation_id':conversation_id,'after':after,'count':len(items)})
    return scrub({'conversation':meta,'items':items,'has_more':len(rows)>limit,'next_after':selected[-1]['message_order'] if len(rows)>limit else None},request.app.state.audit.secrets)


async def audit_run(conn,run_id):
    """接收连接和轮次run编号，返回持久轮次，缺失404；不从客户端接受归属或工具执行权限。"""
    row=await conn.fetchrow('SELECT run_id,trace_id,conversation_id FROM app_runtime.runs WHERE run_id=$1',run_id)
    if not row:raise auth_error(404,'NOT_FOUND','本轮执行记录不存在')
    return row


@router.get('/runs/{run_id}')
async def turn_detail(run_id: ShortId,request: Request,context: AuthContext=Depends(audit_context)):
    """接收单轮run，复用永久trace用量事实，补中文阶段和明确作业状态；不重新执行任何工具。"""
    async with request.app.state.pool.acquire() as conn:
        run=await audit_run(conn,run_id)
        prompts=await conn.fetch('''SELECT attempt_id,created_at,left(request_json->'messages'->0->>'content',160) AS prompt,
            jsonb_array_length(CASE WHEN jsonb_typeof(response_json->'merged'->'tool_calls')='array' THEN response_json->'merged'->'tool_calls' ELSE '[]'::jsonb END)>0 AS has_calls
            FROM app_runtime.model_attempts WHERE trace_id=$1 LIMIT 500''',run['trace_id'])
        phases=await conn.fetch('''SELECT DISTINCT ON(span_id) span_id,event_type,created_at FROM app_audit.events WHERE trace_id=$1
            AND (event_type LIKE '%model.response' OR usage_source<>'unavailable') ORDER BY span_id,event_id DESC''',run['trace_id'])
    value=await trace_detail(run['trace_id'],request,context)
    lookup={row['attempt_id']:row for row in prompts}
    kinds={row['span_id']:row['event_type'] for row in phases}
    times={row['span_id']:row['created_at'] for row in phases}
    for row in value['usage']:
        prompt=lookup.get(row['span_id'])
        kind=kinds.get(row['span_id'],'')
        row['created_at']=prompt['created_at'] if prompt else times.get(row['span_id'])
        row['phase']='公开搜索关键词提炼' if kind.startswith('web.query_model.') else EVENT_NAMES.get(kind) if row['role']=='local_retrieval' and kind in EVENT_NAMES else model_phase(row['role'],(prompt['prompt'] or '') if prompt else '',prompt['has_calls'] if prompt else False)
    value['usage'].sort(key=lambda row:row['created_at'] or datetime.min.replace(tzinfo=timezone.utc))
    # 关联信息只保留用户能够读懂的作业表格，原始请求/回执通过下面的事件逐条展开。
    return {'run':dict(run),'usage':value['usage'],'background_pending':value['background_pending'],
            'jobs':value['related']['memory_jobs'],'sandbox_jobs':value['related']['sandbox_jobs'],'confirmations':value['related']['confirmations']}


@router.get('/runs/{run_id}/events')
async def turn_events(run_id: ShortId,request: Request,context: AuthContext=Depends(audit_context),
                      page: int=Query(1,ge=1,le=10000),limit: int=Query(50,ge=1,le=100),chunks: bool=False):
    """接收轮次和分页，把原审计与缺失的持久运行进度按时间合并；保留来源ID，内联正文按需取回。"""
    async with request.app.state.pool.acquire() as conn:
        run=await audit_run(conn,run_id)
        rows=await conn.fetch('''WITH timeline AS (
          SELECT e.event_id,'audit'::text AS source,e.span_id,e.event_type,e.created_at,e.duration_ms,e.model,
            octet_length(e.raw_json::text) AS bytes,COALESCE(m.model_role,e.raw_json->>'slot') AS model_role,
            left(m.request_json->'messages'->0->>'content',160) AS prompt,
            jsonb_array_length(CASE WHEN jsonb_typeof(m.response_json->'merged'->'tool_calls')='array' THEN m.response_json->'merged'->'tool_calls' ELSE '[]'::jsonb END)>0 AS has_calls,
            e.raw_json->>'worker_id' AS worker_id,e.raw_json->>'runner_id' AS runner_id,
            COALESCE(t.tool_name,e.raw_json->>'tool_name') AS tool_name,left(e.raw_json->>'message',120) AS message,
            e.raw_json->>'decision' AS decision,e.raw_json->>'verdict' AS verdict,j.operation
          FROM app_audit.events e LEFT JOIN app_runtime.model_attempts m ON m.attempt_id=e.span_id
            LEFT JOIN app_runtime.tool_calls t ON t.call_id=e.call_id
            LEFT JOIN app_runtime.sandbox_jobs j ON j.job_id=e.raw_json->>'job_id'
          WHERE e.trace_id=$1 AND ($3 OR e.event_type NOT LIKE '%model.chunk')
          UNION ALL
          SELECT e.event_id,'run_event',NULL::varchar,e.event_type,e.created_at,NULL::double precision,NULL::text,
            octet_length(e.payload::text),NULL::text,NULL::text,false,NULL::text,NULL::text,e.payload->>'tool_name',
            left(e.payload->>'message',120),NULL::text,NULL::text,NULL::text
          FROM app_runtime.run_events e WHERE e.run_id=$2 AND ($3 OR e.event_type<>'assistant.delta')
            AND NOT EXISTS(SELECT 1 FROM app_audit.events a WHERE a.trace_id=$1 AND a.event_type=e.event_type))
          SELECT * FROM timeline ORDER BY created_at,source,event_id LIMIT $4 OFFSET $5''',run['trace_id'],run_id,chunks,limit+1,(page-1)*limit)
    await record_read(request,context,'audit.turn_events_read',{'run_id':run_id,'page':page,'count':min(len(rows),limit)})
    return {'items':[event_view(row) for row in rows[:limit]],'has_more':len(rows)>limit,'next_page':page+1 if len(rows)>limit else None}


@router.get('/run-events/{event_id}')
async def progress_detail(event_id: int,request: Request,context: AuthContext=Depends(audit_context)):
    """接收持久运行进度编号，返回有界JSON安全视图，后台事件与主审计正文采用同一内联交互。"""
    async with request.app.state.pool.acquire() as conn:
        row=await conn.fetchrow('''SELECT event_id,run_id,trace_id,event_type,created_at,octet_length(payload::text) AS bytes,
            CASE WHEN octet_length(payload::text)<=$2 THEN payload END AS data FROM app_runtime.run_events WHERE event_id=$1''',event_id,DETAIL_BYTES)
    if not row:raise auth_error(404,'NOT_FOUND','运行事件不存在')
    await record_read(request,context,'audit.progress_read',{'event_id':event_id})
    return scrub(dict(row)|{'too_large':row['data'] is None},request.app.state.audit.secrets)


@router.get('/messages/{message_id}')
async def full_message(message_id: ShortId,request: Request,context: AuthContext=Depends(audit_context)):
    """接收原消息编号，在用户展开长消息时返回全文，超页面额度提示下载；不静默截断称完整。"""
    async with request.app.state.pool.acquire() as conn:
        row=await conn.fetchrow('''SELECT message_id,role,octet_length(content) AS bytes,
            CASE WHEN octet_length(content)<=$2 THEN content END AS content FROM app_runtime.messages WHERE message_id=$1''',message_id,DETAIL_BYTES)
    if not row:raise auth_error(404,'NOT_FOUND','消息不存在')
    await record_read(request,context,'audit.message_read',{'message_id':message_id})
    return scrub(dict(row)|{'too_large':row['content'] is None},request.app.state.audit.secrets)


@router.get('/messages/{message_id}/export')
async def message_export(message_id: ShortId,request: Request,context: AuthContext=Depends(audit_context)):
    """接收消息编号，受控下载原消息完整安全JSON，16MiB以上拒绝；不会生成回复或改变消息。"""
    async with request.app.state.pool.acquire() as conn:
        size=await conn.fetchval('SELECT octet_length(content) FROM app_runtime.messages WHERE message_id=$1',message_id)
        if size is None:raise auth_error(404,'NOT_FOUND','消息不存在')
        if size>EXPORT_BYTES-4096:raise auth_error(413,'EXPORT_LIMIT','消息超过16MiB导出限制')
        row=await conn.fetchrow('SELECT message_id,conversation_id,run_id,role,content,created_at FROM app_runtime.messages WHERE message_id=$1',message_id)
    await record_read(request,context,'audit.message_exported',{'message_id':message_id})
    body=json.dumps(scrub(dict(row),request.app.state.audit.secrets),ensure_ascii=False,default=str)
    if len(body.encode())>EXPORT_BYTES:raise auth_error(413,'EXPORT_LIMIT','导出大小超过限制')
    return Response(body,media_type='application/json',headers={'Content-Disposition':f'attachment; filename="message-{message_id}.json"','X-Content-Type-Options':'nosniff'})
