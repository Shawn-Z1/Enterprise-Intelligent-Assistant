"""Agent专用Docker任务控制器；固定普通容器模板，每次执行创建并销毁独立实例。"""
import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import shutil
import tarfile
import time
import httpx
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import docker
from fastapi import FastAPI, Header, HTTPException, Request
from PIL import Image

# 控制接口属于可信基础设施；模型无法提供镜像、Docker参数、宿主目录或管理密钥。
ROOT=Path('/var/lib/sandbox')
INPUTS=Path('/var/lib/sandbox-inputs')
INPUT_VOLUME=os.environ.get('SANDBOX_INPUT_VOLUME','zhixing-sandbox-inputs')
IMAGE=os.environ.get('SANDBOX_TASK_IMAGE','zhixing-sandbox-guest:0.7.2')
HEADERS={'Authorization':'Bearer '+os.environ['SANDBOX_CONTROL_TOKEN']}
POLICY={'runtime':'docker','isolation':'ordinary-container','cpu':2,'memory_mib':1024,'pids':64,'seconds':60,'output_mib':256,'network':'none','uid':10001,'read_only':True}
client=docker.from_env()
active={}
# 本实例容量只是本地保护；总额度和排队以PG控制接口为准。
CAPACITY=int(os.environ.get('SANDBOX_RUNNER_CONCURRENCY','2'))
RUNNER_ID=os.environ.get('SANDBOX_RUNNER_ID','sandbox-runner-1')
API_URL=os.environ.get('SANDBOX_API_URL','http://api:8000/internal/sandbox')
if not 1<=CAPACITY<=16 or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',RUNNER_ID):raise RuntimeError('Runner名称或容量配置非法')
slots=asyncio.Semaphore(CAPACITY)


@asynccontextmanager
async def lifespan(app):
    """启动清理本项目标记的孤儿容器/输入，退出停止活动实例；不清理应用原件或产物。"""
    ROOT.mkdir(exist_ok=True);INPUTS.mkdir(exist_ok=True)
    for container in client.containers.list(all=True,filters={'label':['zhixing.sandbox=task','zhixing.runner='+RUNNER_ID]}):container.remove(force=True,v=True)
    (INPUTS/RUNNER_ID).mkdir(exist_ok=True)
    (ROOT/RUNNER_ID).mkdir(exist_ok=True)
    for folder in (INPUTS/RUNNER_ID).iterdir():
        if folder.is_dir() and not folder.is_symlink() and re.fullmatch(r'[A-Za-z0-9_-]{8,32}',folder.name):shutil.rmtree(folder)
    driver=asyncio.create_task(queue_driver())
    yield
    driver.cancel();await asyncio.gather(driver,return_exceptions=True)
    for entry in list(active.values()):
        if entry.get('instance'):
            try:entry['instance'].remove(force=True,v=True)
            except docker.errors.NotFound:pass


app=FastAPI(lifespan=lifespan)


def authorize(value):
    """接收内部控制Bearer令牌，恒时比较；控制密钥不进入任务容器。"""
    if not hmac.compare_digest(value or '',HEADERS['Authorization']):raise HTTPException(401,'控制鉴权失败')


@app.get('/health')
async def health(authorization: str=Header(default='')):
    """检查Docker及固定分析镜像是否存在，返回镜像内容ID/模板；缺失时拒绝开放执行工具。"""
    authorize(authorization)
    try:image=await asyncio.to_thread(client.images.get,IMAGE)
    except docker.errors.ImageNotFound:raise HTTPException(503,'分析镜像未安装')
    version=await asyncio.to_thread(client.version)
    info=await asyncio.to_thread(client.info)
    if info.get('OSType')!='linux' or not all(info.get(key) for key in ('MemoryLimit','PidsLimit','CpuCfsQuota')):
        raise HTTPException(503,'需要支持内存/进程/CPU限额的Linux Docker引擎，不降低执行限制')
    return {'ready':True,'policy':POLICY,'image_id':image.id,'active':len(active),'docker_version':version['Version'],'parser_version':'0.7.2','capacity':CAPACITY,'runner_id':RUNNER_ID,'formats':['txt','md','csv','xlsx','docx','pdf'],'outputs':['txt','md','csv','xlsx','docx','pdf','png']}


