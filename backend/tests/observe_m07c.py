"""M07C实时并发观测：复用隔离验收环境，真实worker/模型/Runner执行，终端只输出调度元数据。"""
import argparse
import asyncio
import io
import csv
import json
import logging
import signal
import tarfile
import time
from pathlib import Path

from app.config import Settings
from app.ids import new_id
from app.timezones import beijing_now
from test_m07c import Scaling


class Console:
    """同步刷新终端和UTF8日志，不输出Cookie、密钥、代码、模型思考或文件正文。"""

    def __init__(self):
        """无参数创建本次观测日志，保存后续控制台时间线；文件位于被Git忽略的验收目录。"""
        folder=Path('/verification/M07C');folder.mkdir(parents=True,exist_ok=True)
        self.path=folder/('live-console-'+beijing_now().strftime('%Y%m%d-%H%M%S')+'.log')
        self.handle=self.path.open('w',encoding='utf-8')

    def write(self,kind,text):
        """接收阶段名和安全元数据，立即输出北京时间日志；返回无值，不缓冲等待测试结束。"""
        line=f"[{beijing_now().strftime('%H:%M:%S.%f')[:-3]}] [{kind}] {text}"
        print(line,flush=True);self.handle.write(line+'\n');self.handle.flush()

    def close(self):
        """无参数关闭本次日志文件，不操作测试之外的文件。"""
        self.handle.close()


