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
from app.rag_index import RagIndex
from app.memory_index import MemoryIndex
from app.long_term_memory import LongTermMemory
from app.memory_worker import MemoryProcessor
from app.rag_service import RagService
from app.web_search import WebSearchService
from app.knowledge_indexer import KnowledgeIndexer
from app.rag_contract import INDEX_VERSION
from app.timezones import beijing_now

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)


async def heartbeat(settings, pool, audit, stopping: asyncio.Event, worker_id: str):
    """接收settings/pool/audit、停止事件及本副本worker_id，保存时间/容量/依赖心跳；停止后返回无任务结果。"""
    events,trace_id = InfrastructureEvents(),new_id()
    path,previous = Path(settings.worker_state_path),None
    await audit.append(trace_id,'service.started',{'service':'worker','milestone':'M11','worker_id':worker_id,'capacity':settings.worker_concurrency})
    while not stopping.is_set():
        results = await check_dependencies(settings)
        state = {'timestamp':beijing_now().isoformat(),'pid':os.getpid(),'worker_id':worker_id,'capacity':settings.worker_concurrency,'ready':all(item.status=='ready' for item in results.values()),
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
        settings.app_minio_secret_key,settings.primary_api_key,settings.fallback_api_key,settings.review_api_key,settings.web_search_api_key,settings.sandbox_control_token))
    audit = AuditRecorder(pool,'worker',secrets)
    gateway = ModelGateway(settings)
    index = RagIndex(settings)
    private_index=MemoryIndex(settings)
    long_memory=LongTermMemory(pool,settings,private_index,audit)
    storage = KnowledgeStorage(settings)
    rag = RagService(pool,settings,index,storage,audit)
    web = WebSearchService(pool,settings,audit)
    # 同一副本的两类队列使用同一随机身份，重启生成新身份；PG租约阻止旧副本写回。
    worker_id=new_id()
    processor = ChatProcessor(pool,settings,gateway,audit,worker_id=worker_id,rag=rag,web=web,long_memory=long_memory)
    personal=MemoryProcessor(pool,settings,gateway,audit,long_memory,worker_id)
    knowledge = KnowledgeProcessor(pool,settings,storage,audit,KnowledgeIndexer(pool,settings,index,audit),worker_id=worker_id)
    await queue_existing_approved(pool,knowledge)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM,signal.SIGINT):
        loop.add_signal_handler(signum,stopping.set)
    health_task = asyncio.create_task(heartbeat(settings,pool,audit,stopping,worker_id))
    # 执行集合只负责本进程调度，真实任务和租约始终保存在 PG。
    tasks = set()
    # 三类任务轮换优先并共用既有槽位；后台记忆不会额外创建不受控并发。
    queues=[processor,knowledge,personal]
    queue_cursor=0
    try:
        while not stopping.is_set():
            tasks = {task for task in tasks if not task.done()}
            if health_task.done():
                health_task.result()
                raise RuntimeError('后台心跳已意外结束')
            await processor.recover()
            await knowledge.recover()
            await personal.recover()
            if len(tasks)<settings.worker_concurrency:
                claimed = False
                for offset in range(len(queues)):
                    queue=queues[(queue_cursor+offset)%len(queues)]
                    task_run = await queue.claim()
                    if task_run:
                        queue_cursor=(queue_cursor+offset+1)%len(queues)
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
        # SIGTERM后停止领取，保持续租并有限等待活动任务；超时走现有中断守卫，不能自动重发SQL。
        pending=set()
        if tasks:_,pending=await asyncio.wait(tasks,timeout=settings.worker_shutdown_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        health_task.cancel()
        await asyncio.gather(health_task,return_exceptions=True)
        await web.close()
        await gateway.close()
        await index.close()
        await private_index.close()
        await long_memory.close()
        await pool.close()


async def queue_existing_approved(pool,knowledge):
    """接收应用池和知识处理器，给第五阶段已批准待索引文档补排真实索引任务，不改变原件或旧审核。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch("""SELECT a.*,d.document_id,d.index_version FROM app_knowledge.applications a JOIN app_knowledge.documents d USING(application_id)
                WHERE (a.status='pending_index' AND d.status='pending_index') OR (a.status='retrievable' AND d.status='retrievable' AND d.index_version<$1)
                FOR UPDATE OF a""",INDEX_VERSION)
            for row in rows:
                kind='reindex' if row['index_version'] is not None and row['index_version']<INDEX_VERSION else 'index'
                if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM app_knowledge.jobs WHERE application_id=$1 AND job_type=$2 AND status IN ('pending','running'))",row['application_id'],kind):
                    trace_id = new_id()
                    await knowledge.service.add_job(conn,dict(row),kind,trace_id,row['document_id'])
                    await conn.execute("UPDATE app_knowledge.applications SET status='pending_index',updated_at=now() WHERE application_id=$1",row['application_id'])
                    await conn.execute("UPDATE app_knowledge.documents SET status='pending_index' WHERE document_id=$1",row['document_id'])
                    await knowledge.service.event(conn,trace_id,'knowledge.index_queued',dict(row),{'origin':'chunk_policy_upgrade' if kind=='reindex' else 'pending_index','target_index_version':INDEX_VERSION})