@app.delete('/jobs/{job_id}')
async def cancel(job_id: str,authorization: str=Header(default='')):
    """接收可信编号，停止对应活动实例；重启后也只查本项目标签，绝不执行或重跑代码。"""
    authorize(authorization)
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,32}',job_id):raise HTTPException(400,'编号非法')
    entry=active.get(job_id)
    if entry:
        entry['cancelled']=True
        if entry.get('instance'):
            try:await asyncio.to_thread(entry['instance'].kill)
            except docker.errors.APIError:pass
    else:
        for container in await asyncio.to_thread(client.containers.list,all=True,filters={'label':['zhixing.sandbox=task','zhixing.job='+job_id,'zhixing.runner='+RUNNER_ID]}):
            await asyncio.to_thread(container.remove,force=True,v=True)
    return {'stopped':True}


def create_instance(job_id,body):
    """从固定模板创建独立普通容器，只挂载该任务只读输入子目录，设置非root/断网/资源限制。"""
    folder=INPUTS/RUNNER_ID/job_id;folder.mkdir()
    files=[];total=0
    for index,item in enumerate(body.get('files',[])):
        raw=base64.b64decode(item['data'],validate=True);total+=len(raw)
        if len(raw)>20*1024*1024 or total>50*1024*1024 or hashlib.sha256(raw).hexdigest()!=item['sha256'] or item['format'] not in ('csv','xlsx','txt','md','docx','pdf'):raise ValueError('输入大小/格式/指纹不符')
        path=f'file_{index+1}.{item["format"]}';(folder/path).write_bytes(raw)
        files.append({k:item[k] for k in ('attachment_id','name','format')}|{'index':item.get('index',index+1),'path':path})
    (folder/'request.json').write_text(json.dumps({'files':files,**{k:body.get(k) for k in ('operation','language','code','chart','read')}},ensure_ascii=False))
    # 只映射Docker内部命名卷的本任务目录，不使用Windows或用户指定宿主路径。
    root=client.volumes.get(INPUT_VOLUME).attrs['Mountpoint']
    mount=docker.types.Mount('/input',root+'/'+RUNNER_ID+'/'+job_id,type='bind',read_only=True)
    image=client.images.get(IMAGE)
    return client.containers.create(image.id,name='zhixing-sandbox-'+job_id,user='10001:10001',network_mode='none',read_only=True,
        cap_drop=['ALL'],security_opt=['no-new-privileges:true'],mem_limit='1g',memswap_limit='1g',nano_cpus=2_000_000_000,pids_limit=64,
        ulimits=[docker.types.Ulimit(name='nofile',soft=128,hard=128),docker.types.Ulimit(name='fsize',soft=20*1024*1024,hard=20*1024*1024)],
        mounts=[mount],tmpfs={'/output':'rw,nosuid,nodev,noexec,size=256m,uid=10001,gid=10001,mode=0700','/tmp':'rw,nosuid,nodev,size=64m,mode=1777'},
        environment={'MPLCONFIGDIR':'/tmp/mpl','OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','HOME':'/tmp','TZ':'Asia/Shanghai'},
        labels={'zhixing.sandbox':'task','zhixing.job':job_id,'zhixing.runner':RUNNER_ID},log_config=docker.types.LogConfig(type='json-file',config={'max-size':'128k','max-file':'1'}))


