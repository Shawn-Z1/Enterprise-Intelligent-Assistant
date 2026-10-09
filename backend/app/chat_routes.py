import asyncio
import hashlib
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import Field, field_validator

from app.auth import StrictBody, auth_error, current_context, mutation_context, security_event
from app.chat_service import RunWriter, TERMINAL, public_run
from app.context import ContextAssembler
from app.ids import ShortId, new_id
from app.model_gateway import GatewayError, ModelSlot
from app.permissions import AuthContext

router = APIRouter(prefix='/api')


class ConversationBody(StrictBody):
    title: str = Field(default='新对话',min_length=1,max_length=120)

    @field_validator('title')
    @classmethod
    def clean_title(cls,value: str) -> str:
        """接收会话标题，去掉两端空白并拒绝空内容，返回用于历史目录的标题。"""
        if not value.strip():
            raise ValueError('标题不能为空')
        return value.strip()


class MessageBody(StrictBody):
    content: str = Field(min_length=1,max_length=16000)
    request_key: ShortId

    @field_validator('content')
    @classmethod
    def clean_content(cls,value: str) -> str:
        """接收用户文字，拒绝纯空白并返回完整输入；不在服务端静默截断原消息。"""
        if not value.strip():
            raise ValueError('消息不能为空')
        return value.strip()


@router.post('/conversations',status_code=201)
async def create_conversation(body: ConversationBody,request: Request,context: AuthContext=Depends(mutation_context)):
    """接收标题和可信当前用户，原子创建本人会话及审计，返回会话元数据。"""
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow('INSERT INTO app_runtime.conversations(conversation_id,user_id,title) VALUES($1,$2,$3) RETURNING conversation_id,title,created_at,updated_at',new_id(),context.user_id,body.title)
            await security_event(request,'chat.conversation_created',{'conversation_id':row['conversation_id'],'title':body.title},context,conn)
    return dict(row)


@router.patch('/conversations/{conversation_id}')
async def rename_conversation(conversation_id: ShortId,body: ConversationBody,request: Request,context: AuthContext=Depends(mutation_context)):
    """接收会话编号、新标题和当前用户，仅重命名本人会话并审计，返回更新元数据。"""
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow('UPDATE app_runtime.conversations SET title=$1,updated_at=now() WHERE conversation_id=$2 AND user_id=$3 RETURNING conversation_id,title,created_at,updated_at',body.title,conversation_id,context.user_id)
            if row is None:
                raise auth_error(404,'NOT_FOUND','会话不存在或不可访问')
            await security_event(request,'chat.conversation_renamed',{'conversation_id':conversation_id,'title':body.title},context,conn)
    return dict(row)


@router.post('/conversations/{conversation_id}/messages',status_code=202)
async def send_message(conversation_id: ShortId,body: MessageBody,request: Request,context: AuthContext=Depends(mutation_context)):
    """接收本人会话、文字及幂等键，预检上下文并原子保存用户原文与待执行任务；不在 HTTP 请求中调用模型。"""
    pool,settings = request.app.state.pool,request.app.state.settings
    fingerprint = hashlib.sha256((conversation_id+'\0'+body.content).encode()).hexdigest()
    async with pool.acquire() as conn:
        async with conn.transaction():
            owned = await conn.fetchrow('SELECT c.title FROM app_runtime.conversations c JOIN app_system.users u USING(user_id) WHERE conversation_id=$1 AND c.user_id=$2 AND u.status=\'active\' FOR UPDATE OF c',conversation_id,context.user_id)
            if owned is None:
                raise auth_error(404,'NOT_FOUND','会话不存在或不可访问')
            previous = await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE user_id=$1 AND request_key=$2',context.user_id,body.request_key)
            if previous:
                if previous['state_json'].get('request_fingerprint')!=fingerprint:
                    raise auth_error(409,'REQUEST_KEY_REUSED','发送标识已用于其他消息，请重新发送')
                return public_run(dict(previous))
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM app_runtime.runs WHERE conversation_id=$1 AND status IN ('pending','running'))",conversation_id):
                raise auth_error(409,'CONVERSATION_BUSY','这段会话正在生成回答，请等待结束或停止后再发送')
            run_id,message_id = new_id(),new_id()
            rows = [dict(row) for row in await conn.fetch('SELECT * FROM app_runtime.messages WHERE conversation_id=$1 AND user_id=$2 ORDER BY message_order',conversation_id,context.user_id)]
            rows.append({'role':'user','content':body.content,'run_id':run_id,'protocol_json':{}})
            slot = ModelSlot('primary',settings.primary_base_url,settings.primary_api_key.get_secret_value(),settings.primary_model,settings.primary_context_tokens)
            try:
                ContextAssembler(rows,run_id,settings.model_output_tokens).assemble(slot)
            except GatewayError as error:
                raise auth_error(413,error.code,error.message)
            run = dict(await conn.fetchrow('''INSERT INTO app_runtime.runs(run_id,trace_id,conversation_id,user_id,request_key,state_json)
                VALUES($1,$2,$3,$4,$5,$6) RETURNING *''',run_id,request.state.trace_id,conversation_id,context.user_id,body.request_key,
                {'request_fingerprint':fingerprint,'user_message_id':message_id,'auth_session_id':context.session_id}))
            await conn.execute('INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content,run_id) VALUES($1,$2,$3,\'user\',$4,$5)',message_id,conversation_id,context.user_id,body.content,run_id)
            title = ' '.join(body.content.split())[:36] if owned['title']=='新对话' else owned['title']
            await conn.execute('UPDATE app_runtime.conversations SET title=$1,updated_at=now() WHERE conversation_id=$2',title,conversation_id)
            await RunWriter(pool,request.app.state.audit,run).append_event(conn,'run.queued',{'message':'消息已保存，等待后台处理','user_message_id':message_id})
            await request.app.state.audit.append(request.state.trace_id,'chat.message_received',{'message_id':message_id,'content':body.content,'request_key':body.request_key},
                user_id=context.user_id,conversation_id=conversation_id,run_id=run_id,connection=conn)
    return public_run(run)


