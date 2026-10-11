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
from contextlib import asynccontextmanager
from pathlib import Path

import docker
from fastapi import FastAPI, Header, HTTPException, Request
from PIL import Image

# 控制接口属于可信基础设施；模型无法提供镜像、Docker参数、宿主目录或管理密钥。
ROOT=Path('/var/lib/sandbox')
INPUTS=Path('/var/lib/sandbox-inputs')
INPUT_VOLUME=os.environ.get('SANDBOX_INPUT_VOLUME','zhixing-sandbox-inputs')
IMAGE=os.environ.get('SANDBOX_TASK_IMAGE','zhixing-sandbox-guest:0.7.1')
HEADERS={'Authorization':'Bearer '+os.environ['SANDBOX_CONTROL_TOKEN']}
POLICY={'runtime':'docker','isolation':'ordinary-container','cpu':2,'memory_mib':1024,'pids':64,'seconds':60,'output_mib':256,'network':'none','uid':10001,'read_only':True}
client=docker.from_env()
active={}
slots=asyncio.Semaphore(2)


@asynccontextmanager
async def lifespan(app):
    """启动清理本项目标记的孤儿容器/输入，退出停止活动实例；不清理应用原件或产物。"""
    ROOT.mkdir(exist_ok=True);INPUTS.mkdir(exist_ok=True)
    for container in client.containers.list(all=True,filters={'label':'zhixing.sandbox=task'}):container.remove(force=True,v=True)
    for folder in INPUTS.iterdir():
        if folder.is_dir() and not folder.is_symlink() and re.fullmatch(r'[A-Za-z0-9_-]{8,32}',folder.name):shutil.rmtree(folder)
    yield
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
    return {'ready':True,'policy':POLICY,'image_id':image.id,'active':len(active),'docker_version':version['Version']}


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
        for container in await asyncio.to_thread(client.containers.list,all=True,filters={'label':['zhixing.sandbox=task','zhixing.job='+job_id]}):
            await asyncio.to_thread(container.remove,force=True,v=True)
    return {'stopped':True}


def create_instance(job_id,body):
    """从固定模板创建独立普通容器，只挂载该任务只读输入子目录，设置非root/断网/资源限制。"""
    folder=INPUTS/job_id;folder.mkdir()
    files=[];total=0
    for index,item in enumerate(body.get('files',[])):
        raw=base64.b64decode(item['data'],validate=True);total+=len(raw)
        if len(raw)>20*1024*1024 or total>50*1024*1024 or hashlib.sha256(raw).hexdigest()!=item['sha256'] or item['format'] not in ('csv','xlsx'):raise ValueError('输入大小/格式/指纹不符')
        path=f'file_{index+1}.{item["format"]}';(folder/path).write_bytes(raw)
        files.append({k:item[k] for k in ('attachment_id','name','format')}|{'path':path})
    (folder/'request.json').write_text(json.dumps({'files':files,**{k:body.get(k) for k in ('operation','language','code','chart')}},ensure_ascii=False))
    # 只映射Docker内部命名卷的本任务目录，不使用Windows或用户指定宿主路径。
    root=client.volumes.get(INPUT_VOLUME).attrs['Mountpoint']
    mount=docker.types.Mount('/input',root+'/'+job_id,type='bind',read_only=True)
    image=client.images.get(IMAGE)
    return client.containers.create(image.id,name='zhixing-sandbox-'+job_id,user='10001:10001',network_mode='none',read_only=True,
        cap_drop=['ALL'],security_opt=['no-new-privileges:true'],mem_limit='1g',memswap_limit='1g',nano_cpus=2_000_000_000,pids_limit=64,
        ulimits=[docker.types.Ulimit(name='nofile',soft=128,hard=128),docker.types.Ulimit(name='fsize',soft=20*1024*1024,hard=20*1024*1024)],
        mounts=[mount],tmpfs={'/output':'rw,nosuid,nodev,noexec,size=256m,uid=10001,gid=10001,mode=0700','/tmp':'rw,nosuid,nodev,size=64m,mode=1777'},
        environment={'MPLCONFIGDIR':'/tmp/mpl','OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','HOME':'/tmp','TZ':'Asia/Shanghai'},
        labels={'zhixing.sandbox':'task','zhixing.job':job_id},log_config=docker.types.LogConfig(type='json-file',config={'max-size':'128k','max-file':'1'}))


