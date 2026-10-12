"""M07C有限验收：隔离PG，实际两个Runner/worker容器、十任务排队及少量真实模型；不做压力矩阵。"""
import asyncio
import csv
import io
import json
import time
import unittest
from pathlib import Path

import httpx
import test_m04 as base
from app.config import Settings
from app.chat_service import ChatProcessor,RunWriter
from app.ids import new_id
from app.knowledge_storage import KnowledgeStorage
from app.model_gateway import ModelGateway
from app.rag_service import live_context
from app.sandbox_service import SandboxArguments,SandboxPending
from app.sandbox_queue import recover_queue
from test_m07b import FileFlow
from test_m07 import M07Flow


class Scaling(unittest.IsolatedAsyncioTestCase):
    """使用临时数据库和真实服务副本，固定测试代码验证基础设施，真实模型验证检查点接续。"""
    login=base.ChatTests.login
    upload=FileFlow.upload
    processor=FileFlow.processor

    async def asyncSetUp(self):
        """创建隔离数据库、复用可信节点镜像/网络并启动两个测试Runner，不读取主库业务资料。"""
        await base.ChatTests.asyncSetUp(self)
        self.actual=Settings();self.evidence=[];self.containers=[];self.runner_ids=[];self.samples=[]
        names=('sandbox_runner_url','sandbox_control_token','app_minio_endpoint','app_minio_access_key','app_minio_secret_key','app_minio_bucket',
            'primary_base_url','primary_api_key','primary_model','fallback_base_url','fallback_api_key','fallback_model','review_base_url','review_api_key','review_model')
        self.settings=self.settings.model_copy(update={name:getattr(self.actual,name) for name in names}|{'sandbox_enabled':True,'primary_context_tokens':65536,'fallback_context_tokens':65536,'review_context_tokens':65536})
        self.storage=KnowledgeStorage(self.settings);self.api.app.state.settings=self.settings;self.api.app.state.knowledge_storage=self.storage
        # Docker socket仅授予可信验收驱动；用户代码仍只在无socket的任务客体执行。
        self.engine=httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds='/var/run/docker.sock'),base_url='http://docker',timeout=100)
        self.tag='m07c-'+new_id().replace('_','').replace('-','').lower();self.alias=self.tag+'-api'
        self.control_headers={'Authorization':'Bearer '+self.actual.sandbox_control_token.get_secret_value()}
        self.remote=httpx.AsyncClient(base_url='http://'+self.alias+':8000',timeout=20,trust_env=False)
        try:
            info=await self.docker('GET','/info');self.evidence.append({'engine':{key:info[key] for key in ('OSType','Architecture','NCPU','MemTotal')}})
            self.api_id=await self.clone('api',{'POSTGRES_DB':self.name,'SANDBOX_GLOBAL_CONCURRENCY':'2','SANDBOX_USER_CONCURRENCY':'1'},alias=self.alias)
            await self.until(lambda:self.remote_ready(),40)
            self.runners=[await self.clone('sandbox-runner',{'SANDBOX_API_URL':'http://'+self.alias+':8000/internal/sandbox','SANDBOX_RUNNER_ID':'','SANDBOX_RUNNER_CONCURRENCY':'1'}) for _ in range(2)]
            await self.until(lambda:self.nodes_ready(2),40)
        except BaseException:
            await self.cleanup();await base.ChatTests.asyncTearDown(self);raise

    async def docker(self,method,path,**kwargs):
        """接收可信Docker方法和路径，返回JSON或空结果；错误只显示引擎安全响应，不输出环境凭证。"""
        response=await self.engine.request(method,path,**kwargs)
        self.assertLess(response.status_code,400,response.text[:800])
        return response.json() if response.content else None

    async def clone(self,service,changes,alias=None,entrypoint=None):
        """接收项目服务与有限测试覆盖，创建同镜像临时副本；仅改独立库/容量，不开放测试端口。"""
        found=await self.docker('GET','/containers/json',params={'filters':json.dumps({'label':['com.docker.compose.project=zhixing','com.docker.compose.service='+service]})})
        self.assertTrue(found,'主服务须先启动：'+service)
        source=await self.docker('GET','/containers/'+found[0]['Id']+'/json')
        env=dict(item.split('=',1) for item in source['Config']['Env']);env.update(changes);env.pop('HOSTNAME',None)
        name=self.tag+'-'+service+'-'+str(len(self.containers))
        host={'NetworkMode':'zhixing-network','Binds':source['HostConfig'].get('Binds') or [],'Memory':source['HostConfig'].get('Memory',0),'NanoCpus':source['HostConfig'].get('NanoCpus',0)}
        config={'Image':source['Config']['Image'],'Env':[key+'='+value for key,value in env.items()],'Cmd':source['Config']['Cmd'],
            'Entrypoint':entrypoint or source['Config']['Entrypoint'],'WorkingDir':source['Config']['WorkingDir'],'User':source['Config']['User'],
            'Labels':{'zhixing.m07c-test':self.tag},'HostConfig':host,
            'NetworkingConfig':{'EndpointsConfig':{'zhixing-network':{'Aliases':[alias or name]}}}}
        created=await self.docker('POST','/containers/create',params={'name':name},json=config);ident=created['Id'];self.containers.append(ident)
        if service=='sandbox-runner':self.runner_ids.append(ident[:12])
        await self.docker('POST','/containers/'+ident+'/start');return ident

    async def remote_ready(self):
        """无参数读取临时API真实依赖健康，返回是否可以接收节点控制。"""
        try:return (await self.remote.get('/api/health/ready')).status_code==200
        except httpx.HTTPError:return False

    async def nodes_ready(self,count):
        """接收预期健康副本数，核对测试库节点心跳，返回真实节点是否就绪。"""
        return await self.admin.fetchval('SELECT count(*) FROM app_runtime.sandbox_nodes WHERE expires_at>now() AND NOT draining')==count

    async def until(self,predicate,seconds=90):
        """接收异步条件和有限时限，逐步等待真实状态；超时明确失败，不能以固定睡眠冒充完成。"""
        limit=time.monotonic()+seconds
        while time.monotonic()<limit:
            if await predicate():return
            await asyncio.sleep(.2)
        self.fail('等待实际状态超时')

    async def reserve(self,headers,index):
        """接收虚构编号，经实际路由保存原件与用户任务；返回未执行run，不调用模型。"""
        raw=f'marker,amount\ncase-{index},{index+1}\ncase-{index},{2*(index+1)}\n'.encode()
        conversation,ids=self.upload(headers,[(f'case-{index}.csv',raw)])
        response=self.api.post('/api/conversations/'+conversation+'/messages',headers=headers,json={'content':'固定基础设施回归任务 '+str(index),'attachment_ids':ids,'request_key':new_id()})
        self.assertEqual(response.status_code,202,response.text)
        return {'run':response.json()['run_id'],'user':None,'headers':headers,'index':index,'ids':ids}

    async def enqueue(self,case,run,delay):
        """接收已领取run及固定代码参数，提交真实生产队列并暂停Agent占位，返回关联任务。"""
        processor=self.processor();index=case['index'];ids=case['ids']
        writer=RunWriter(self.pool,self.audit,run);context=await live_context(self.pool,run['user_id'],run['state_json']['auth_session_id']);call=new_id()
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO app_runtime.tool_calls(call_id,run_id,tool_name,input_json,status,idempotency_key) VALUES($1,$2,'run_sandbox_code','{}','running',$3)",call,run['run_id'],new_id())
        code=f"import time\ntime.sleep({delay})\nf=next(iter(tables.values()))\nassert set(f['marker'])=={{'case-{index}'}}\nemit_result([{{'marker':'case-{index}','total':int(f['amount'].sum())}}],description='虚构并发回归结果')"
        args=SandboxArguments(attachment_ids=ids,code=code,purpose='固定代码基础设施验收')
        with self.assertRaises(SandboxPending) as waiting:await processor.sandbox.execute(args,context,run,call,writer)
        await writer.suspend_sandbox(waiting.exception.job_id)
        return case|{'job':waiting.exception.job_id,'user':run['user_id']}

    async def submit(self,headers,index,delay=1.5):
        """接收单个虚构任务，经实际路由及领取后入队；仅用于少量确定性生命周期故障。"""
        case=await self.reserve(headers,index);run=await self.processor().claim()
        self.assertEqual(run['run_id'],case['run']);return await self.enqueue(case,run,delay)

    async def finish_manual(self,cases):
        """接收固定任务清单，关闭测试驱动的Agent占位；不伪造模型回答，真实产物只由Runner回执产生。"""
        for case in cases:
            row=await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',case['run'])
            if row['status'] not in ('cancelled','failed','completed','interrupted'):await RunWriter(self.pool,self.audit,dict(row)).finish('completed')

    async def batch(self,headers,label):
        """接收两用户和配置名，提交十个真实客体任务，核对容量、排队、唯一回执及文件数值。"""
        # 先保存十条run、原子领取，再同时提交沙箱；真实Runner自行领取，不由测试驱动逐个执行。
        pending=[await self.reserve(headers[index%2],index) for index in range(10)]
        processor=self.processor();claimed=await asyncio.gather(*(processor.claim() for _ in pending))
        by_run={run['run_id']:run for run in claimed}
        cases=await asyncio.gather(*(self.enqueue(case,by_run[case['run']],1.5) for case in pending))
        deadline=time.monotonic()+100;points=[]
        while time.monotonic()<deadline:
            rows=await self.admin.fetch('SELECT status,count(*) AS n FROM app_runtime.sandbox_jobs WHERE job_id=ANY($1::varchar[]) GROUP BY status',[x['job'] for x in cases]);counts={x['status']:x['n'] for x in rows}
            point={'running':counts.get('running',0),'queued':counts.get('queued',0),'completed':counts.get('completed',0)};points.append(point)
            self.assertLessEqual(point['running'],2)
            users=await self.admin.fetch("SELECT user_id,count(*) AS n FROM app_runtime.sandbox_jobs WHERE status='running' GROUP BY user_id")
            self.assertTrue(all(x['n']<=1 for x in users))
            if point['completed']==10:break
            self.assertFalse(counts.get('failed') or counts.get('unknown'),counts)
            await asyncio.sleep(.15)
        self.assertEqual(points[-1]['completed'],10,points[-1]);self.assertGreaterEqual(max(x['queued'] for x in points),5)
        rows=await self.admin.fetch('SELECT j.job_id,j.runner_id,j.lease_version,j.created_at,j.started_at,j.finished_at,r.result_json FROM app_runtime.sandbox_jobs j JOIN app_runtime.sandbox_results r USING(job_id) WHERE j.job_id=ANY($1::varchar[])',[x['job'] for x in cases])
        self.assertEqual(len(rows),10);by_id={row['job_id']:row for row in rows};values=[]
        for case in cases:
            row=by_id[case['job']];self.assertEqual(row['lease_version'],1)
            self.assertEqual(row['result_json']['rows'][0]['total'],3*(case['index']+1));values.append(row['result_json']['rows'][0])
            artifact=row['result_json']['artifacts'][0]['artifact_id']
            response=self.api.get('/api/sandbox/artifacts/'+artifact,headers=case['headers']);self.assertEqual(response.status_code,200)
            parsed=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))));self.assertEqual(int(parsed[0]['total']),3*(case['index']+1))
            other=headers[1] if case['index']%2==0 else headers[0]
            self.assertEqual(self.api.get('/api/sandbox/artifacts/'+artifact,headers=other).status_code,404)
        self.evidence.append({'phase':label,'count':10,'max_running':max(x['running'] for x in points),'max_queued':max(x['queued'] for x in points),
            'runners':sorted({row['runner_id'] for row in rows}),'jobs':[{'job':row['job_id'],'wait_seconds':(row['started_at']-row['created_at']).total_seconds(),'execution_seconds':(row['finished_at']-row['started_at']).total_seconds()} for row in rows]})
        await self.finish_manual(cases);return values

    async def jobs_done(self,cases):
        """接收少量任务，返回是否均已终止并真正释放客体额度，取消/未知不能被成功回执覆盖。"""
        count=await self.admin.fetchval("SELECT count(*) FROM app_runtime.sandbox_jobs WHERE job_id=ANY($1::varchar[]) AND status IN ('completed','failed','cancelled','unknown') AND resources_released",[x['job'] for x in cases])
        return count==len(cases)

    async def test_ten_jobs_single_and_multi_runner(self):
        """同一十任务批次分别由双/单Runner执行，核对真实等待、有限容量和结果一致。"""
        users=[self.login('zhangsan'),self.login('lisi')]
        multiple=await self.batch(users,'two-runners-ten-jobs')
        self.assertEqual(len(self.evidence[-1]['runners']),2);self.assertEqual(self.evidence[-1]['max_running'],2)
        await self.docker('POST','/containers/'+self.runners[1]+'/stop',params={'t':90})
        await self.until(lambda:self.nodes_ready(1),20)
        single=await self.batch(users,'one-runner-ten-jobs');self.assertEqual(single,multiple);self.assertEqual(self.evidence[-1]['max_running'],1)

    async def test_lifecycle_and_worker_handoff(self):
        """少量目标任务验证重启/取消/旧回执，并由实际模型验证worker检查点接续。"""
        users=[self.login('zhangsan'),self.login('lisi')]
        await self.lifecycle(users);await self.real_dialogue(users)

    async def lifecycle(self,users):
        """接收两用户，验证运行中重启不清理另节点、排空后退出、排队/运行取消和旧租约拒绝。"""
        cases=[await self.submit(users[0],20,8),await self.submit(users[1],21,8),await self.submit(users[0],22,1)]
        async def both_running():return await self.admin.fetchval("SELECT count(*) FROM app_runtime.sandbox_jobs WHERE status='running'")==2
        await self.until(both_running,20)
        async def guests_started():
            """无参数确认两个领取任务的客体均已创建并运行，不把PG预约额度误当实际容器。"""
            guests=await self.docker('GET','/containers/json',params={'filters':json.dumps({'label':['zhixing.sandbox=task']})})
            return len([x for x in guests if x['Labels'].get('zhixing.job') in (cases[0]['job'],cases[1]['job'])])==2
        await self.until(guests_started,15)
        self.api.post('/api/runs/'+cases[2]['run']+'/cancel',headers=cases[2]['headers']);await recover_queue(self.pool,self.audit)
        row=await self.admin.fetchrow('SELECT * FROM app_runtime.sandbox_jobs WHERE job_id=$1',cases[0]['job'])
        response=await self.remote.post('/internal/sandbox/jobs/'+row['job_id']+'/complete',headers=self.control_headers,json={'runner_id':row['runner_id'],'instance_id':row['node_instance_id'],'lease_version':0,'receipt':{'ok':True,'result':{'text':'假成功'},'artifacts':[]}})
        self.assertEqual(response.json(),{'saved':False})
        conflict=await self.remote.post('/internal/sandbox/nodes/heartbeat',headers=self.control_headers,json={'runner_id':row['runner_id'],'instance_id':new_id(),'capacity':1,'manifest':row['node_policy']})
        self.assertEqual(conflict.status_code,409)
        ident=next(x for x in self.runners if x.startswith(row['runner_id']))
        sibling=cases[1]['job'];before=(await self.docker('GET','/containers/json',params={'filters':json.dumps({'label':['zhixing.job='+sibling]})}))[0]['Id']
        restart=asyncio.create_task(self.docker('POST','/containers/'+ident+'/restart',params={'t':90}))
        await asyncio.sleep(.8)
        still=await self.docker('GET','/containers/'+before+'/json');self.assertTrue(still['State']['Running'])
        await restart;await self.until(lambda:self.nodes_ready(2),40);await self.until(lambda:self.jobs_done(cases),40)
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_runtime.sandbox_jobs WHERE job_id=$1',cases[2]['job']),'cancelled')
        await self.finish_manual(cases)
        running=await self.submit(users[0],23,10)
        async def started():return await self.admin.fetchval("SELECT status='running' FROM app_runtime.sandbox_jobs WHERE job_id=$1",running['job'])
        await self.until(started,20)
        self.api.post('/api/runs/'+running['run']+'/cancel',headers=running['headers']);await recover_queue(self.pool,self.audit)
        await self.until(lambda:self.jobs_done([running]),20)
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_runtime.sandbox_jobs WHERE job_id=$1',running['job']),'cancelled')
        unknown=await self.submit(users[0],24,10)
        async def started_unknown():return await self.admin.fetchval("SELECT status='running' FROM app_runtime.sandbox_jobs WHERE job_id=$1",unknown['job'])
        await self.until(started_unknown,20)
        await self.admin.execute("UPDATE app_runtime.sandbox_jobs SET lease_until=now()-interval '1 second' WHERE job_id=$1",unknown['job']);await recover_queue(self.pool,self.audit)
        held=await self.admin.fetchrow('SELECT status,resources_released FROM app_runtime.sandbox_jobs WHERE job_id=$1',unknown['job'])
        self.assertEqual(held['status'],'unknown');self.assertFalse(held['resources_released'])
        await self.until(lambda:self.jobs_done([unknown]),20);self.assertEqual(await self.admin.fetchval('SELECT lease_version FROM app_runtime.sandbox_jobs WHERE job_id=$1',unknown['job']),1)
        # 模拟容器已关闭但确认消息丢失，旧控制器已移除时只恢复该过期节点，不重跑任务。
        row=await self.admin.fetchrow('SELECT runner_id FROM app_runtime.sandbox_jobs WHERE job_id=$1',unknown['job'])
        owner=next(x for x in self.runners if x.startswith(row['runner_id']))
        await self.docker('POST','/containers/'+owner+'/stop',params={'t':90})
        await self.admin.execute('UPDATE app_runtime.sandbox_jobs SET resources_released=false WHERE job_id=$1',unknown['job'])
        helper=await self.clone('sandbox-runner',{'SANDBOX_API_URL':'http://'+self.alias+':8000/internal/sandbox','SANDBOX_RUNNER_ID':row['runner_id']},entrypoint=['python','/app/recover_node.py'])
        async def recovered():return not (await self.docker('GET','/containers/'+helper+'/json'))['State']['Running']
        await self.until(recovered,20)
        self.assertEqual((await self.docker('GET','/containers/'+helper+'/json'))['State']['ExitCode'],0)
        self.assertTrue(await self.admin.fetchval('SELECT resources_released FROM app_runtime.sandbox_jobs WHERE job_id=$1',unknown['job']))
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_runtime.sandbox_jobs WHERE job_id=$1',unknown['job']),'unknown')
        await self.docker('POST','/containers/'+owner+'/start');await self.until(lambda:self.nodes_ready(2),40)
        await self.finish_manual([unknown]);self.evidence.append({'phase':'lifecycle','sibling_container_survived':True,'queued_cancel':True,'running_cancel':True,'stale_completion_denied':True,'duplicate_node_denied':True,'unknown_not_reexecuted':True,'orphan_recovery_released_only_closed_resources':True})

    async def real_dialogue(self,users):
        """接收两用户，实际模型生成统计代码；第一worker暂停后退出，另一worker从PG检查点完成答案。"""
        first=await self.clone('worker',{'POSTGRES_DB':self.name,'WORKER_CONCURRENCY':'1'})
        conversation,ids=self.upload(users[0],[('real-demo.csv',b'team,amount\nA,10\nA,20\nB,5\n')])
        response=self.api.post('/api/conversations/'+conversation+'/messages',headers=users[0],json={'content':'仅依据本条CSV，在沙箱实际运行Python，先time.sleep(8)再按team汇总amount，生成结果CSV。输出总计，不联网、不查内部库。','attachment_ids':ids,'request_key':new_id()})
        self.assertEqual(response.status_code,202,response.text);ident=response.json()['run_id']
        async def waiting_code():return await self.admin.fetchval("SELECT EXISTS(SELECT 1 FROM app_runtime.sandbox_jobs j JOIN app_runtime.runs r USING(run_id) WHERE j.run_id=$1 AND j.operation='execute' AND r.status='waiting_sandbox')",ident)
        await self.until(waiting_code,100)
        initial=await self.admin.fetchval("SELECT raw_json->>'worker_id' FROM app_audit.events WHERE run_id=$1 AND event_type='run.started' ORDER BY created_at LIMIT 1",ident)
        second=await self.clone('worker',{'POSTGRES_DB':self.name,'WORKER_CONCURRENCY':'1'})
        await self.docker('POST','/containers/'+first+'/stop',params={'t':75})
        conversation2,ids2=self.upload(users[1],[('other-demo.csv',b'team,amount\nX,7\nX,8\n')])
        response=self.api.post('/api/conversations/'+conversation2+'/messages',headers=users[1],json={'content':'仅用本条CSV在沙箱实际按team汇总amount，生成CSV下载，说明总计。不联网、不查内部库。','attachment_ids':ids2,'request_key':new_id()})
        self.assertEqual(response.status_code,202,response.text);other=response.json()['run_id']
        async def finished():return await self.admin.fetchval("SELECT count(*) FROM app_runtime.runs WHERE run_id=ANY($1::varchar[]) AND status='completed'",[ident,other])==2
        await self.until(finished,120)
        owners=await self.admin.fetch("SELECT raw_json->>'worker_id' AS worker FROM app_audit.events WHERE run_id=$1 AND event_type='run.started'",ident)
        self.assertGreaterEqual(len({row['worker'] for row in owners}),2)
        for run in (ident,other):
            reply=await self.admin.fetchrow("SELECT content,protocol_json FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",run)
            self.assertEqual(reply['protocol_json']['message']['answer_review']['status'],'approved',reply['content'])
            self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_runtime.tool_calls WHERE run_id=$1 AND tool_name='run_sandbox_code' AND status='completed'",run),1)
            self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.query_approvals WHERE run_id=$1',run),0)
        self.assertIn('35',(await self.admin.fetchval("SELECT content FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",ident)))
        self.evidence.append({'phase':'actual-model-worker-handoff','runs':[ident,other],'worker_ids':sorted({row['worker'] for row in owners}),'original_worker':initial,'approved_answers':2})
        await self.docker('POST','/containers/'+second+'/stop',params={'t':75})

    async def cleanup(self):
        """无参数只停止并移除本测试副本；客体输入由Runner清理，持久回执保留，不操作主服务或共享原文件。"""
        for ident in reversed(self.containers):
            try:
                await self.engine.post('/containers/'+ident+'/stop',params={'t':90})
                await self.engine.delete('/containers/'+ident,params={'force':'true'})
            except httpx.HTTPError:pass
        await self.remote.aclose();await self.engine.aclose()

    async def asyncTearDown(self):
        """保存无密钥证据，移除测试副本/对象/独立库；断言关闭前数据已持久化。"""
        folder=Path('/verification/M07C');folder.mkdir(parents=True,exist_ok=True)
        (folder/(self._testMethodName+'.json')).write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        await self.cleanup()
        rows=await self.admin.fetch('SELECT object_key FROM app_runtime.analysis_attachments UNION ALL SELECT object_key FROM app_runtime.sandbox_artifacts')
        for row in rows:await self.storage.remove_new(row['object_key'])
        await base.ChatTests.asyncTearDown(self)