async def owned_run(run_id: str,request: Request,context: AuthContext) -> dict:
    """接收执行编号、请求及可信用户，读取本人任务；不存在和他人任务都返回 404。"""
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1 AND user_id=$2',run_id,context.user_id)
    if row is None:
        await security_event(request,'resources.access_rejected',{'resource':'run','resource_id':run_id},context)
        raise auth_error(404,'NOT_FOUND','执行记录不存在或不可访问')
    return dict(row)


@router.get('/runs/{run_id}')
async def run_status(run_id: ShortId,request: Request,context: AuthContext=Depends(current_context)):
    """接收执行编号和当前身份，返回本人当前运行状态，不暴露原始上下文或模型请求。"""
    return public_run(await owned_run(run_id,request,context))


@router.post('/runs/{run_id}/cancel')
async def cancel_run(run_id: ShortId,request: Request,context: AuthContext=Depends(mutation_context)):
    """接收本人执行编号，持久请求停止并审计；已结束任务幂等返回状态，后台关闭真实模型连接。"""
    run = await owned_run(run_id,request,context)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1 AND user_id=$2 FOR UPDATE',run_id,context.user_id)
            if row['status'] in TERMINAL:
                return public_run(dict(row))
            await conn.execute('UPDATE app_runtime.runs SET cancel_requested=true,updated_at=now() WHERE run_id=$1',run_id)
            await security_event(request,'chat.cancel_requested',{'run_id':run_id},context,conn)
    if row['status']=='pending':
        await RunWriter(request.app.state.pool,request.app.state.audit,run).finish('cancelled',{'code':'RUN_CANCELLED','message':'本次回答已停止'})
    return {'cancel_requested':True}


@router.get('/runs/{run_id}/events')
async def run_events(run_id: ShortId,request: Request,after: int=Query(0,ge=0),context: AuthContext=Depends(current_context)):
    """接收本人任务和补发序号，按持久事件输出 SSE；断线只补发事件，不再次调用模型。"""
    await owned_run(run_id,request,context)
    last_header = request.headers.get('last-event-id','')
    if last_header:
        if not last_header.isdigit():
            raise auth_error(422,'INVALID_CURSOR','事件补发序号不正确')
        after = max(after,int(last_header))
    async with request.app.state.pool.acquire() as conn:
        latest = await conn.fetchval('SELECT COALESCE(max(sequence),0) FROM app_runtime.run_events WHERE run_id=$1',run_id)
    if after>latest:
        raise auth_error(422,'INVALID_CURSOR','事件补发序号超过当前记录')

    async def stream():
        """无参数，轮询本人持久事件并定期重检会话；产出 SSE 帧，断线结束当前读取，不取消后台任务。"""
        cursor = after
        while not await request.is_disconnected():
            async with request.app.state.pool.acquire() as conn:
                authorized = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM app_system.auth_sessions s JOIN app_system.users u USING(user_id) WHERE s.session_id=$1 AND s.user_id=$2 AND s.revoked_at IS NULL AND s.expires_at>now() AND u.status=\'active\')',context.session_id,context.user_id)
                if not authorized:
                    yield 'event: stream.end\ndata: '+json.dumps({'status':'unauthenticated'})+'\n\n'
                    return
                # 先读取状态再读事件，防止两次查询之间提交终态时遗漏最后一批事件。
                state = await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1 AND user_id=$2',run_id,context.user_id)
                rows = await conn.fetch('SELECT * FROM app_runtime.run_events WHERE run_id=$1 AND sequence>$2 ORDER BY sequence LIMIT 100',run_id,cursor)
            for row in rows:
                cursor = row['sequence']
                value = {'event_id':row['event_id'],'sequence':cursor,'run_id':run_id,'trace_id':row['trace_id'],
                         'timestamp':row['created_at'],'type':row['event_type'],'stage':'model' if row['event_type'].startswith(('model.','assistant.')) else 'runtime','data':row['payload']}
                yield f'id: {cursor}\nevent: run.event\ndata: '+json.dumps(jsonable_encoder(value),ensure_ascii=False)+'\n\n'
            if state['status'] in TERMINAL and len(rows)<100:
                yield 'event: stream.end\ndata: '+json.dumps(jsonable_encoder(public_run(dict(state))),ensure_ascii=False)+'\n\n'
                return
            if not rows:
                yield ': heartbeat\n\n'
            await asyncio.sleep(0.3)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
