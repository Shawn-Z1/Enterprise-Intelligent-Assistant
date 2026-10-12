import asyncio
import logging
from time import monotonic

from app.audit import AuditRecorder, Usage, redact_secrets
from app.context import ContextAssembler
from app.context_memory import ContextMemory
from app.ids import new_id
from app.model_gateway import GatewayError, ModelGateway, ModelResult
from app.sensitive_actions import HumanSecondaryConfirmationHook
from app.business_sql import BusinessSqlService
from app.sensitive_actions import public_action

logger = logging.getLogger("zhixing.chat")
TERMINAL = {"completed","failed","cancelled","interrupted"}


class RunCancelled(asyncio.CancelledError):
    """当前执行被用户取消或账号禁用；与进程关闭造成的中断分别记录。"""


def public_run(row: dict) -> dict:
    """接收内部执行行，返回本人可见状态及安全错误，排除输入上下文、租约和内部模型响应。"""
    state = row.get('state_json') or {}
    return {key:row[key] for key in ('run_id','trace_id','conversation_id','status','created_at','updated_at')} | {
        'error':state.get('error'),'model':state.get('model'),'slot':state.get('slot'),'cancel_requested':row.get('cancel_requested',False),'pending_action':state.get('pending_action'),'pending_job':state.get('pending_job')}


