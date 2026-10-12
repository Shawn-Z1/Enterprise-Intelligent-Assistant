"""本人文件沙箱：应用只传递受鉴权字节/快照，生成代码始终交给独立Docker服务。"""
import asyncio
import base64
import hashlib
import json
from typing import Literal

import httpx
from pydantic import BaseModel,ConfigDict,Field
from app.auth import auth_error
from app.ids import new_id
from app.knowledge_storage import KnowledgeStorage
from app.rag_service import live_context


class SandboxArguments(BaseModel):
    """模型仅指定本人附件、inspect/execute、语言/代码/目的及图表字段，不能指定运行参数。"""
    model_config=ConfigDict(extra='forbid')
    operation: Literal['inspect','execute']='execute'
    attachment_ids: list[str]=Field(default_factory=list,max_length=10)
    language: Literal['python','sql']='python'
    code: str=Field(default='',max_length=32000)
    purpose: str=Field(min_length=1,max_length=2000)
    chart: dict=Field(default_factory=dict)


class ReadArguments(BaseModel):
    """模型只提供本人附件与有界读取范围，返回真实页/正文/工作表，不接受路径或查询身份。"""
    model_config=ConfigDict(extra='forbid')
    attachment_id: str
    offset: int=Field(default=0,ge=0,le=2_000_000)
    limit: int=Field(default=12000,ge=1,le=12000)
    page: int | None=Field(default=None,ge=1,le=300)
    table: str | None=Field(default=None,max_length=40)
    column_offset: int=Field(default=0,ge=0,le=1999)
    column_limit: int=Field(default=20,ge=1,le=20)


class SandboxPending(Exception):
    """持久任务尚未完成的内部路由信号，不作为模型代码失败或用户需要批准的动作。"""
    def __init__(self, job_id):
        """接收可信任务编号，保存供图暂停节点使用的定位键；不包含文件或凭证。"""
        self.job_id=job_id
        super().__init__(job_id)


