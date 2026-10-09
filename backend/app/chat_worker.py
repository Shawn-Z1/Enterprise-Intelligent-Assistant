import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from app.audit import AuditRecorder
from app.chat_service import ChatProcessor
from app.config import get_settings
from app.database import create_app_pool
from app.dependencies import check_dependencies
from app.events import InfrastructureEvents
from app.ids import new_id
from app.model_gateway import ModelGateway
from app.knowledge_storage import KnowledgeStorage
from app.knowledge_worker import KnowledgeProcessor
from app.timezones import beijing_now

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)


async def heartbeat(settings, pool, audit, stopping: asyncio.Event):
    """接收配置、连接池、审计和停止事件，持续保存北京时间心跳及真实依赖状态；停止后返回。"""
    events,trace_id = InfrastructureEvents(),new_id()
    path,previous = Path(settings.worker_state_path),None
    await audit.append(trace_id,'service.started',{'service':'worker','milestone':'M05'})
    while not stopping.is_set():
        results = await check_dependencies(settings)
        state = {'timestamp':beijing_now().isoformat(),'pid':os.getpid(),'ready':all(item.status=='ready' for item in results.values()),
                 'dependencies':{name:item.status for name,item in results.items()}}
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state),encoding='utf-8')
        temporary.replace(path)
        if state['dependencies']!=previous:
            await audit.append(trace_id,'worker.dependencies.changed',state['dependencies'])
            previous = state['dependencies']
        events.record(trace_id,'worker.heartbeat',state['dependencies'])
        try:
            await asyncio.wait_for(stopping.wait(),timeout=settings.worker_interval_seconds)
        except TimeoutError:
            pass


async def run():
    """无参数启动文本任务 worker 和依赖心跳；限制并发、扫描租约并响应容器停止，关闭时明确中断任务。"""
    settings = get_settings()
    pool = await create_app_pool(settings)
    secrets = tuple(value.get_secret_value() for value in (settings.postgres_password,settings.business_db_password,
        settings.app_minio_secret_key,settings.primary_api_key,settings.fallback_api_key))
    audit = AuditRecorder(pool,'worker',secrets)
    gateway = ModelGateway(settings)
    processor = ChatProcessor(pool,settings,gateway,audit)
    knowledge = KnowledgeProcessor(pool,settings,KnowledgeStorage(settings),audit)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM,signal.SIGINT):
        loop.add_signal_handler(signum,stopping.set)
    health_task = asyncio.create_task(heartbeat(settings,pool,audit,stopping))
    # 执行集合只负责本进程调度，真实任务和租约始终保存在 PG。
    tasks = set()
    # 两个队列交替优先，持续聊天不会饿死知识解析；共用既有并发上限。
    prefer_knowledge = True
    try:
        while not stopping.is_set():
            tasks = {task for task in tasks if not task.done()}
            if health_task.done():
                health_task.result()
                raise RuntimeError('后台心跳已意外结束')
            await processor.recover()
            await knowledge.recover()
            if len(tasks)<settings.worker_concurrency:
                queues = [knowledge,processor] if prefer_knowledge else [processor,knowledge]
                prefer_knowledge = not prefer_knowledge
                claimed = False
                for queue in queues:
                    task_run = await queue.claim()
                    if task_run:
                        tasks.add(asyncio.create_task(queue.process(task_run)))
                        claimed = True
                        break
                if claimed:
                    continue
            try:
                await asyncio.wait_for(stopping.wait(),timeout=0.3)
            except TimeoutError:
                pass
    finally:
        stopping.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        health_task.cancel()
        await asyncio.gather(health_task,return_exceptions=True)
        await gateway.close()
        await pool.close()
