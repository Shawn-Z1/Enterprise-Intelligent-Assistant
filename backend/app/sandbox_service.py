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
from app.sensitive_actions import ActionRequest


class SandboxArguments(BaseModel):
    """模型仅指定本人附件、inspect/execute、语言/代码/目的及图表字段，不能指定运行参数。"""
    model_config=ConfigDict(extra='forbid')
    operation: Literal['inspect','execute']
    attachment_ids: list[str]=Field(default_factory=list,max_length=10)
    language: Literal['python','sql']='python'
    code: str=Field(default='',max_length=32000)
    purpose: str=Field(min_length=1,max_length=2000)
    chart: dict=Field(default_factory=dict)


class SandboxService:
    """绑定可信用户/run的执行服务，复用通用确认、永久审计及应用文件存储。"""
    def __init__(self,pool,settings,audit,hook):
        """接收应用连接池、固定配置、审计与Hook；不创建本地解释器或容器管理接口。"""
        self.pool,self.settings,self.audit,self.hook=pool,settings,audit,hook
        self.storage=KnowledgeStorage(settings)

    def headers(self):
        """无参数返回仅供节点控制的密钥，不能出现在模型工具或文件执行环境。"""
        return {'Authorization':'Bearer '+self.settings.sandbox_control_token.get_secret_value()}

    async def health(self):
        """无参数确认专用服务与固定普通Docker模板可用，失败不降级到应用执行。"""
        try:
            async with httpx.AsyncClient(timeout=4,trust_env=False) as client:
                response=await client.get(self.settings.sandbox_runner_url+'/health',headers=self.headers());response.raise_for_status()
                value=response.json()
                if not value['ready'] or value['policy']['runtime']!='docker' or value['policy']['network']!='none':raise ValueError()
                return value
        except (httpx.HTTPError,ValueError,KeyError):raise auth_error(503,'SANDBOX_UNAVAILABLE','独立Docker沙箱不可用，不会在应用内执行代码') from None

    async def attachments(self,ids,context,run):
        """接收模型附件编号及可信身份/run，返回仅本会话本人文件的固定元数据，越权统一404。"""
        selected=run['state_json'].get('attachment_ids',[])
        if any(ident not in selected for ident in ids):raise auth_error(404,'ATTACHMENT_NOT_FOUND','附件不属于本次用户选择的输入')
        if len(set(ids))!=len(ids):raise auth_error(422,'ATTACHMENT_DUPLICATE','附件编号重复')
        async with self.pool.acquire() as conn:
            rows=await conn.fetch('SELECT * FROM app_runtime.analysis_attachments WHERE attachment_id=ANY($1::varchar[]) AND user_id=$2 AND conversation_id=$3',ids,context.user_id,run['conversation_id'])
        lookup={row['attachment_id']:dict(row) for row in rows}
        if len(lookup)!=len(ids):raise auth_error(404,'ATTACHMENT_NOT_FOUND','附件不存在或不属于本人当前会话')
        if sum(row['size_bytes'] for row in lookup.values())>50*1024*1024:raise auth_error(413,'FILE_LIMIT','本次分析输入合计最多50MiB')
        return [lookup[ident] for ident in ids]

    async def prepare(self,args,context,run,call_id):
        """接收真实工具参数，冻结文件指纹/代码/运行模板；execute仅注册人工确认，不开始执行。"""
        files=await self.attachments(args.attachment_ids,context,run)
        if args.operation=='inspect':return None
        if not args.code.strip():raise auth_error(422,'CODE_REQUIRED','执行须提供完整代码')
        async with self.pool.acquire() as conn:
            count=await conn.fetchval("SELECT count(*) FROM app_runtime.sandbox_attempts WHERE run_id=$1 AND operation='execute'",run['run_id'])
        if count>=3:raise auth_error(429,'SANDBOX_ATTEMPT_LIMIT','同一任务最多执行3次代码，不再请求额外确认')
        health=await self.health()
        request=ActionRequest(action_type='sandbox.execute',tool_name='run_sandbox_code',purpose=args.purpose,
            target={'files':[{key:f[key] for key in ('attachment_id','filename','original_sha256')} for f in files],
                    'policy':health['policy'],'image_id':health['image_id']},payload=args.model_dump())
        return await self.hook.prepare(request,context,run,call_id)

    async def validate(self,request,context,run):
        """接收批准快照及当前身份/run，复验输入指纹和固定镜像/策略，变更须新确认。"""
        args=SandboxArguments(**request.payload)
        files=await self.attachments(args.attachment_ids,context,run);health=await self.health()
        expected=[{key:f[key] for key in ('attachment_id','filename','original_sha256')} for f in files]
        if expected!=request.target['files'] or health['policy']!=request.target['policy'] or health['image_id']!=request.target['image_id']:
            raise auth_error(403,'ACTION_CHANGED','文件或隔离策略变化，请重新确认')

    async def execute(self,args,context,run,call_id,writer,action_id=None):
        """接收可信调用和可选批准，实际受控请求专用节点，保存回执/产物；未知结果不自动重跑。"""
        if args.operation=='execute' and not action_id:raise auth_error(403,'ACTION_NOT_APPROVED','生成代码必须持有本次人工确认，不能直接执行')
        if action_id:
            async def validator(request,current):
                """接收待消费快照和当前身份，执行文件/策略复验，不执行生成代码。"""
                await self.validate(request,current,run)
            action=await self.hook.owned(action_id,context)
            if action['decision']=='completed':
                completed,_=await self.hook.consume(action_id,context,run,validator)
                return completed['result_json']
            args=SandboxArguments(**action['payload_json']['payload'])
        files=await self.attachments(args.attachment_ids,context,run)
        health=await self.health()
        async with self.pool.acquire() as conn:
            prior=await conn.fetchrow('SELECT * FROM app_runtime.sandbox_attempts WHERE call_id=$1',call_id)
            if prior:
                if prior['status']=='completed':return prior['result_json']
                raise auth_error(409,'SANDBOX_RESULT_UNKNOWN','上次执行结果未知，不自动重跑；请重新发起')
            count=await conn.fetchval("SELECT count(*) FROM app_runtime.sandbox_attempts WHERE run_id=$1 AND operation='execute'",run['run_id'])
            if args.operation=='execute' and count>=3:raise auth_error(429,'SANDBOX_ATTEMPT_LIMIT','同一任务最多执行3次代码')
        attempt_id=new_id();packet={'user_id':context.user_id,**args.model_dump(),'files':[]}
        for file in files:
            try:raw=await self.storage.read_verified(file)
            except Exception:
                if action_id:
                    async with self.pool.acquire() as conn:await conn.execute("UPDATE app_runtime.query_approvals SET decision='invalidated' WHERE approval_id=$1 AND decision IN ('pending','approved')",action_id)
                raise
            packet['files'].append({'attachment_id':file['attachment_id'],'name':file['filename'],'format':file['format'],'sha256':file['original_sha256'],'data':base64.b64encode(raw).decode()})
        # 所有原件真实SHA复验成功才消费执行批准；变更输入不会被误标为已开始执行。
        if action_id:await self.hook.consume(action_id,context,run,validator)
        fingerprint=hashlib.sha256(json.dumps(packet,sort_keys=True).encode()).hexdigest()
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO app_runtime.sandbox_attempts(attempt_id,call_id,run_id,user_id,operation,status,request_fingerprint,input_json,node_policy) VALUES($1,$2,$3,$4,$5,'running',$6,$7,$8)",attempt_id,call_id,run['run_id'],context.user_id,args.operation,fingerprint,{'arguments':args.model_dump(),'files':[{'id':f['attachment_id'],'sha256':f['original_sha256']} for f in files]},health)
        await self.audit.append(run['trace_id'],'sandbox.started',{'attempt_id':attempt_id,'fingerprint':fingerprint,'policy':health,'operation':args.operation},user_id=context.user_id,run_id=run['run_id'])
        try:
            async with httpx.AsyncClient(timeout=75,trust_env=False) as client:
                task=asyncio.create_task(client.post(self.settings.sandbox_runner_url+'/jobs/'+attempt_id,headers=self.headers(),json=packet))
                try:
                    while not task.done():
                        await asyncio.wait({task},timeout=.5)
                        await writer.check_active()
                        await live_context(self.pool,context.user_id,run['state_json']['auth_session_id'])
                    response=await task
                    if response.status_code==429:
                        receipt={'ok':False,'error':{'code':'SANDBOX_BUSY','message':'沙箱忙碌，请稍后重新发起；本次没有启动任务容器'},'artifacts':[]}
                    else:response.raise_for_status();receipt=response.json()
                except BaseException:
                    await client.delete(self.settings.sandbox_runner_url+'/jobs/'+attempt_id,headers=self.headers())
                    task.cancel();await asyncio.gather(task,return_exceptions=True);raise
            await writer.check_active()
            current=await live_context(self.pool,context.user_id,run['state_json']['auth_session_id'])
            if current.permission_version!=context.permission_version:raise auth_error(403,'ACTION_INVALIDATED','身份权限版本变化，停止收集和发布')
            output={'citations':[],'message':'沙箱文件检查完成' if args.operation=='inspect' else '隔离代码执行完成','attempt_id':attempt_id,
                    'operation':args.operation,'receipt':{k:v for k,v in receipt.items() if k!='artifacts'}}
            if not receipt.get('ok'):
                output.update(error=receipt.get('error',{'code':'SANDBOX_FAILED','message':'沙箱未成功'}),message='沙箱未成功执行，不生成成功统计或图表')
            else:
                result_id=new_id();data=receipt['result'];artifacts=[]
                for artifact in receipt['artifacts']:
                    raw=base64.b64decode(artifact['data'],validate=True)
                    if hashlib.sha256(raw).hexdigest()!=artifact['sha256']:raise ValueError('产物SHA不符')
                    ident=new_id();key=f'sandbox/{context.user_id}/{result_id}/{ident}'
                    await self.storage.put(key,raw)
                    artifacts.append({'artifact_id':ident,'filename':artifact['name'],'size_bytes':len(raw),'sha256':artifact['sha256'],'object_key':key})
                value={**data,'result_id':result_id,'artifacts':[{k:v for k,v in a.items() if k!='object_key'} for a in artifacts],
                       'input_files':[{k:f[k] for k in ('attachment_id','filename','original_sha256')} for f in files]}
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        current_run=await conn.fetchrow('SELECT status,cancel_requested,lease_owner FROM app_runtime.runs WHERE run_id=$1 FOR UPDATE',run['run_id'])
                        if current_run['status']!='running' or current_run['cancel_requested'] or current_run['lease_owner']!=run.get('lease_owner'):
                            raise auth_error(409,'SANDBOX_RESULT_UNKNOWN','任务已停止或租约变化，不能发布执行产物')
                        await conn.execute('INSERT INTO app_runtime.sandbox_results(result_id,attempt_id,user_id,conversation_id,result_json) VALUES($1,$2,$3,$4,$5)',result_id,attempt_id,context.user_id,run['conversation_id'],value)
                        for artifact in artifacts:await conn.execute('INSERT INTO app_runtime.sandbox_artifacts(artifact_id,result_id,filename,size_bytes,sha256,object_key) VALUES($1,$2,$3,$4,$5,$6)',artifact['artifact_id'],result_id,artifact['filename'],artifact['size_bytes'],artifact['sha256'],artifact['object_key'])
                output.update(result_id=result_id,result=value)
                output['citations']=[{'type':'sandbox','result_id':result_id,'title':'文件检查结果' if args.operation=='inspect' else '沙箱分析结果','content':json.dumps(value,ensure_ascii=False)[:14000]}]
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE app_runtime.sandbox_attempts SET status='completed',result_json=$1,updated_at=now() WHERE attempt_id=$2",output,attempt_id)
            await self.audit.append(run['trace_id'],'sandbox.completed',{'attempt_id':attempt_id,'output':output},user_id=context.user_id,run_id=run['run_id'])
            if action_id:await self.hook.complete(action_id,run,output,'completed')
            return output
        except (BaseException,) as error:
            async with self.pool.acquire() as conn:await conn.execute("UPDATE app_runtime.sandbox_attempts SET status='unknown',updated_at=now() WHERE attempt_id=$1 AND status='running'",attempt_id)
            if action_id:await self.hook.complete(action_id,run,{'citations':[],'error':{'code':'SANDBOX_RESULT_UNKNOWN','message':'执行中断或结果未知，不自动重跑'}},'unknown')
            raise