class RecoveryFence(M07Flow):
    """复用SQL确定性边界夹具，仅验证多worker租约争用，不把夹具当真实模型成功证据。"""

    async def asyncTearDown(self):
        """保存本阶段租约守卫的有限证据，再复用原隔离库清理，不删除主库资料。"""
        folder=Path('/verification/M07C');folder.mkdir(parents=True,exist_ok=True)
        (folder/'lease-fence.json').write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        await super().asyncTearDown()

    async def test_expired_confirmed_run_claimed_once(self):
        """两个worker同时恢复/领取过期确认任务；未知SQL不重发，旧owner不能覆盖新租约或重复回复。"""
        run,action,_,headers=await self.wait_action();self.decide(headers,action)
        old=await self.processor().claim()
        await self.admin.execute("UPDATE app_runtime.query_approvals SET decision='executing',consumed_at=now() WHERE approval_id=$1",action['approval_id'])
        await self.admin.execute("UPDATE app_runtime.runs SET lease_until=now()-interval '1 second' WHERE run_id=$1",run['run_id'])
        workers=[self.processor(),self.processor()]
        await asyncio.gather(*(worker.recover() for worker in workers))
        self.assertEqual(await self.admin.fetchval('SELECT decision FROM app_runtime.query_approvals WHERE approval_id=$1',action['approval_id']),'unknown')
        claimed=await asyncio.gather(*(worker.claim() for worker in workers))
        self.assertEqual(sum(row is not None for row in claimed),1)
        index=next(i for i,row in enumerate(claimed) if row is not None);fresh=claimed[index]
        await RunWriter(self.pool,self.audit,old).finish('interrupted',{'code':'OLD_OWNER','message':'旧进程不能覆盖'})
        self.assertEqual(await self.admin.fetchval('SELECT lease_owner FROM app_runtime.runs WHERE run_id=$1',run['run_id']),fresh['lease_owner'])
        await workers[index].process(fresh)
        self.assertEqual(self.sent,[])
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",run['run_id']),1)
        self.evidence.append({'run_id':run['run_id'],'unique_claim':True,'sql_executions':0,'assistant_messages':1,'old_owner_rejected':True})