class SandboxService:
    """绑定可信用户/run的执行服务，复用持久沙箱队列、永久审计及应用文件存储，不创建文件执行批准。"""
    def __init__(self,pool,settings,audit,hook):
        """接收应用连接池、固定配置、审计与Hook；不创建本地解释器或容器管理接口。"""
        self.pool,self.settings,self.audit=pool,settings,audit
        self.storage=KnowledgeStorage(settings)

    def headers(self):
        """无参数返回仅供节点控制的密钥，不能出现在模型工具或文件执行环境。"""
        return {'Authorization':'Bearer '+self.settings.sandbox_control_token.get_secret_value()}

    async def health(self):
        """无参数确认专用服务与固定普通Docker模板可用，失败不降级到应用执行。"""
        try:
            # 共享健康目录避免只探测DNS随机返回的一个副本；排空/失联节点不提供新的能力。
            async with self.pool.acquire() as conn:
                value=await conn.fetchval('''SELECT manifest FROM app_runtime.sandbox_nodes
                    WHERE expires_at>now() AND NOT draining ORDER BY runner_id LIMIT 1''')
            if value is None or value['policy']['runtime']!='docker' or value['policy']['network']!='none':raise ValueError()
            return value
        except (httpx.HTTPError,ValueError,KeyError):raise auth_error(503,'SANDBOX_UNAVAILABLE','独立Docker沙箱不可用，不会在应用内执行代码') from None

    async def attachments(self,ids,context,run):
        """接收模型附件编号及可信身份/run，返回仅本会话本人文件的固定元数据，越权统一404。"""
        selected=run['state_json'].get('attachment_ids',[])
        if any(ident not in selected for ident in ids):raise auth_error(404,'ATTACHMENT_NOT_FOUND','附件不属于当前会话已发送的输入')
        if len(set(ids))!=len(ids):raise auth_error(422,'ATTACHMENT_DUPLICATE','附件编号重复')
        async with self.pool.acquire() as conn:
            rows=await conn.fetch('SELECT * FROM app_runtime.analysis_attachments WHERE attachment_id=ANY($1::varchar[]) AND user_id=$2 AND conversation_id=$3',ids,context.user_id,run['conversation_id'])
        lookup={row['attachment_id']:dict(row) for row in rows}
        if len(lookup)!=len(ids):raise auth_error(404,'ATTACHMENT_NOT_FOUND','附件不存在或不属于本人当前会话')
        if sum(row['size_bytes'] for row in lookup.values())>50*1024*1024:raise auth_error(413,'FILE_LIMIT','本次分析输入合计最多50MiB')
        return [lookup[ident] for ident in ids]

    async def submit(self, operation, arguments, context, run, writer, call_id=None):
        """接收固定操作、工具参数和可信run，幂等提交PG任务；已完成返回回执，待执行抛出暂停信号。"""
        ids=([arguments['attachment_id']] if operation=='read' else arguments.get('attachment_ids',run['state_json'].get('attachment_ids',[])))
        files=await self.attachments(ids,context,run)
        # 幂等键只来自服务器run/工具调用，模型不能决定是否复用旧代码结果。
        key=run['run_id']+':'+(call_id or 'attachment_prepare')
        async with self.pool.acquire() as conn:
            prior=await conn.fetchrow('SELECT * FROM app_runtime.sandbox_jobs WHERE idempotency_key=$1',key)
        if prior:
            if prior['result_json'] is not None:return prior['result_json']
            raise SandboxPending(prior['job_id'])
        await writer.check_active()
        health=await self.health()
        selected=run['state_json'].get('attachment_ids',[])
        refs=[{key:f[key] for key in ('attachment_id','filename','format','size_bytes','original_sha256','object_key')}|
              {'index':selected.index(f['attachment_id'])+1} for f in files]
        # 提交前检查真实字节，领取时再次检查，排队中修改原件不会进入代码实例。
        for file in files:await self.storage.read_verified(file)
        payload={'arguments':arguments,'files':refs}
        fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest();job_id=new_id()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(7100702)')
                current=await conn.fetchrow('SELECT status,cancel_requested,lease_owner FROM app_runtime.runs WHERE run_id=$1 FOR UPDATE',run['run_id'])
                if current['status']!='running' or current['cancel_requested'] or current['lease_owner']!=run.get('lease_owner'):
                    raise auth_error(409,'RUN_STOPPED','任务已停止，不能提交沙箱操作')
                previous=await conn.fetchrow('SELECT * FROM app_runtime.sandbox_jobs WHERE idempotency_key=$1',key)
                if previous:
                    if previous['result_json'] is not None:return previous['result_json']
                    raise SandboxPending(previous['job_id'])
                queued=await conn.fetchval("SELECT count(*) FROM app_runtime.sandbox_jobs WHERE status='queued'")
                per_user=await conn.fetchval("SELECT count(*) FROM app_runtime.sandbox_jobs WHERE user_id=$1 AND status IN ('queued','running')",context.user_id)
                if queued>=self.settings.sandbox_queue_limit or per_user>=10:
                    raise auth_error(429,'SANDBOX_QUEUE_LIMIT','沙箱积压达到上限，请稍后再试；正常并发满额会自动排队')
                if operation=='execute':
                    if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM app_runtime.sandbox_jobs WHERE run_id=$1 AND status='unknown')",run['run_id']):
                        raise auth_error(409,'SANDBOX_RESULT_UNKNOWN','本轮先前执行结果未知，不能自动再次运行代码；请确认清理后另发任务')
                    attempts=await conn.fetchval("SELECT count(*) FROM app_runtime.sandbox_attempts WHERE run_id=$1 AND operation='execute'",run['run_id'])
                    if attempts>=3:raise auth_error(429,'SANDBOX_ATTEMPT_LIMIT','同一任务最多执行3次代码')
                    if not arguments.get('code','').strip():raise auth_error(422,'CODE_REQUIRED','执行须提供完整代码')
                await conn.execute('''INSERT INTO app_runtime.sandbox_jobs(job_id,run_id,user_id,call_id,idempotency_key,operation,input_json,node_policy,request_fingerprint,expires_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,now()+$10*interval '1 second')''',job_id,run['run_id'],context.user_id,call_id,key,operation,payload,health,fingerprint,self.settings.sandbox_wait_seconds)
                if call_id:
                    await conn.execute("INSERT INTO app_runtime.sandbox_attempts(attempt_id,call_id,run_id,user_id,operation,status,request_fingerprint,input_json,node_policy) VALUES($1,$2,$3,$4,$5,'queued',$6,$7,$8)",job_id,call_id,run['run_id'],context.user_id,operation,fingerprint,payload,health)
                await self.audit.append(run['trace_id'],'sandbox.queued',{'job_id':job_id,'operation':operation,'fingerprint':fingerprint,'files':[{'id':f['attachment_id'],'sha256':f['original_sha256']} for f in files]},user_id=context.user_id,run_id=run['run_id'],connection=conn)
        raise SandboxPending(job_id)

    async def prepare_attachments(self, context, run, writer):
        """接收当前身份/run，强制固定解析本轮附件概况，返回回执或持久等待，不调用审核/生成模型。"""
        return await self.submit('prepare',{'attachment_ids':run['state_json'].get('new_attachment_ids',run['state_json'].get('attachment_ids',[]))},context,run,writer)

    async def execute(self, args, context, run, call_id, writer, action_id=None):
        """接收真实代码调用并自动提交隔离执行；保留旧形参兼容，但批准不再作为文件执行条件。"""
        operation='prepare' if args.operation=='inspect' else 'execute'
        return await self.submit(operation,args.model_dump(),context,run,writer,call_id)

    async def read(self, args, context, run, call_id, writer):
        """接收本人附件及分页参数，提交固定读取任务，不执行模型代码或创建人工确认。"""
        return await self.submit('read',args.model_dump(),context,run,writer,call_id)

    async def guard(self, citations, context, run):
        """接收当前来源与身份，复核文件来源归属和原件SHA，防止旧解析/产物成为变更资料的证据。"""
        for citation in citations:
            if citation.get('type')!='sandbox':continue
            async with self.pool.acquire() as conn:
                result=await conn.fetchval('SELECT result_json FROM app_runtime.sandbox_results WHERE result_id=$1 AND user_id=$2 AND conversation_id=$3',citation['result_id'],context.user_id,run['conversation_id'])
            if result is None:raise auth_error(403,'ATTACHMENT_EVIDENCE_CHANGED','文件来源已失效，停止发送')
            for original in result.get('input_files',[]):
                current=(await self.attachments([original['attachment_id']],context,run))[0]
                if original['original_sha256']!=current['original_sha256']:raise auth_error(403,'ATTACHMENT_EVIDENCE_CHANGED','文件指纹已变化，请重新发送')
                await self.storage.read_verified(current)
