import asyncio

from starlette.exceptions import HTTPException

from app.auth import auth_error
from app.ids import new_id
from app.knowledge_service import KnowledgeService, INACTIVE, fingerprint


class KnowledgeProcessor:
    """后台只解析预览和已批准原件；M05 永不插入正式切片、向量或可检索状态。"""

    def __init__(self,pool,settings,storage,audit):
        """接收受限数据库、限制配置、原件存储和审计，初始化 worker 标识与公共知识服务。"""
        self.pool,self.settings,self.storage,self.audit = pool,settings,storage,audit
        self.worker_id = new_id()
        self.service = KnowledgeService(pool,storage,audit)

    async def valid(self,conn,row,job) -> bool:
        """接收事务、当前材料和任务，复检状态、指纹、正式文档及原批准；返回任务是否仍可继续。"""
        if job['job_type']=='delete':
            return row['status']=='deleting'
        if row['status'] in INACTIVE or fingerprint(row)!=job['expected_fingerprint'] or row['fingerprint']!=job['expected_fingerprint']:
            return False
        if job['job_type']=='preview':
            return True
        creator = await conn.fetchrow('SELECT role,status,knowledge_enabled,department_id,grade FROM app_system.users WHERE user_id=$1',row['creator_user_id'])
        if creator is None or creator['status']!='active' or not (creator['role']=='admin' or creator['knowledge_enabled'] and creator['department_id'] and creator['grade']):
            return False
        document = await conn.fetchrow('SELECT * FROM app_knowledge.documents WHERE document_id=$1',job['document_id'])
        if document is None or document['status']=='invalidated' or document['approved_fingerprint']!=job['expected_fingerprint'] or document['acl_snapshot']!=row['acl_snapshot']:
            return False
        if row['approval_origin']=='admin_direct':
            return creator['role']=='admin' and document['review_id'] is None
        review = await conn.fetchrow('SELECT * FROM app_knowledge.reviews WHERE review_id=$1',document['review_id'])
        return bool(review and review['application_id']==row['application_id'] and review['decision']=='approved' and
                    review['application_fingerprint']==job['expected_fingerprint'] and review['acl_snapshot']==row['acl_snapshot'])

    async def invalidate(self,conn,row,job,error):
        """接收锁定材料、失效任务和安全错误，原子标失效并取消晚到任务，保留原件、解析和审核历史。"""
        await conn.execute("UPDATE app_knowledge.applications SET status='invalidated',error_json=$1,updated_at=now() WHERE application_id=$2",error,row['application_id'])
        await conn.execute("UPDATE app_knowledge.documents SET status='invalidated' WHERE application_id=$1",row['application_id'])
        await conn.execute("UPDATE app_knowledge.jobs SET status='cancelled',lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE application_id=$1 AND status IN ('pending','running')",row['application_id'])
        await self.service.event(conn,job['trace_id'],'knowledge.approval_invalidated',row,{'job_id':job['job_id'],'expected_fingerprint':job['expected_fingerprint'],'error':error})

    async def claim(self) -> dict | None:
        """无参数，以材料优先的锁顺序领取一条持久任务；返回任务，失效任务不会读取文件或解析。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                application = await conn.fetchrow("SELECT * FROM app_knowledge.applications a WHERE EXISTS(SELECT 1 FROM app_knowledge.jobs j WHERE j.application_id=a.application_id AND j.status='pending') ORDER BY a.created_at,a.application_id FOR UPDATE OF a SKIP LOCKED LIMIT 1")
                if application is None:
                    return None
                row = dict(application)
                found = await conn.fetchrow("SELECT * FROM app_knowledge.jobs WHERE application_id=$1 AND status='pending' ORDER BY created_at,job_id FOR UPDATE SKIP LOCKED LIMIT 1",row['application_id'])
                if found is None:
                    return None
                job = dict(found)
                if not await self.valid(conn,row,job):
                    if row['status'] in INACTIVE:
                        await conn.execute("UPDATE app_knowledge.jobs SET status='cancelled',updated_at=now() WHERE job_id=$1",job['job_id'])
                    else:
                        await self.invalidate(conn,row,job,{'code':'APPROVAL_CHANGED','message':'材料、权限或批准快照已变化，请重新提交'})
                    return None
                await conn.execute("UPDATE app_knowledge.jobs SET status='running',attempts=attempts+1,lease_owner=$1,lease_until=now()+$2*interval '1 second',updated_at=now() WHERE job_id=$3",self.worker_id,self.settings.run_lease_seconds,job['job_id'])
                if job['job_type']=='publish':
                    await conn.execute("UPDATE app_knowledge.applications SET status='parsing',parse_status='processing',updated_at=now() WHERE application_id=$1",row['application_id'])
                    await conn.execute("UPDATE app_knowledge.documents SET status='parsing' WHERE document_id=$1",job['document_id'])
                elif job['job_type']=='preview':
                    await conn.execute("UPDATE app_knowledge.applications SET parse_status='processing',updated_at=now() WHERE application_id=$1",row['application_id'])
                await self.service.event(conn,job['trace_id'],'knowledge.job_started',row,{'job_id':job['job_id'],'job_type':job['job_type']})
                return job

    async def renew(self,job,task):
        """接收已领取任务和处理协程，续租并检查撤回/删除；取消时终止解析子进程，不重新发布。"""
        while True:
            await asyncio.sleep(2)
            async with self.pool.acquire() as conn:
                owner = await conn.fetchrow('SELECT status,lease_owner FROM app_knowledge.jobs WHERE job_id=$1',job['job_id'])
                if owner['status']!='running' or owner['lease_owner']!=self.worker_id:
                    task.cancel()
                    return
                await conn.execute("UPDATE app_knowledge.jobs SET lease_until=now()+$1*interval '1 second' WHERE job_id=$2 AND lease_owner=$3 AND status='running'",self.settings.run_lease_seconds,job['job_id'],self.worker_id)

    async def finish(self,job,result):
        """接收任务与解析内容，再次锁定和校验批准，保存不可变解析并标待索引；删除任务只完成失效状态。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = dict(await conn.fetchrow('SELECT * FROM app_knowledge.applications WHERE application_id=$1 FOR UPDATE',job['application_id']))
                current = await conn.fetchrow('SELECT * FROM app_knowledge.jobs WHERE job_id=$1 FOR UPDATE',job['job_id'])
                if current['status']!='running' or current['lease_owner']!=self.worker_id:
                    return
                if not await self.valid(conn,row,job):
                    await self.invalidate(conn,row,job,{'code':'APPROVAL_CHANGED','message':'材料、权限或批准已变化，晚到任务不能发布'})
                    return
                if job['job_type']=='delete':
                    await conn.execute("UPDATE app_knowledge.applications SET status='invalidated',updated_at=now() WHERE application_id=$1",row['application_id'])
                    kind = 'knowledge.deleted'
                else:
                    parse_id = new_id()
                    await conn.execute('''INSERT INTO app_knowledge.parses(parse_id,application_id,fingerprint,source_sha256,content_json)
                        VALUES($1,$2,$3,$4,$5) ON CONFLICT(application_id,fingerprint) DO NOTHING''',parse_id,row['application_id'],job['expected_fingerprint'],row['original_sha256'],result)
                    await conn.execute("UPDATE app_knowledge.applications SET parse_status='completed',error_json=NULL,updated_at=now() WHERE application_id=$1",row['application_id'])
                    if job['job_type']=='publish':
                        await conn.execute("UPDATE app_knowledge.applications SET status='pending_index' WHERE application_id=$1",row['application_id'])
                        await conn.execute("UPDATE app_knowledge.documents SET status='pending_index' WHERE document_id=$1",job['document_id'])
                    kind = 'knowledge.parsed_pending_index' if job['job_type']=='publish' else 'knowledge.preview_parsed'
                await conn.execute("UPDATE app_knowledge.jobs SET status='completed',lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE job_id=$1",job['job_id'])
                await self.service.event(conn,job['trace_id'],kind,row,{'job_id':job['job_id'],'document_id':job['document_id'],
                    'page_count':result.get('page_count') if result else None,'characters':len(result.get('text','')) if result else None,'indexing_performed':False})

    async def failed(self,job,error):
        """接收失败任务和安全错误，保存明确失败；原件变化使批准失效，已撤回或已删除的任务不覆盖状态。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = dict(await conn.fetchrow('SELECT * FROM app_knowledge.applications WHERE application_id=$1 FOR UPDATE',job['application_id']))
                state = await conn.fetchval('SELECT status FROM app_knowledge.jobs WHERE job_id=$1',job['job_id'])
                if state!='running' or row['status'] in INACTIVE:
                    return
                if error['code']=='SOURCE_CHANGED':
                    await self.invalidate(conn,row,job,error)
                    return
                await conn.execute("UPDATE app_knowledge.jobs SET status='failed',error_json=$1,lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE job_id=$2",error,job['job_id'])
                await conn.execute("UPDATE app_knowledge.applications SET parse_status='failed',error_json=$1,updated_at=now() WHERE application_id=$2",error,row['application_id'])
                if job['job_type']=='publish':
                    await conn.execute("UPDATE app_knowledge.applications SET status='failed' WHERE application_id=$1",row['application_id'])
                    await conn.execute("UPDATE app_knowledge.documents SET status='failed' WHERE document_id=$1",job['document_id'])
                await self.service.event(conn,job['trace_id'],'knowledge.processing_failed',row,{'job_id':job['job_id'],'error':error})

    async def recover(self):
        """无参数结束租约已失效的任务，保留失败证据等待显式重试，不把批准伪装为索引完成。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM app_knowledge.jobs WHERE status='running' AND lease_until<now()")
        for row in rows:
            await self.failed(dict(row),{'code':'JOB_INTERRUPTED','message':'后台进程中断，可检查原件后重试'})

    async def process(self,job):
        """接收已领取任务，核验原件后解析并再次检查批准；任何失败都有明确状态，不执行嵌入或索引。"""
        renewal = asyncio.create_task(self.renew(job,asyncio.current_task()))
        try:
            if job['job_type']=='delete':
                await self.finish(job,None)
                return
            async with self.pool.acquire() as conn:
                row = dict(await conn.fetchrow('SELECT * FROM app_knowledge.applications WHERE application_id=$1',job['application_id']))
                cached = await conn.fetchval('SELECT content_json FROM app_knowledge.parses WHERE application_id=$1 AND fingerprint=$2',row['application_id'],job['expected_fingerprint'])
            data = await self.storage.read_verified(row)
            parsed = cached or await self.storage.parse(data,row['file_format'])
            await self.storage.read_verified(row)
            await self.finish(job,parsed)
        except asyncio.CancelledError:
            await self.failed(job,{'code':'JOB_INTERRUPTED','message':'任务已停止或后台中断'})
        except Exception as error:
            detail = error.detail if isinstance(error,HTTPException) and isinstance(error.detail,dict) else {'code':'KNOWLEDGE_PROCESSING_ERROR','message':'文件处理或存储失败，请检查原件后重试'}
            await self.failed(job,detail)
        finally:
            renewal.cancel()
            await asyncio.gather(renewal,return_exceptions=True)