class Timeline:
    """从已提交PG审计、队列和实际Docker容器观测状态，不模拟任何节点归属。"""

    def __init__(self,harness,console):
        """接收隔离测试驱动和输出器，建立短标签与去重集合；标签只对应真实任务/节点。"""
        self.h=harness;self.out=console;self.cases={};self.workers={};self.runners={}
        self.owners={};self.job_labels={};self.job_runs={};self.producers={}
        # 审计ID可能在事务提交前分配，使用集合去重，避免高ID先提交导致遗漏低ID。
        self.seen=set();self.guests={};self.queue_state=None;self.max_pending=0;self.max_queued=0;self.max_running=0

    def task(self,ident):
        """接收真实run编号，返回稳定对话任务标签；未知编号原样标明，不猜测来源。"""
        return self.cases.get(ident,{}).get('label',ident or '未知任务')

    def job(self,ident):
        """接收真实job编号，生成稳定沙箱任务标签；包含解析、详细读取和代码执行各自的任务。"""
        if ident not in self.job_labels:self.job_labels[ident]=f'J{len(self.job_labels)+1:02d}'
        return self.job_labels[ident]

    def worker(self,ident):
        """接收PG保存的实际worker身份，返回映射名称及短编号，不把驱动进程冒充worker。"""
        return f"{self.workers.get(ident,'Worker身份绑定中')}({ident or '尚未取得身份'})"

    def runner(self,ident):
        """接收PG/Docker标签中的runner_id，返回真实副本名称及身份。"""
        return f"{self.runners.get(ident,'Runner未知')}({ident or '尚未领取'})"

    async def bind_worker(self,container,label):
        """接收实际worker容器和展示名，从其心跳文件读取身份；不会读取环境配置或凭证。"""
        async def ready():
            response=await self.h.engine.get('/containers/'+container+'/archive',params={'path':'/tmp/zhixing-worker-health.json'})
            if response.status_code!=200:return False
            with tarfile.open(fileobj=io.BytesIO(response.content)) as archive:
                member=next((entry for entry in archive if entry.isfile() and entry.size<32768),None)
                if member is None:return False
                state=json.loads(archive.extractfile(member).read())
            if not state.get('ready'):return False
            self.workers[state['worker_id']]=label
            self.out.write('节点',f"{label} 身份={state['worker_id']}，每实例额度={state['capacity']}，容器={container[:12]}")
            return True
        await self.h.until(ready,40)

    async def snapshot(self):
        """无参数读取本次对话的已提交审计和队列，输出新增节点动作及发生变化的队列列表。"""
        ids=list(self.cases)
        if not ids:return
        async with self.h.pool.acquire() as conn:
            events=await conn.fetch('''SELECT event_id,event_type,run_id,created_at,
                raw_json->>'worker_id' AS worker,raw_json->>'runner_id' AS runner,raw_json->>'job_id' AS job,
                raw_json->>'operation' AS operation,raw_json->>'slot' AS slot,raw_json->>'model' AS model,
                raw_json->'error'->>'code' AS error FROM app_audit.events
                WHERE run_id=ANY($1::varchar[]) ORDER BY event_id''',ids)
            runs=await conn.fetch('SELECT run_id,status,lease_owner FROM app_runtime.runs WHERE run_id=ANY($1::varchar[]) ORDER BY created_at,run_id',ids)
            jobs=await conn.fetch('SELECT job_id,run_id,status,operation,runner_id,resources_released FROM app_runtime.sandbox_jobs WHERE run_id=ANY($1::varchar[]) ORDER BY created_at,job_id',ids)
        for row in jobs:self.job_runs[row['job_id']]=row['run_id'];self.job(row['job_id'])
        operations={'prepare':'附件预处理','read':'详细读取','execute':'执行代码'}
        for row in events:
            if row['event_id'] in self.seen:continue
            kind=row['event_type'];ident=row['run_id'];task=self.task(ident)
            # 启动心跳可能晚于首个领取审计；身份未绑定时保留事件，随后用真实Worker1/2名称补读。
            if kind=='run.started' and row['worker'] not in self.workers:continue
            if kind!='run.started' and ident not in self.owners:continue
            self.seen.add(row['event_id'])
            if kind=='run.started':
                self.owners[ident]=row['worker'];self.out.write('Worker领取',f"{self.worker(row['worker'])} 接收 {task}，开始/接续执行")
            elif kind=='sandbox.queued':
                self.producers[row['job']]=self.owners.get(ident)
                self.out.write('提交沙箱队列',f"{self.worker(self.producers[row['job']])} 将 {task}/{self.job(row['job'])}（{operations.get(row['operation'],row['operation'])}）入队；由空闲Runner领取")
            elif kind=='sandbox.claimed':
                self.out.write('Runner领取',f"{self.runner(row['runner'])} 领取 {task}/{self.job(row['job'])}；提交者={self.worker(self.producers.get(row['job']))}")
            elif kind=='run.waiting_sandbox':
                self.out.write('Worker释放槽位',f"{self.worker(self.owners.get(ident))} 暂停 {task}，等待 {self.job(row['job'])}；该worker可处理其他任务")
            elif kind in ('sandbox.completed','sandbox.failed','sandbox.cancelled','sandbox.unknown'):
                self.out.write('沙箱回执',f"{task}/{self.job(row['job'])} 状态={kind.split('.')[-1]}；Runner={self.runner(row['runner']) if row['runner'] else '见领取记录'}；{'可恢复Agent' if kind=='sandbox.completed' else '按真实错误处理'}")
            elif kind=='model.request':
                role={'primary':'主模型','fallback':'备用模型','review':'审核模型'}.get(row['slot'],row['slot'] or '模型')
                self.out.write('模型调用',f"{task}：{self.worker(self.owners.get(ident))} 请求{role} {row['model'] or ''}")
            elif kind in ('run.completed','run.failed','run.interrupted','run.cancelled'):
                self.out.write('对话结束',f"{task} 状态={kind.split('.')[-1]}，执行者={self.worker(self.owners.get(ident))}"+(f"，错误={row['error']}" if row['error'] else ''))
        guests=await self.h.docker('GET','/containers/json',params={'filters':json.dumps({'label':['zhixing.sandbox=task']})})
        visible={}
        for guest in guests:
            ident=guest['Labels'].get('zhixing.job')
            if ident not in self.job_runs:continue
            visible[guest['Id']]=guest
            if guest['Id'] not in self.guests:
                self.out.write('容器运行',f"{self.runner(guest['Labels'].get('zhixing.runner'))} 拉起 {guest['Names'][0].lstrip('/')}，处理 {self.task(self.job_runs[ident])}/{self.job(ident)}，状态={guest['State']}")
        remembered=dict(visible)
        for ident,guest in self.guests.items():
            if ident not in visible:
                response=await self.h.engine.get('/containers/'+ident+'/json')
                job=guest['Labels']['zhixing.job']
                if response.status_code==404:
                    self.out.write('容器销毁',f"{guest['Names'][0].lstrip('/')} 已由Docker移除，任务={self.task(self.job_runs[job])}/{self.job(job)}")
                else:
                    if not guest.get('_exited_logged'):self.out.write('容器退出',f"{guest['Names'][0].lstrip('/')} 已停止运行，等待控制器销毁")
                    guest['_exited_logged']=True;remembered[ident]=guest
        self.guests=remembered
        pending=[self.task(row['run_id']) for row in runs if row['status']=='pending']
        working=[f"{self.task(row['run_id'])}@{self.worker(row['lease_owner'])}" for row in runs if row['status']=='running']
        waiting=[self.task(row['run_id']) for row in runs if row['status']=='waiting_sandbox']
        queued=[f"{self.task(row['run_id'])}/{self.job(row['job_id'])}:{operations[row['operation']]}" for row in jobs if row['status']=='queued']
        executing=[f"{self.task(row['run_id'])}/{self.job(row['job_id'])}@{self.runner(row['runner_id'])}" for row in jobs if row['status']=='running']
        completed=sum(row['status']=='completed' for row in runs)
        self.max_pending=max(self.max_pending,len(pending));self.max_queued=max(self.max_queued,len(queued));self.max_running=max(self.max_running,len(executing))
        state=(pending,working,waiting,queued,executing,completed)
        if state!=self.queue_state:
            self.queue_state=state
            self.out.write('Worker队列',f"待领取({len(pending)})=[{', '.join(pending) or '空'}]；执行中=[{', '.join(working) or '空'}]；等待沙箱=[{', '.join(waiting) or '空'}]；已完成={completed}/{len(ids)}")
            self.out.write('沙箱队列',f"待领取({len(queued)})=[{', '.join(queued) or '空'}]；执行中({len(executing)})=[{', '.join(executing) or '空'}]；额度=全局2/每Runner1/每用户1")

    async def follow(self,stopping):
        """接收停止事件，每250毫秒观测一次，仅变化时输出；结束时最后复核一次实际状态。"""
        while not stopping.is_set():
            await self.snapshot()
            try:await asyncio.wait_for(stopping.wait(),.25)
            except TimeoutError:pass
        await self.snapshot()