def collect(container):
    """从已完成任务容器读取输出，拒绝链接/大文件，复核有限结果、重编码PNG并净化CSV公式。"""
    # Docker归档接口不读取tmpfs挂载，因此使用固定只读tar命令收集；该命令不是模型代码。
    _,stream=container.exec_run(['tar','-C','/output','-cf','-','.'],stream=True,user='10001:10001');raw=bytearray();files={}
    for chunk in stream:
        raw.extend(chunk)
        if len(raw)>23*1024*1024:raise ValueError('产物合计超限')
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for entry in archive:
            if entry.isdir():continue
            if not entry.isfile() or entry.size>20*1024*1024:raise ValueError('产物必须为普通限额文件，不能为链接')
            if entry.name in ('./receipt.json','./result.csv','./chart.png'):files[entry.name.split('/')[-1]]=archive.extractfile(entry).read()
    if 'receipt.json' not in files or len(files['receipt.json'])>2*1024*1024:raise ValueError('缺少有效回执')
    receipt=json.loads(files['receipt.json']);artifacts=[]
    if receipt.get('ok'):
        result=receipt['result']
        if len(result.get('rows',[]))>200 or len(result.get('files',[]))>100:raise ValueError('结构化结果超限')
        for name in result.get('artifacts',[]):
            if name not in ('result.csv','chart.png') or name not in files:raise ValueError('产物清单异常')
            data=files[name]
            if name.endswith('.png'):
                with Image.open(io.BytesIO(data)) as image:
                    if image.format!='PNG' or image.width*image.height>20_000_000:raise ValueError('PNG格式/尺寸非法')
                    image.load();out=io.BytesIO();image.save(out,format='PNG');data=out.getvalue()
            else:
                out=io.StringIO();writer=csv.writer(out)
                for row in csv.reader(io.StringIO(data.decode('utf-8-sig'))):writer.writerow(["'"+v if (v.lstrip().startswith(('=','+','-','@')) and not re.fullmatch(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?',v.strip())) or v.startswith(('\t','\r')) else v for v in row])
                data=out.getvalue().encode('utf-8-sig')
            artifacts.append({'name':name,'data':base64.b64encode(data).decode(),'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    if sum(item['size'] for item in artifacts)>20*1024*1024:raise ValueError('净化后的产物合计超过20MiB')
    receipt['artifacts']=artifacts;return receipt


@app.post('/jobs/{job_id}')
async def execute(job_id: str,request: Request,authorization: str=Header(default='')):
    """接收已授权任务，按全局2/每用户1创建独立容器，限额收集后销毁；重复回执不重跑。"""
    authorize(authorization)
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,32}',job_id):raise HTTPException(400,'编号非法')
    if int(request.headers.get('content-length','0'))>72*1024*1024:raise HTTPException(413,'请求超限')
    raw=await request.body()
    if len(raw)>72*1024*1024:raise HTTPException(413,'请求超限')
    body=json.loads(raw);digest=hashlib.sha256(raw).hexdigest();saved=ROOT/f'{job_id}.json'
    if saved.exists():
        prior=json.loads(saved.read_text())
        if prior['digest']!=digest or prior['status']!='done':raise HTTPException(409,'任务正在执行或结果未知，不能自动重跑')
        return prior['receipt']
    if slots.locked() or any(v['user_id']==body['user_id'] for v in active.values()):raise HTTPException(429,'沙箱忙碌，请稍后新发起任务')
    if body.get('operation') not in ('inspect','execute') or body.get('language','python') not in ('python','sql') or len(body.get('files',[]))>10 or len(body.get('code',''))>32000:raise HTTPException(400,'任务参数超限')
    async with slots:
        active[job_id]={'user_id':body['user_id'],'instance':None,'cancelled':False}
        saved.write_text(json.dumps({'digest':digest,'status':'started'}));start=time.monotonic();container=None
        try:
            container=await asyncio.to_thread(create_instance,job_id,body);active[job_id]['instance']=container
            if active[job_id]['cancelled']:raise ValueError('任务已取消')
            await asyncio.to_thread(container.start)
            async def wait():
                """轮询客体完成标记和输出限额；固定入口保留实例直至可信收集，避免tmpfs提前消失。"""
                while True:
                    if active[job_id]['cancelled']:raise ValueError('任务已取消')
                    await asyncio.to_thread(container.reload)
                    if len(await asyncio.to_thread(container.logs))>=65536:raise ValueError('stdout/stderr达到64KiB上限')
                    if container.status=='exited':raise ValueError('进程异常退出或资源超限')
                    try:
                        status,_=await asyncio.to_thread(container.exec_run,['test','-f','/output/.ready'],user='10001:10001')
                        if status==0:return
                    except docker.errors.NotFound:pass
                    await asyncio.sleep(.25)
            await asyncio.wait_for(wait(),timeout=60)
            receipt=await asyncio.wait_for(asyncio.to_thread(collect,container),timeout=15)
            receipt.update(exit_code=0,stdout=(await asyncio.to_thread(container.logs,stdout=True,stderr=False)).decode(errors='replace')[:65536],
                stderr=(await asyncio.to_thread(container.logs,stdout=False,stderr=True)).decode(errors='replace')[:65536],seconds=round(time.monotonic()-start,3),runtime=POLICY,image_id=container.image.id)
        except (Exception,asyncio.CancelledError) as error:
            receipt={'ok':False,'error':{'code':'SANDBOX_LIMIT','message':str(error)[:1000] or '超时/取消'},'artifacts':[],'seconds':round(time.monotonic()-start,3)}
        finally:
            if container:
                try:await asyncio.to_thread(container.remove,force=True,v=True)
                except docker.errors.NotFound:pass
            active.pop(job_id,None)
            folder=INPUTS/job_id
            if folder.exists():await asyncio.to_thread(shutil.rmtree,folder)
        saved.write_text(json.dumps({'digest':digest,'status':'done','receipt':receipt}));return receipt