def collect(container):
    """接收已完成实例，读取有限顶层产物并验证类型/链接/字节；收集器不执行文件内容。"""
    _,stream=container.exec_run(['tar','-C','/output','-cf','-','.'],stream=True,user='10001:10001')
    raw=bytearray();files={}
    for chunk in stream:
        raw.extend(chunk)
        if len(raw)>23*1024*1024:raise ValueError('产物合计超限')
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for entry in archive:
            if entry.isdir():continue
            name=entry.name.removeprefix('./')
            if not entry.isfile() or entry.size>20*1024*1024 or '/' in name or '\\' in name:raise ValueError('产物必须为限额顶层普通文件，不能为链接')
            if name in files:raise ValueError('重复产物名称')
            files[name]=archive.extractfile(entry).read()
    if 'receipt.json' not in files or len(files['receipt.json'])>2*1024*1024:raise ValueError('缺少有效回执')
    receipt=json.loads(files['receipt.json']);artifacts=[]
    if receipt.get('ok'):
        result=receipt['result']
        if len(result.get('rows',[]))>200 or len(result.get('files',[]))>100:raise ValueError('结构化结果超限')
        for name in result.get('artifacts',[]):
            kind=name.rsplit('.',1)[-1].lower()
            if kind not in ('txt','md','csv','xlsx','docx','pdf','png') or name not in files:raise ValueError('产物清单异常')
            data=files[name]
            if kind=='png':
                with Image.open(io.BytesIO(data)) as picture:
                    if picture.format!='PNG' or picture.width*picture.height>20_000_000:raise ValueError('PNG格式/尺寸非法')
                    picture.load();out=io.BytesIO();picture.save(out,format='PNG');data=out.getvalue()
            elif kind=='csv':
                out=io.StringIO();writer=csv.writer(out)
                for row in csv.reader(io.StringIO(data.decode('utf-8-sig'))):
                    writer.writerow(["'"+v if (v.lstrip().startswith(('=','+','-','@')) and not re.fullmatch(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?',v.strip())) or v.startswith(('\t','\r')) else v for v in row])
                data=out.getvalue().encode('utf-8-sig')
            elif kind in ('txt','md'):data.decode('utf-8-sig')
            elif kind=='pdf':
                if not data.startswith(b'%PDF-') or b'%%EOF' not in data[-1024:]:raise ValueError('PDF格式异常')
            else:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    entries=archive.infolist();required='word/document.xml' if kind=='docx' else 'xl/workbook.xml'
                    if required not in archive.namelist() or len(entries)>2000 or sum(x.file_size for x in entries)>100*1024*1024:raise ValueError('文档产物格式/解压规模异常')
                    if any('vba' in x.filename.lower() or x.flag_bits&1 for x in entries):raise ValueError('产物不能含宏或加密')
            artifacts.append({'name':name,'data':base64.b64encode(data).decode(),'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    if sum(x['size'] for x in artifacts)>20*1024*1024:raise ValueError('净化后的产物超过20MiB')
    receipt['artifacts']=artifacts;return receipt


async def execute_job(job_id, body, lease=None):
    """接收可信任务及可选续租函数，实际创建/执行/收集/销毁；重复控制请求复用回执不重跑。"""
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,32}',job_id):raise ValueError('编号非法')
    if body.get('operation') not in ('prepare','read','inspect','execute') or body.get('language','python') not in ('python','sql') or len(body.get('files',[]))>10 or len(body.get('code',''))>32000:raise ValueError('执行参数超限')
    digest=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest();saved=ROOT/RUNNER_ID/(job_id+'.json')
    if saved.exists():
        prior=json.loads(saved.read_text())
        if prior['digest']!=digest or prior['status']!='done':raise ValueError('执行结果未知，不能自动重跑')
        return prior['receipt']
    async with slots:
        active[job_id]={'user_id':body['user_id'],'instance':None,'cancelled':False}
        saved.write_text(json.dumps({'digest':digest,'status':'started'}));start=time.monotonic();container=None
        try:
            if lease and not await lease():raise ValueError('任务已取消或租约失效')
            container=await asyncio.to_thread(create_instance,job_id,body);active[job_id]['instance']=container
            await asyncio.to_thread(container.start);last_renew=time.monotonic()
            while True:
                if active[job_id]['cancelled']:raise ValueError('任务已取消')
                if time.monotonic()-start>POLICY['seconds']:raise ValueError('执行超过60秒限额')
                if lease and time.monotonic()-last_renew>=1:
                    if not await lease():raise ValueError('任务已取消或租约失效')
                    last_renew=time.monotonic()
                await asyncio.to_thread(container.reload)
                if len(await asyncio.to_thread(container.logs))>=65536:raise ValueError('stdout/stderr超过64KiB')
                if container.status=='exited':raise ValueError('进程异常退出或资源超限')
                status,_=await asyncio.to_thread(container.exec_run,['test','-f','/output/.ready'],user='10001:10001')
                if status==0:break
                await asyncio.sleep(.25)
            receipt=await asyncio.to_thread(collect,container)
            receipt['stdout']=(await asyncio.to_thread(container.logs)).decode('utf-8',errors='replace')[:65536]
            receipt['duration_ms']=round((time.monotonic()-start)*1000)
        except asyncio.CancelledError:raise
        except Exception as error:receipt={'ok':False,'error':{'code':'SANDBOX_FAILED','message':str(error)[:1500]},'artifacts':[]}
        finally:
            if container:
                try:await asyncio.to_thread(container.remove,force=True,v=True)
                except docker.errors.NotFound:pass
            await asyncio.to_thread(shutil.rmtree,INPUTS/RUNNER_ID/job_id,True)
            active.pop(job_id,None)
        # 先销毁并清输入，再持久回执/回复；模型不会持有一个可复用解释器。
        saved.write_text(json.dumps({'digest':digest,'status':'done','receipt':receipt}));return receipt


async def queue_driver():
    """无参数从可信API领取PG任务并有界并发；节点不存应用凭证，网络故障停止未知执行。"""
    tasks=set()
    async with httpx.AsyncClient(timeout=15,trust_env=False) as api:
        async def work(body):
            """接收已领取任务，续租、执行后提交真实回执；提交失败留存未知而不重跑代码。"""
            ident=body['job_id'];version=body['lease_version'];node={'runner_id':RUNNER_ID,'lease_version':version}
            async def lease():
                """无参数核验实际任务归属/取消并续租；控制接口不可用即停止当前客体。"""
                value=await api.post(API_URL+'/jobs/'+ident+'/renew',headers=HEADERS,json=node)
                value.raise_for_status();return value.json()['continue']
            try:
                receipt=await execute_job(ident,body,lease)
                for attempt in range(2):
                    try:
                        response=await api.post(API_URL+'/jobs/'+ident+'/complete',headers=HEADERS,json={**node,'receipt':receipt})
                        response.raise_for_status();break
                    except httpx.HTTPError:
                        if attempt:raise
                        await asyncio.sleep(.5)
            except asyncio.CancelledError:raise
            except Exception:
                # 领取/回执均由PG租约恢复标未知，不输出请求头或敏感输入。
                pass
        try:
            while True:
                tasks={task for task in tasks if not task.done()}
                if len(tasks)<CAPACITY:
                    try:
                        state=await health(HEADERS['Authorization'])
                        response=await api.post(API_URL+'/claim',headers=HEADERS,json={'runner_id':RUNNER_ID,'capacity':CAPACITY,'image_id':state['image_id']})
                        response.raise_for_status();body=response.json()['job']
                        if body:
                            tasks.add(asyncio.create_task(work(body)));continue
                    except Exception:pass
                await asyncio.sleep(.3)
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


@app.post('/jobs/{job_id}')
async def diagnostic_execute(job_id: str, request: Request, authorization: str=Header(default='')):
    """接收内部控制诊断请求，验证固定容器执行；普通Agent只走PG队列，不使用此入口。"""
    authorize(authorization)
    if int(request.headers.get('content-length','0'))>72*1024*1024:raise HTTPException(413,'请求超限')
    raw=await request.body()
    if len(raw)>72*1024*1024:raise HTTPException(413,'请求超限')
    body=json.loads(raw)
    if slots.locked():raise HTTPException(429,'内部诊断额度已满')
    return await execute_job(job_id,body)
