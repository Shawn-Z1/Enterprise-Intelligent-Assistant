"""PG沙箱队列的可信控制接口；Runner经内部鉴权领取任务，不持有应用数据库或对象存储凭证。"""
import base64
import hashlib
import hmac
import json
from pathlib import PurePosixPath

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from app.auth import auth_error
from app.chat_service import RunWriter, TERMINAL
from app.ids import new_id, ShortId
from app.rag_service import live_context

router=APIRouter(prefix='/internal/sandbox',include_in_schema=False)
FORMATS={'txt','md','csv','xlsx','docx','pdf','png'}


class NodeBody(BaseModel):
    """可信Runner的领取/续租字段，严格限制名称、容量与实际领取版本。"""
    model_config=ConfigDict(extra='forbid')
    runner_id: str=Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    instance_id: ShortId
    capacity: int=Field(default=2,ge=1,le=16)
    image_id: str=Field(default='',max_length=100)
    lease_version: int=Field(default=0,ge=0)


class CompletionBody(NodeBody):
    """任务完成回执只来自可信控制器，原始代码不能调用此接口或持有控制令牌。"""
    receipt: dict


class HeartbeatBody(NodeBody):
    """接收节点固定能力与排空状态；启动清理完成后才开放领取，不接受模型提交。"""
    manifest: dict
    draining: bool=False
    cleaned: bool=False


@router.post('/nodes/heartbeat')
async def node_heartbeat(body: HeartbeatBody,request: Request,authorization: str=Header(default='')):
    """接收节点身份/能力/排空状态，原子注册及续期；活节点身份冲突拒绝，清理后释放旧代次额度。"""
    authorize(request,authorization)
    manifest=body.manifest
    if not manifest.get('image_id') or manifest.get('policy',{}).get('network')!='none' or manifest.get('policy',{}).get('runtime')!='docker':
        raise auth_error(422,'SANDBOX_NODE_POLICY','节点能力必须来自受限Docker模板')
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(7100702)')
            prior=await conn.fetchrow('SELECT *,expires_at>now() AS live FROM app_runtime.sandbox_nodes WHERE runner_id=$1 FOR UPDATE',body.runner_id)
            if prior and prior['live'] and prior['instance_id']!=body.instance_id:raise auth_error(409,'SANDBOX_NODE_CONFLICT','Runner身份仍被其他启动代次占用')
            await conn.execute('''INSERT INTO app_runtime.sandbox_nodes(runner_id,instance_id,capacity,manifest,draining)
                VALUES($1,$2,$3,$4,$5) ON CONFLICT(runner_id) DO UPDATE SET instance_id=EXCLUDED.instance_id,
                capacity=EXCLUDED.capacity,manifest=EXCLUDED.manifest,draining=EXCLUDED.draining,heartbeat_at=now(),expires_at=now()+interval '30 seconds' ''',
                body.runner_id,body.instance_id,body.capacity,manifest,body.draining)
            if body.cleaned:
                # 新节点已清理本归属孤儿后，旧代次任务只能结束为未知，绝不能重跑或覆盖为成功。
                jobs=await conn.fetch('''SELECT * FROM app_runtime.sandbox_jobs WHERE runner_id=$1
                    AND node_instance_id IS DISTINCT FROM $2 AND NOT resources_released FOR UPDATE''',body.runner_id,body.instance_id)
                for job in jobs:
                    if job['status']=='running':await finish_error(conn,job,request.app.state.audit,'SANDBOX_RESULT_UNKNOWN','原执行节点重启，结果未知，不自动重跑','unknown',released=True)
                    else:await conn.execute('UPDATE app_runtime.sandbox_jobs SET resources_released=true WHERE job_id=$1',job['job_id'])
    return {'registered':True}


@router.post('/nodes/stopped')
async def node_stopped(body: NodeBody,request: Request,authorization: str=Header(default='')):
    """接收已经排空的节点身份，立即关闭其容量；只允许本启动代次释放，不影响其他副本。"""
    authorize(request,authorization)
    async with request.app.state.pool.acquire() as conn:
        value=await conn.fetchval('''UPDATE app_runtime.sandbox_nodes SET draining=true,expires_at=now()
            WHERE runner_id=$1 AND instance_id=$2 RETURNING runner_id''',body.runner_id,body.instance_id)
    return {'stopped':bool(value)}