class RunWriter:
    """所有执行事件先进入 PostgreSQL，SSE 只读取持久事件，不依赖进程内广播。"""

    def __init__(self, pool, audit: AuditRecorder, run: dict):
        """接收低权限连接池、统一审计和任务身份快照，保存写入上下文，不向客户端取所有者。"""
        self.pool,self.audit,self.run = pool,audit,run

    async def append_event(self, conn, kind: str, data: dict) -> dict:
        """接收已锁定执行行的事务连接、事件类型和安全展示数据，追加唯一序号事件并返回记录。"""
        sequence = await conn.fetchval('SELECT COALESCE(max(sequence),0)+1 FROM app_runtime.run_events WHERE run_id=$1',self.run['run_id'])
        return dict(await conn.fetchrow('''INSERT INTO app_runtime.run_events(run_id,trace_id,sequence,event_type,payload)
            VALUES($1,$2,$3,$4,$5) RETURNING event_id,sequence,created_at''',self.run['run_id'],self.run['trace_id'],sequence,kind,data))

    async def emit(self, kind: str, data: dict):
        """接收网关公开事件，在当前任务锁内检查取消和禁用后持久追加；返回无凭证事件，不传隐藏思考。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow('SELECT r.status,r.cancel_requested,r.lease_owner,u.status AS user_status FROM app_runtime.runs r JOIN app_system.users u USING(user_id) WHERE r.run_id=$1 FOR UPDATE OF r',self.run['run_id'])
                if row['cancel_requested'] or row['user_status']!='active':
                    raise RunCancelled()
                if row['status'] in TERMINAL:
                    raise RunCancelled()
                if self.run.get('lease_owner') and row['lease_owner']!=self.run['lease_owner']:raise RunCancelled()
                await self.append_event(conn,kind,data)

    async def check_active(self):
        """无参数只核验本任务仍运行且用户未取消/禁用；缓冲模型输出时不追加无意义的逐token事件。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT r.status,r.cancel_requested,r.lease_owner,u.status AS user_status FROM app_runtime.runs r JOIN app_system.users u USING(user_id) WHERE r.run_id=$1',self.run['run_id'])
        if row is None or row['cancel_requested'] or row['user_status']!='active' or row['status'] in TERMINAL:
            raise RunCancelled()
        if self.run.get('lease_owner') and row['lease_owner']!=self.run['lease_owner']:raise RunCancelled()

    async def suspend(self,action_id):
        """接收已保存检查点的待确认动作，在run锁内发布确认事件并释放租约；等待不占worker执行槽。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                run=await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1 FOR UPDATE',self.run['run_id'])
                if run['cancel_requested'] or run['status']!='running':raise RunCancelled()
                action=await conn.fetchrow('SELECT * FROM app_runtime.query_approvals WHERE approval_id=$1 AND run_id=$2 AND decision=\'pending\'',action_id,self.run['run_id'])
                if action is None:raise GatewayError('ACTION_STATE_CHANGED','待确认动作状态已变化')
                await conn.execute("UPDATE app_runtime.runs SET status='waiting_confirmation',state_json=state_json || $1::jsonb,lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE run_id=$2",{'pending_action':action_id,'resume_job':None,'pending_job':None},self.run['run_id'])
                await self.append_event(conn,'confirmation.required',{'message':'敏感操作尚未执行，请确认本次操作','action':public_action(dict(action)),'status':'waiting_confirmation'})
                await self.audit.append(self.run['trace_id'],'run.waiting_confirmation',{'action_id':action_id},user_id=self.run['user_id'],run_id=self.run['run_id'],connection=conn)

    async def suspend_sandbox(self, job_id):
        """接收已有检查点的沙箱job，在run锁内保存等待并释放worker；处理回执先到的竞争。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                run=await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1 FOR UPDATE',self.run['run_id'])
                if run['cancel_requested'] or run['status']!='running':raise RunCancelled()
                job=await conn.fetchrow('SELECT * FROM app_runtime.sandbox_jobs WHERE job_id=$1 AND run_id=$2',job_id,self.run['run_id'])
                if job is None:raise GatewayError('SANDBOX_STATE_CHANGED','沙箱任务不可恢复')
                ready=job['result_json'] is not None
                state={'pending_job':job_id,'resume_job':job_id if ready else None,'resume_action':None,'pending_action':None}
                await conn.execute('UPDATE app_runtime.runs SET status=$1,state_json=state_json || $2::jsonb,lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE run_id=$3','pending' if ready else 'waiting_sandbox',state,self.run['run_id'])
                await self.append_event(conn,'sandbox.waiting',{'message':'回执已保存，等待继续回答' if ready else '等待附件处理或沙箱执行，空闲后自动继续','status':'pending' if ready else 'waiting_sandbox','job_id':job_id})
                await self.audit.append(self.run['trace_id'],'run.waiting_sandbox',{'job_id':job_id,'ready':ready},user_id=self.run['user_id'],run_id=self.run['run_id'],connection=conn)

    async def record_model(self, kind: str, data: dict, usage: Usage | None):
        """接收模型实际请求、原始包或结果，在同一事务中保存尝试与永久审计；失败则阻止后续模型调用。"""
        cleaned = redact_secrets(data,self.audit.secrets)
        attempt = data.get('attempt_id')
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if kind=='model.request':
                    await conn.execute('''INSERT INTO app_runtime.model_attempts(attempt_id,trace_id,span_id,model_role,provider,model,status,request_json)
                        VALUES($1,$2,$1,$3,$4,$5,'started',$6)''',attempt,self.run['trace_id'],data['slot'],data['provider'],data['model'],cleaned['request'])
                elif kind in ('model.response','model.failed','model.interrupted'):
                    await conn.execute('UPDATE app_runtime.model_attempts SET status=$1,response_json=$2 WHERE attempt_id=$3',
                        'completed' if kind=='model.response' else ('interrupted' if kind=='model.interrupted' else 'failed'),cleaned,attempt)
                await self.audit.append(self.run['trace_id'],kind,cleaned,span_id=attempt or self.run['run_id'],
                    parent_span_id=self.run['run_id'] if attempt else None,span_name='model.attempt' if attempt else 'chat.run',
                    user_id=self.run['user_id'],conversation_id=self.run['conversation_id'],run_id=self.run['run_id'],
                    usage=usage,duration_ms=data.get('duration_ms'),connection=conn)

    async def finish(self, status: str, error: dict | None = None, result: ModelResult | None = None):
        """接收终态、安全错误或成功模型结果，原子落原始回复、终态事件和审计；同一 run 最多一条助手回复。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow('SELECT status,cancel_requested,state_json,lease_owner FROM app_runtime.runs WHERE run_id=$1 FOR UPDATE',self.run['run_id'])
                if row['status'] in TERMINAL and not row['state_json'].get('audit_pending'):
                    return
                # 旧worker失去租约不能覆盖新worker的运行或已恢复队列；本人取消仍可收尾。
                if self.run.get('lease_owner') and row['lease_owner']!=self.run['lease_owner'] and not row['cancel_requested']:return
                if result and row['cancel_requested']:
                    status,result,error = 'cancelled',None,{'code':'RUN_CANCELLED','message':'本次回答已停止'}
                payload = {'status':status,'error':error}
                if result:
                    if result.message.get('tool_calls'):
                        raise GatewayError('TOOLS_NOT_READY','模型返回了工具调用，本阶段尚未开放工具执行')
                    message_id = new_id()
                    protocol = {'provider':result.provider,'model':result.model,'slot':result.slot,'message':result.message,
                                'usage':result.usage.model_dump(mode='json'),'finish_reason':result.finish_reason}
                    await conn.execute('''INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content,protocol_json,run_id)
                        VALUES($1,$2,$3,'assistant',$4,$5,$6)''',message_id,self.run['conversation_id'],self.run['user_id'],result.content,protocol,self.run['run_id'])
                    payload.update(message_id=message_id,model=result.model,slot=result.slot,usage=result.usage.model_dump(mode='json'),finish_reason=result.finish_reason)
                state = {**row['state_json'],**payload,'audit_pending':False}
                await conn.execute('UPDATE app_runtime.runs SET status=$1,state_json=$2,lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE run_id=$3',status,state,self.run['run_id'])
                await conn.execute('UPDATE app_runtime.conversations SET updated_at=now() WHERE conversation_id=$1',self.run['conversation_id'])
                await self.append_event(conn,'run.'+status,payload)
                await self.audit.append(self.run['trace_id'],'run.'+status,payload,span_id=self.run['run_id'],span_name='chat.run',
                    user_id=self.run['user_id'],conversation_id=self.run['conversation_id'],run_id=self.run['run_id'],connection=conn)


class ChatProcessor:
    """后台会话执行器，生产使用真实 LangGraph 工具循环；任务、原消息和事件持久化，模型统一走网关。"""

    def __init__(self, pool, settings, gateway: ModelGateway, audit: AuditRecorder, worker_id: str | None = None, rag=None, web=None):
        """接收连接池、运行配置、网关和审计，保存 worker 标识；允许隔离测试复用真实执行代码。"""
        self.pool,self.settings,self.gateway,self.audit = pool,settings,gateway,audit
        self.worker_id = worker_id or new_id()
        self.rag = rag
        self.web = web
        self.hook=HumanSecondaryConfirmationHook(pool,audit)
        from app.sandbox_service import SandboxService
        self.sandbox=SandboxService(pool,settings,audit,self.hook) if settings.sandbox_enabled else None
        self.business=BusinessSqlService(pool,settings,gateway,audit,self.hook)

    async def claim(self) -> dict | None:
        """无参数，在短事务中通过 SKIP LOCKED 领取一个待执行任务及租约，返回可信身份任务或空。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM app_runtime.runs WHERE status='pending' ORDER BY created_at,run_id FOR UPDATE SKIP LOCKED LIMIT 1")
                if row is None:
                    return None
                await conn.execute("UPDATE app_runtime.runs SET status='running',lease_owner=$1,lease_until=now()+$2*interval '1 second',updated_at=now() WHERE run_id=$3",self.worker_id,self.settings.run_lease_seconds,row['run_id'])
                result = dict(row)
                result['status'] = 'running'
                result['lease_owner']=self.worker_id
                writer = RunWriter(self.pool,self.audit,result)
                await writer.append_event(conn,'run.started',{'message':'已开始处理你的问题'})
                await self.audit.append(row['trace_id'],'run.started',{'worker_id':self.worker_id},span_id=row['run_id'],span_name='chat.run',
                    user_id=row['user_id'],conversation_id=row['conversation_id'],run_id=row['run_id'],connection=conn)
                return result

    async def renew(self, run: dict, task: asyncio.Task):
        """接收任务和执行协程，定期检查取消、禁用及租约；需要停止时取消模型流，不重新发送任务。"""
        while True:
            await asyncio.sleep(1)
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow('SELECT r.cancel_requested,r.status,r.lease_owner,u.status AS user_status FROM app_runtime.runs r JOIN app_system.users u USING(user_id) WHERE run_id=$1',run['run_id'])
                if row['cancel_requested'] or row['user_status']!='active' or row['lease_owner']!=self.worker_id or row['status']!='running':
                    task.cancel()
                    return
                await conn.execute("UPDATE app_runtime.runs SET lease_until=now()+$1*interval '1 second' WHERE run_id=$2 AND lease_owner=$3 AND status='running'",self.settings.run_lease_seconds,run['run_id'],self.worker_id)

    async def recover(self):
        """无参数，明确结束租约已失效的运行并补录审计失败状态；不自动重复可能已发出的模型请求。"""
        if self.sandbox:
            from app.sandbox_queue import recover_queue
            await recover_queue(self.pool,self.audit)
        async with self.pool.acquire() as conn:
            unresolved=await conn.fetch("SELECT a.approval_id,r.* FROM app_runtime.query_approvals a JOIN app_runtime.runs r USING(run_id) WHERE a.decision='executing' AND r.status IN ('failed','cancelled','interrupted')")
            for item in unresolved:
                async with conn.transaction():
                    await conn.execute("UPDATE app_runtime.query_approvals SET decision='unknown',execution_token_hash=NULL WHERE approval_id=$1 AND decision='executing'",item['approval_id'])
                    await self.audit.append(item['trace_id'],'action.unknown',{'action_id':item['approval_id'],'reason':'任务结束前没有持久回执，禁止自动重跑'},user_id=item['user_id'],run_id=item['run_id'],connection=conn)
            # 失效的等待只恢复协议以说明拒绝，不在恢复扫描执行SQL。
            waiting=await conn.fetch("SELECT r.*,a.approval_id FROM app_runtime.runs r JOIN app_runtime.query_approvals a ON a.approval_id=r.state_json->>'pending_action' JOIN app_system.users u ON u.user_id=r.user_id JOIN app_system.auth_sessions s ON s.session_id=r.state_json->>'auth_session_id' WHERE r.status='waiting_confirmation' AND (a.expires_at<=now() OR a.decision<>'pending' OR u.permission_version<>a.permission_version OR u.status<>'active' OR s.revoked_at IS NOT NULL OR s.expires_at<=now())")
            for row in waiting:
                async with conn.transaction():
                    await conn.execute("UPDATE app_runtime.query_approvals SET decision=CASE WHEN expires_at<=now() THEN 'expired' ELSE 'invalidated' END WHERE approval_id=$1 AND decision='pending'",row['approval_id'])
                    await conn.execute("UPDATE app_runtime.runs SET status='pending',state_json=state_json || $1::jsonb WHERE run_id=$2 AND status='waiting_confirmation'",{'resume_action':row['approval_id']},row['run_id'])
            rows = await conn.fetch("SELECT * FROM app_runtime.runs WHERE (status='running' AND lease_until<now()) OR state_json->>'audit_pending'='true'")
        for row in rows:
            if row['state_json'].get('resume_action') and not row['state_json'].get('audit_pending'):
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        # 多副本可能拿到同一过期快照；先锁住并复检当前租约，不能改新worker正在执行的确认动作。
                        expired=await conn.fetchval("SELECT run_id FROM app_runtime.runs WHERE run_id=$1 AND status='running' AND lease_until<now() FOR UPDATE",row['run_id'])
                        if not expired:continue
                        # 已领取但尚未保存回执的执行标未知；后续节点只能回填错误，不重发SQL。
                        await conn.execute("UPDATE app_runtime.query_approvals SET decision='unknown',execution_token_hash=NULL WHERE run_id=$1 AND decision='executing'",row['run_id'])
                        await conn.execute("UPDATE app_runtime.runs SET status='pending',lease_owner=NULL,lease_until=NULL WHERE run_id=$1 AND status='running' AND lease_until<now()",row['run_id'])
                continue
            writer = RunWriter(self.pool,self.audit,dict(row))
            error = row['state_json'].get('error') or {'code':'RUN_INTERRUPTED','message':'后台进程中断，本次回答已结束；可重新发送问题'}
            await writer.finish('failed' if row['state_json'].get('audit_pending') else 'interrupted',error)

    async def process(self, run: dict):
        """接收已领取任务，组装本人历史、调用模型并保存回复；取消、超时和异常均明确终止，不伪造成功。"""
        writer = RunWriter(self.pool,self.audit,run)
        renewal = asyncio.create_task(self.renew(run,asyncio.current_task()))
        started=monotonic()
        memory=ContextMemory(self.pool,self.settings,self.gateway,writer)
        try:
            rows = await memory.history()
            await writer.emit('context.cache',{'message':'已读取会话工作上下文','cache_status':memory.last_cache_status})
            assembler = ContextAssembler([dict(row) for row in rows],run['run_id'],self.settings.model_output_tokens)
            # 普通文本路径也保持同一任务的时间信息固定，不随重新领取变化。
            assembler.timestamp=run['created_at'].isoformat()
            remaining=self.settings.run_timeout_seconds-run['state_json'].get('active_elapsed',0)
            async with asyncio.timeout(max(0.01,remaining)):
                if self.rag:
                    from app.rag_agent import RagAgent
                    result = await RagAgent(self,self.rag,writer,[dict(row) for row in rows],memory=memory).run()
                else:
                    async def factory(slot):
                        """接收目标槽位，用本任务缓存/摘要生成可发送历史；普通文本路径也不能绕过压缩预算。"""
                        return await memory.fit(slot,assembler.assemble(slot,check_budget=False),[],[],writer.check_active)
                    result = await self.gateway.complete(factory,writer.record_model,writer.emit)
                if result is not None:
                    if result.finish_reason=='length':raise GatewayError('MODEL_OUTPUT_LIMIT','模型生成因长度限制结束，未发布截断内容。请将内容分段处理后重试')
                    await writer.finish('completed',result=result)
        except asyncio.CancelledError:
            async with self.pool.acquire() as conn:
                cancelled = await conn.fetchval('SELECT r.cancel_requested OR u.status<>\'active\' FROM app_runtime.runs r JOIN app_system.users u USING(user_id) WHERE run_id=$1',run['run_id'])
            await writer.finish('cancelled' if cancelled else 'interrupted',{'code':'RUN_CANCELLED' if cancelled else 'RUN_INTERRUPTED',
                'message':'本次回答已停止' if cancelled else '后台进程中断，本次回答已结束；可重新发送问题'})
        except Exception as error:
            public = error.public() if isinstance(error,GatewayError) else {'code':'RUN_TIMEOUT' if isinstance(error,TimeoutError) else 'RUN_STORAGE_ERROR',
                'message':'本次回答超时，请缩短问题后重试' if isinstance(error,TimeoutError) else '本次执行遇到存储或审计错误，已停止回答'}
            try:
                await writer.finish('failed',public)
            except Exception:
                # 审计故障时隔离任务，阻止继续外部调用；持久标记待补审计，恢复后追加明确失败事件。
                logger.error('执行存储失败，任务已隔离：%s',run['run_id'])
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE app_runtime.runs SET status='failed',state_json=state_json || $1::jsonb,lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE run_id=$2",
                        {'error':{'code':'AUDIT_UNAVAILABLE','message':'审计存储不可用，本次回答已停止'},'audit_pending':True},run['run_id'])
        finally:
            await memory.close()
            renewal.cancel()
            await asyncio.gather(renewal,return_exceptions=True)
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE app_runtime.runs SET state_json=state_json || jsonb_build_object('active_elapsed',COALESCE((state_json->>'active_elapsed')::double precision,0)+$1::double precision) WHERE run_id=$2",monotonic()-started,run['run_id'])