class Demo(Scaling):
    async def run_demo(self,count=10,timeout=600,delay=2):
        """接收对话数量/总时限/可观察等待秒数，经真实两worker和两Runner执行并核对CSV字节。"""
        self.timeline=Timeline(self,self.console)
        for index,ident in enumerate(self.runners,1):
            self.timeline.runners[ident[:12]]=f'Runner{index}';self.console.write('节点',f"Runner{index} 身份={ident[:12]}，每实例额度=1")
        users=[self.login('zhangsan'),self.login('lisi')]
        # 在worker启动前同时积累完整批次，首次队列快照可以明确看到10个待领取任务。
        for index in range(count):
            headers=users[index%2];conversation,ids=self.upload(headers,[(f'case-{index}.csv',f'marker,amount\ncase-{index},{index+1}\ncase-{index},{2*(index+1)}\n'.encode())])
            question=f'仅处理本条新上传的case-{index}.csv。调用run_sandbox_code执行Python：先time.sleep({delay})，按marker汇总amount，结果列名必须为marker和total，其中total使用数字类型。把结果转换为记录字典列表，调用emit_result生成结果CSV供下载，并回答总额。不要联网，不查询知识库或业务数据库。'
            response=self.api.post('/api/conversations/'+conversation+'/messages',headers=headers,json={'content':question,'attachment_ids':ids,'request_key':new_id()})
            self.assertEqual(response.status_code,202,response.text);ident=response.json()['run_id'];label=f'A{index+1:02d}'
            self.timeline.cases[ident]={'label':label,'headers':headers,'expected':3*(index+1)}
            self.console.write('用户提交',f"{label} run_id={ident}，用户={'zhangsan' if index%2==0 else 'lisi'}，文件=case-{index}.csv，预期总额={3*(index+1)}")
        stopping=asyncio.Event();observer=asyncio.create_task(self.timeline.follow(stopping));started=time.monotonic()
        try:
            worker_containers=[]
            for index in range(2):
                ident=await self.clone('worker',{'POSTGRES_DB':self.name,'WORKER_CONCURRENCY':'2'})
                worker_containers.append(ident)
            for index,ident in enumerate(worker_containers):
                await self.timeline.bind_worker(ident,f'Worker{index+1}')
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                if observer.done():observer.result()
                async with self.pool.acquire() as conn:
                    rows=await conn.fetch('SELECT run_id,status,state_json->\'error\' AS error FROM app_runtime.runs WHERE run_id=ANY($1::varchar[])',list(self.timeline.cases))
                bad=[row for row in rows if row['status'] in ('failed','interrupted','cancelled','waiting_confirmation')]
                self.assertFalse(bad,[(self.timeline.task(row['run_id']),row['status'],row['error']) for row in bad])
                if sum(row['status']=='completed' for row in rows)==count:break
                await asyncio.sleep(.25)
            else:self.fail('对话批次超时；已打印当前两个队列状态')
            stopping.set();await observer
            for ident,case in self.timeline.cases.items():
                artifacts=await self.admin.fetch("SELECT a.artifact_id FROM app_runtime.sandbox_artifacts a JOIN app_runtime.sandbox_results r USING(result_id) JOIN app_runtime.sandbox_jobs j USING(job_id) WHERE j.run_id=$1 AND a.filename LIKE '%.csv'",ident)
                self.assertTrue(artifacts,case['label']+'没有真实CSV产物');matched=False
                for artifact in artifacts:
                    response=self.api.get('/api/sandbox/artifacts/'+artifact['artifact_id'],headers=case['headers']);self.assertEqual(response.status_code,200)
                    values=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
                    self.evidence.append({'phase':'console-artifact','task':case['label'],'rows':values,'expected':case['expected']})
                    self.console.write('结果读取',f"{case['label']} CSV列={list(values[0]) if values else []}，行数={len(values)}，预期总额={case['expected']}")
                    try:matched=matched or abs(sum(float(row['total']) for row in values)-case['expected'])<1e-6 if values else matched
                    except (ValueError,TypeError,KeyError):pass
                self.assertTrue(matched,case['label']+'实际CSV数值不符合预期');self.console.write('结果核对',f"{case['label']} 已下载真实CSV，总额={case['expected']}，校验通过")
            summary={'dialogs':count,'completed':count,'worker_pending_peak':self.timeline.max_pending,'sandbox_queued_peak':self.timeline.max_queued,'sandbox_running_peak':self.timeline.max_running,'seconds':round(time.monotonic()-started,2)}
            self.evidence.append({'phase':'live-console','summary':summary});self.console.write('汇总',json.dumps(summary,ensure_ascii=False))
        finally:
            stopping.set()
            if not observer.done():observer.cancel()
            await asyncio.gather(observer,return_exceptions=True)