@router.get('/status')
async def queue_status(request: Request,authorization: str=Header(default='')):
    """接收控制鉴权，返回有限节点/队列/持有额度指标；不返回文件、代码、密钥或业务资料。"""
    authorize(request,authorization)
    async with request.app.state.pool.acquire() as conn:
        nodes=await conn.fetch('SELECT runner_id,instance_id,capacity,draining,expires_at>now() AS live FROM app_runtime.sandbox_nodes ORDER BY runner_id')
        jobs=await conn.fetch('SELECT status,count(*) AS count FROM app_runtime.sandbox_jobs GROUP BY status')
        held=await conn.fetchval('SELECT count(*) FROM app_runtime.sandbox_jobs WHERE NOT resources_released')
        loads=await conn.fetch('SELECT runner_id,count(*) AS held FROM app_runtime.sandbox_jobs WHERE NOT resources_released GROUP BY runner_id')
    capacity=sum(row['capacity'] for row in nodes if row['live'] and not row['draining'])
    by_node={row['runner_id']:row['held'] for row in loads}
    free=sum(max(0,row['capacity']-by_node.get(row['runner_id'],0)) for row in nodes if row['live'] and not row['draining'])
    return {'nodes':[dict(row) for row in nodes],'jobs':{row['status']:row['count'] for row in jobs},'held_slots':held,
        'healthy_capacity':capacity,'global_limit':request.app.state.settings.sandbox_global_concurrency,
        'available_slots':max(0,min(free,request.app.state.settings.sandbox_global_concurrency-held))}


def authorize(request, value):
    """接收请求和Bearer，恒时验证独立控制令牌；空配置或错误令牌均拒绝。"""
    token=request.app.state.settings.sandbox_control_token.get_secret_value()
    if not token or not hmac.compare_digest(value or '', 'Bearer '+token):raise auth_error(401,'SANDBOX_CONTROL_DENIED','控制鉴权失败')


def error_output(code, message):
    """接收真实错误类别和安全说明，返回不能冒充成功资料的固定工具观察。"""
    return {'citations':[],'error':{'code':code,'message':message},'message':message,'receipt':{'ok':False}}


async def wake(conn, job, audit):
    """接收已终态任务和同一事务连接，恢复正在等待该job的Agent；不会凭回执批准业务SQL。"""
    run=await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1 FOR UPDATE',job['run_id'])
    if run['status']=='waiting_sandbox' and run['state_json'].get('pending_job')==job['job_id'] and not run['cancel_requested']:
        await conn.execute("UPDATE app_runtime.runs SET status='pending',state_json=state_json || $1::jsonb,lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE run_id=$2",{'resume_job':job['job_id']},job['run_id'])
        await RunWriter(None,audit,dict(run)).append_event(conn,'sandbox.ready',{'message':'沙箱回执已保存，等待继续回答','status':'pending'})


async def finish_error(conn, job, audit, code, message, status='failed',released=None):
    """接收已锁任务和错误，保存有限回执并唤醒Agent；终态不能再次触发代码执行。"""
    output=error_output(code,message)
    released=job['resources_released'] if released is None else released
    await conn.execute('UPDATE app_runtime.sandbox_jobs SET status=$1,result_json=$2,cancel_requested=true,resources_released=$4,finished_at=now(),updated_at=now() WHERE job_id=$3',status,output,job['job_id'],released)
    if job['call_id']:await conn.execute("UPDATE app_runtime.sandbox_attempts SET status=$1,result_json=$2,updated_at=now() WHERE attempt_id=$3",status,output,job['job_id'])
    run=await conn.fetchrow('SELECT trace_id FROM app_runtime.runs WHERE run_id=$1',job['run_id'])
    await audit.append(run['trace_id'],'sandbox.'+status,{'job_id':job['job_id'],'error':output['error']},user_id=job['user_id'],run_id=job['run_id'],connection=conn)
    await wake(conn,job,audit)


async def recover_queue(pool, audit):
    """接收共享池和审计，处理有界等待、失效身份、取消与租约失联；结果未知不重新领取执行。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows=await conn.fetch('''SELECT j.*,r.cancel_requested AS run_cancel,r.status AS run_status,u.status AS user_status,
                (s.revoked_at IS NULL AND s.expires_at>now()) AS authenticated,
                j.expires_at<=now() AS expired,j.lease_until<=now() AS stale
                FROM app_runtime.sandbox_jobs j JOIN app_runtime.runs r USING(run_id)
                JOIN app_system.users u ON u.user_id=j.user_id LEFT JOIN app_system.auth_sessions s ON s.session_id=r.state_json->>'auth_session_id'
                WHERE j.status IN ('queued','running') AND (r.cancel_requested OR u.status<>'active' OR s.session_id IS NULL OR s.revoked_at IS NOT NULL OR s.expires_at<=now()
                OR r.status IN ('completed','failed','cancelled','interrupted') OR (j.status='queued' AND j.expires_at<=now()) OR (j.status='running' AND j.lease_until<=now()))
                FOR UPDATE OF j SKIP LOCKED''')
            for job in rows:
                cancelled=job['run_cancel'] or job['run_status'] in TERMINAL or job['user_status']!='active' or not job['authenticated']
                if cancelled:await finish_error(conn,job,audit,'SANDBOX_CANCELLED','任务已取消或登录失效，未发布新的产物','cancelled')
                elif job['status']=='queued':await finish_error(conn,job,audit,'SANDBOX_WAIT_TIMEOUT','沙箱排队超过时限，本次未执行代码')
                else:await finish_error(conn,job,audit,'SANDBOX_RESULT_UNKNOWN','执行节点失联或租约过期；结果未知，不自动重跑','unknown')


@router.post('/claim')
async def claim(body: NodeBody, request: Request, authorization: str=Header(default='')):
    """接收可信节点容量/镜像，在短事务原子领取一个有额度的任务；返回受鉴权输入字节或空。"""
    authorize(request,authorization);pool=request.app.state.pool;settings=request.app.state.settings
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 全局领取锁协调总额度与同用户执行数；本地Semaphore不是共享真源。
            await conn.execute('SELECT pg_advisory_xact_lock(7100702)')
            node=await conn.fetchrow('''SELECT * FROM app_runtime.sandbox_nodes WHERE runner_id=$1 AND instance_id=$2
                AND expires_at>now() AND NOT draining''',body.runner_id,body.instance_id)
            if node is None or node['capacity']!=body.capacity or node['manifest']['image_id']!=body.image_id:return {'job':None}
            # total是共享队列中已领取的执行数量，保证所有节点共用全局上限。
            total=await conn.fetchval("SELECT count(*) FROM app_runtime.sandbox_jobs WHERE NOT resources_released")
            # local是该Runner持有的额度，与全局额度分别检查。
            local=await conn.fetchval("SELECT count(*) FROM app_runtime.sandbox_jobs WHERE NOT resources_released AND runner_id=$1",body.runner_id)
            if total>=settings.sandbox_global_concurrency or local>=body.capacity:return {'job':None}
            job=await conn.fetchrow('''SELECT j.* FROM app_runtime.sandbox_jobs j JOIN app_runtime.runs r USING(run_id)
                WHERE j.status='queued' AND NOT j.cancel_requested AND j.expires_at>now() AND NOT r.cancel_requested
                AND r.status IN ('running','waiting_sandbox') AND j.node_policy->>'image_id'=$1 AND j.node_policy->'policy'=$2 AND j.node_policy->>'parser_version'=$4
                AND (SELECT count(*) FROM app_runtime.sandbox_jobs active WHERE active.user_id=j.user_id AND NOT active.resources_released)<$3
                ORDER BY j.created_at,j.job_id FOR UPDATE OF j SKIP LOCKED LIMIT 1''',body.image_id,node['manifest']['policy'],settings.sandbox_user_concurrency,node['manifest']['parser_version'])
            if job is None:return {'job':None}
            run=await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',job['run_id'])
            version=job['lease_version']+1
            await conn.execute("UPDATE app_runtime.sandbox_jobs SET status='running',runner_id=$1,node_instance_id=$4,resources_released=false,lease_version=$2,lease_until=now()+interval '30 seconds',started_at=now(),updated_at=now() WHERE job_id=$3",body.runner_id,version,job['job_id'],body.instance_id)
            if job['call_id']:await conn.execute("UPDATE app_runtime.sandbox_attempts SET status='running' WHERE attempt_id=$1",job['job_id'])
            await request.app.state.audit.append(run['trace_id'],'sandbox.claimed',{'job_id':job['job_id'],'runner_id':body.runner_id,'lease_version':version},user_id=job['user_id'],run_id=job['run_id'],connection=conn)
    try:
        await live_context(pool,job['user_id'],run['state_json']['auth_session_id'])
        packet={'operation':job['operation'],'user_id':job['user_id'],'files':[],'job_id':job['job_id'],'lease_version':version,
                **job['input_json']['arguments']}
        # 后端只传经过SHA复核的固定输入，队列表不保存Base64或对象存储密钥。
        for file in job['input_json']['files']:
            raw=await request.app.state.knowledge_storage.read_verified(file)
            packet['files'].append({'attachment_id':file['attachment_id'],'name':file['filename'],'format':file['format'],
                'index':file['index'],'sha256':file['original_sha256'],'data':base64.b64encode(raw).decode()})
        if job['operation']=='read':packet['read']=job['input_json']['arguments']
        packet['operation']=job['operation']
        return {'job':packet}
    except Exception:
        async with pool.acquire() as conn:
            async with conn.transaction():
                current=await conn.fetchrow('SELECT * FROM app_runtime.sandbox_jobs WHERE job_id=$1 FOR UPDATE',job['job_id'])
                if current['status']=='running':await finish_error(conn,current,request.app.state.audit,'ATTACHMENT_EVIDENCE_CHANGED','输入文件或身份已变化，本次没有执行代码',released=True)
        return {'job':None}


@router.post('/jobs/{job_id}/renew')
async def renew(job_id: ShortId, body: NodeBody, request: Request, authorization: str=Header(default='')):
    """接收实际领取版本，复检身份和取消并续租；旧版本/终态要求节点停止，不允许重新执行。"""
    authorize(request,authorization)
    async with request.app.state.pool.acquire() as conn:
        row=await conn.fetchrow('SELECT j.*,r.state_json,r.cancel_requested AS run_cancel,r.status AS run_status FROM app_runtime.sandbox_jobs j JOIN app_runtime.runs r USING(run_id) WHERE job_id=$1',job_id)
        node=await conn.fetchval('SELECT 1 FROM app_runtime.sandbox_nodes WHERE runner_id=$1 AND instance_id=$2 AND expires_at>now()',body.runner_id,body.instance_id)
        if not node or row is None or row['status']!='running' or row['runner_id']!=body.runner_id or row['node_instance_id']!=body.instance_id or row['lease_version']!=body.lease_version or row['cancel_requested'] or row['run_cancel'] or row['run_status'] in TERMINAL:return {'continue':False}
        try:await live_context(request.app.state.pool,row['user_id'],row['state_json']['auth_session_id'])
        except Exception:return {'continue':False}
        changed=await conn.fetchval("UPDATE app_runtime.sandbox_jobs SET lease_until=now()+interval '30 seconds',updated_at=now() WHERE job_id=$1 AND status='running' AND lease_version=$2 RETURNING job_id",job_id,body.lease_version)
    return {'continue':bool(changed)}


@router.post('/jobs/{job_id}/complete')
async def complete(job_id: ShortId, body: CompletionBody, request: Request, authorization: str=Header(default='')):
    """接收可信节点回执，校验租约/身份/产物并持久化，再唤醒Agent；重复回执不重复保存产物。"""
    authorize(request,authorization);storage=request.app.state.knowledge_storage;new_keys=[]
    if len(json.dumps(body.receipt))>30*1024*1024:raise auth_error(413,'SANDBOX_OUTPUT_LIMIT','回执超限')
    try:
        async with request.app.state.pool.acquire() as conn:
            async with conn.transaction():
                job=await conn.fetchrow('SELECT * FROM app_runtime.sandbox_jobs WHERE job_id=$1 FOR UPDATE',job_id)
                if job is None:raise auth_error(404,'NOT_FOUND','任务不存在')
                if job['runner_id']!=body.runner_id or job['node_instance_id']!=body.instance_id or job['lease_version']!=body.lease_version:return {'saved':False}
                if job['status'] in ('completed','failed','cancelled','unknown'):
                    # 同一真实执行已销毁容器后的迟到通知可释放额度，但不能覆盖终态或再次唤醒。
                    await conn.execute('UPDATE app_runtime.sandbox_jobs SET resources_released=true WHERE job_id=$1',job_id)
                    return {'saved':job['status'] in ('completed','failed')}
                if job['status']!='running':return {'saved':False}
                run=await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1 FOR UPDATE',job['run_id'])
                if run['cancel_requested'] or run['status'] in TERMINAL:
                    await finish_error(conn,job,request.app.state.audit,'SANDBOX_CANCELLED','任务已停止，迟到结果不恢复成功','cancelled',released=True);return {'saved':False}
                await live_context(request.app.state.pool,job['user_id'],run['state_json']['auth_session_id'])
                receipt=body.receipt
                if not receipt.get('ok'):
                    error=receipt.get('error') or {'code':'SANDBOX_FAILED','message':'隔离执行未完成'}
                    await finish_error(conn,job,request.app.state.audit,error['code'],error['message'],'unknown' if error['code']=='SANDBOX_RESULT_UNKNOWN' else 'failed',released=True)
                    return {'saved':True}
                result_id=new_id();artifacts=[];total=0
                for artifact in receipt.get('artifacts',[]):
                    name=artifact['name'];suffix=name.rsplit('.',1)[-1].lower()
                    if PurePosixPath(name).name!=name or '\\' in name or len(name)>180 or suffix not in FORMATS:raise ValueError('产物名称/格式非法')
                    raw=base64.b64decode(artifact['data'],validate=True);total+=len(raw)
                    if total>20*1024*1024 or hashlib.sha256(raw).hexdigest()!=artifact['sha256']:raise ValueError('产物大小/指纹非法')
                    ident=new_id();key=f'sandbox/{job["user_id"]}/{result_id}/{ident}'
                    new_keys.append(key);await storage.put(key,raw)
                    artifacts.append({'artifact_id':ident,'filename':name,'size_bytes':len(raw),'sha256':artifact['sha256'],'object_key':key})
                value=receipt['result']|{'result_id':result_id,'operation':job['operation'],'artifacts':[{k:v for k,v in x.items() if k!='object_key'} for x in artifacts],
                    'stdout':receipt.get('stdout','')[:65536],'duration_ms':receipt.get('duration_ms'),'input_files':[{k:f[k] for k in ('attachment_id','filename','original_sha256')} for f in job['input_json']['files']]}
                title={'prepare':'附件预处理概况','read':'附件详细读取','execute':'沙箱执行结果'}[job['operation']]
                output={'operation':job['operation'],'message':title+'已完成','result':value,'result_id':result_id,'receipt':{'ok':True},
                    'citations':[{'type':'sandbox','result_id':result_id,'title':title,'content':json.dumps(value,ensure_ascii=False)[:14000]}]}
                await conn.execute('INSERT INTO app_runtime.sandbox_results(result_id,attempt_id,job_id,user_id,conversation_id,result_json) VALUES($1,$2,$3,$4,$5,$6)',result_id,job_id if job['call_id'] else None,job_id,job['user_id'],run['conversation_id'],value)
                for x in artifacts:await conn.execute('INSERT INTO app_runtime.sandbox_artifacts(artifact_id,result_id,filename,size_bytes,sha256,object_key) VALUES($1,$2,$3,$4,$5,$6)',x['artifact_id'],result_id,x['filename'],x['size_bytes'],x['sha256'],x['object_key'])
                await conn.execute("UPDATE app_runtime.sandbox_jobs SET status='completed',resources_released=true,result_json=$1,finished_at=now(),updated_at=now() WHERE job_id=$2",output,job_id)
                if job['call_id']:await conn.execute("UPDATE app_runtime.sandbox_attempts SET status='completed',result_json=$1,updated_at=now() WHERE attempt_id=$2",output,job_id)
                await request.app.state.audit.append(run['trace_id'],'sandbox.completed',{'job_id':job_id,'runner_id':body.runner_id,'output':output},user_id=job['user_id'],run_id=job['run_id'],connection=conn)
                await wake(conn,job,request.app.state.audit)
        return {'saved':True}
    except BaseException:
        for key in new_keys:await storage.remove_new(key)
        raise