async def main(args,console):
    """接收已验证CLI参数和输出器，准备隔离环境、运行可见批次并始终清理本次临时资源。"""
    actual=Settings()
    if not actual.primary_api_key.get_secret_value() or not actual.review_api_key.get_secret_value():raise RuntimeError('该模式使用真实主/审核模型，请先在.env配置对应API Key')
    harness=Demo('run_demo');harness.console=console;prepared=False
    # Docker中的SIGTERM和Ctrl+C都先取消本次驱动，让finally清理测试资源。
    current=asyncio.current_task();loop=asyncio.get_running_loop();stop_requested=False
    def request_stop():
        """无参数仅取消主驱动一次；重复停止信号不能打断已经开始的资源清理。"""
        nonlocal stop_requested
        if not stop_requested:stop_requested=True;current.cancel()
    for signum in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(signum,request_stop)
    console.write('说明','真实模型模式：两worker×每实例2槽；两Runner×每实例1槽；忙时排队，使用独立测试库和虚构文件')
    console.write('说明','Worker提交到共享沙箱队列，不直接指定Runner；J编号表示一次沙箱任务，A编号表示一次对话')
    try:
        console.write('准备','复用M07C测试基础设施，创建独立PG及临时API/Runner容器')
        await harness.asyncSetUp();prepared=True
        await harness.run_demo(args.tasks,args.timeout,args.delay)
    finally:
        if prepared:
            console.write('清理','停止本次尚未结束的对话，然后清理测试副本、测试MinIO对象与独立PG')
            async def cleanup():
                """无参数关闭本批运行并复用隔离清理；独立协程受shield保护，终止信号不留下半清理环境。"""
                for ident,case in getattr(getattr(harness,'timeline',None),'cases',{}).items():
                    harness.api.post('/api/runs/'+ident+'/cancel',headers=case['headers'])
                await harness.asyncTearDown()
            cleanup_task=asyncio.create_task(cleanup());interrupted=False
            try:await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:interrupted=True;await cleanup_task
            console.write('清理','本次测试清理完成，正式服务与正式数据保留')
            if interrupted:raise asyncio.CancelledError()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='实时观测真实Worker/Runner/沙箱并发和两个队列')
    parser.add_argument('--tasks',type=int,default=10,choices=range(1,11))
    parser.add_argument('--timeout',type=int,default=600)
    parser.add_argument('--delay',type=int,default=2,choices=range(0,11))
    args=parser.parse_args()
    if not 30<=args.timeout<=1200:parser.error('timeout须在30–1200秒之间')
    logging.getLogger().setLevel(logging.WARNING);output=Console()
    try:asyncio.run(main(args,output))
    except (KeyboardInterrupt,asyncio.CancelledError):output.write('停止','用户中止了本次观测');raise SystemExit(130)
    except Exception as error:output.write('失败',str(error));raise SystemExit(1)
    finally:output.write('日志',str(output.path));output.close()
