import hashlib
import json
from pathlib import PurePath

from app.auth import auth_error
from app.ids import new_id

INACTIVE = {'withdrawn','deleting','invalidated'}
FORMATS = {'.pdf':'pdf','.docx':'docx','.txt':'txt','.md':'md','.markdown':'md'}


def fingerprint(row: dict) -> str:
    """接收申请完整字段，以原件、位置、ACL 和创建者构造稳定指纹，返回审批绑定值，排除可变处理状态。"""
    fields = ('application_id','creator_user_id','title','filename','file_format','byte_size','original_sha256','object_key',
              'allowed_department_ids','minimum_grade','acl_snapshot','source_ids')
    value = {key:row.get(key) for key in fields}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def file_kind(filename: str,data: bytes) -> str:
    """接收不可信文件名及原件字节，校验名称、扩展名和基本文件签名，返回支持格式或明确拒绝。"""
    if not filename or len(filename)>200 or any(char in filename for char in ('/','\\','\0','\r','\n')):
        raise auth_error(422,'INVALID_FILENAME','文件名不正确或超过 200 字符')
    kind = FORMATS.get(PurePath(filename).suffix.lower())
    if kind is None:
        raise auth_error(415,'UNSUPPORTED_FORMAT','当前仅支持 PDF、DOCX、TXT 和 Markdown，不支持此文件格式')
    if not data:
        raise auth_error(422,'EMPTY_FILE','文件内容为空')
    if kind=='pdf' and not data.startswith(b'%PDF-'):
        raise auth_error(422,'INVALID_PDF','文件内容不是有效的 PDF')
    if kind=='docx' and not data.startswith(b'PK'):
        raise auth_error(422,'INVALID_DOCX','文件内容不是有效的 DOCX')
    return kind


def acl_allows(context,row: dict) -> bool:
    """接收每次从 PG 读取的身份和申请 ACL，返回正文资格；创建者身份不提供部门/职级豁免。"""
    return context.is_admin or (context.can_use_knowledge and
        (not row['allowed_department_ids'] or context.department_id in row['allowed_department_ids']) and
        (row['minimum_grade'] is None or (context.grade or 0)>=row['minimum_grade']))


def metadata(row: dict,context) -> dict:
    """接收申请内部行和当前用户，返回管理资料及动作资格，排除对象路径、正文和存储凭证。"""
    fields = ('application_id','creator_user_id','title','filename','file_format','byte_size','status','parse_status',
              'allowed_department_ids','minimum_grade','fingerprint','approval_origin','error_json','created_at','updated_at','document_id','document_status','creator_name')
    value = {key:row.get(key) for key in fields}
    active = row['status'] not in INACTIVE and fingerprint(row)==row['fingerprint']
    value.update(can_preview=bool(active and acl_allows(context,row)),
                 can_delete=bool(active and context.can_use_knowledge),
                 can_withdraw=bool(row['status']=='pending_review' and context.can_use_knowledge),
                 can_review=bool(active and context.is_admin and row['status']=='pending_review'),
                 can_retry=bool(context.can_use_knowledge and (row['parse_status']=='failed' and row['status']=='pending_review' or context.is_admin and row['status']=='failed' or row['status']=='deleting' and row.get('error_json'))),
                 retrievable=row.get('document_status')=='retrievable')
    return value


class KnowledgeService:
    """知识申请、审核和维护公共服务，文件上传与后续对话转存共用同一审批/任务边界。"""

    def __init__(self,pool,storage,audit):
        """接收受限连接池、应用原件存储和统一审计，保存依赖，不取得数据库管理员凭证。"""
        self.pool,self.storage,self.audit = pool,storage,audit

    async def event(self,conn,trace_id,kind,row,data=None):
        """接收事务、追踪、事件和申请，永久追加身份/材料快照及可选数据；失败阻止同事务业务变更。"""
        await self.audit.append(trace_id,kind,{'application_id':row['application_id'],'fingerprint':fingerprint(row),
            'original_sha256':row['original_sha256'],'acl_snapshot':row['acl_snapshot'],**(data or {})},
            user_id=row['creator_user_id'],span_name='knowledge.lifecycle',connection=conn)

    async def actor(self,conn,context,admin=False):
        """接收事务和可信身份，修改前锁定并复查当前资格；审核/直接发布另需当前管理员角色。"""
        row = await conn.fetchrow('SELECT role,status,department_id,grade,knowledge_enabled FROM app_system.users WHERE user_id=$1 FOR UPDATE',context.user_id)
        allowed = row and row['status']=='active' and (row['role']=='admin' or row['knowledge_enabled'] and row['department_id'] and row['grade'])
        if not allowed or admin and row['role']!='admin':
            raise auth_error(403,'KNOWLEDGE_NOT_AUTHORIZED','当前身份没有执行此知识操作的资格')
        return row

    async def application(self,application_id: str,context,body=False) -> dict:
        """接收申请编号和当前身份，读取本人或管理员管理资料；正文另校验 ACL、状态和材料指纹。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('''SELECT a.*,d.document_id,d.status AS document_status FROM app_knowledge.applications a
                LEFT JOIN app_knowledge.documents d USING(application_id) WHERE a.application_id=$1''',application_id)
        if row is None or not context.is_admin and row['creator_user_id']!=context.user_id:
            raise auth_error(404,'NOT_FOUND','知识申请不存在或不可管理')
        result = dict(row)
        if body:
            if row['status'] in INACTIVE:
                raise auth_error(404,'NOT_FOUND','材料已撤回或失效')
            if fingerprint(result)!=row['fingerprint']:
                raise auth_error(409,'APPLICATION_CHANGED','材料或访问范围已变化，请重新提交')
            if not acl_allows(context,result):
                raise auth_error(403,'KNOWLEDGE_ACCESS_DENIED','当前部门或职级不符合此材料的访问范围')
        return result

    async def document(self,document_id: str,context) -> dict:
        """接收正式文档编号和当前身份，检查批准状态与 ACL；允许合法来源预览，不赋予他人管理权限。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('''SELECT a.*,d.document_id,d.status AS document_status,d.acl_snapshot AS document_acl,d.embedding_space,d.index_version,
                d.approved_fingerprint FROM app_knowledge.documents d JOIN app_knowledge.applications a USING(application_id)
                WHERE d.document_id=$1''',document_id)
        if row is None or row['document_status'] not in ('pending_index','retrievable') or row['status'] in INACTIVE:
            raise auth_error(404,'NOT_FOUND','正式文档尚未完成处理或已失效')
        value = dict(row)
        if fingerprint(value)!=row['approved_fingerprint'] or row['document_acl']!=row['acl_snapshot']:
            raise auth_error(409,'APPLICATION_CHANGED','文档批准与当前材料不匹配，拒绝读取')
        if not acl_allows(context,value):
            raise auth_error(403,'KNOWLEDGE_ACCESS_DENIED','当前部门或职级不符合文档访问范围')
        return value

    async def add_job(self,conn,row,kind,trace_id,document_id=None):
        """接收事务、原申请、任务类型与追踪，创建绑定材料指纹的持久任务，返回短任务编号。"""
        job_id = new_id()
        await conn.execute('''INSERT INTO app_knowledge.jobs(job_id,application_id,document_id,job_type,status,expected_fingerprint,trace_id)
            VALUES($1,$2,$3,$4,'pending',$5,$6)''',job_id,row['application_id'],document_id,kind,fingerprint(row),trace_id)
        return job_id

    async def approve_document(self,conn,row,trace_id,review_id=None):
        """接收已批准申请及可选审核记录，建立待处理文档与发布任务；尚不产生切片、向量或可检索状态。"""
        document_id = new_id()
        await conn.execute('''INSERT INTO app_knowledge.documents(document_id,application_id,creator_user_id,review_id,status,acl_snapshot,approved_fingerprint)
            VALUES($1,$2,$3,$4,'approved_pending_ingest',$5,$6)''',document_id,row['application_id'],row['creator_user_id'],review_id,row['acl_snapshot'],fingerprint(row))
        await self.add_job(conn,row,'publish',trace_id,document_id)
        return document_id

    async def submit(self,filename: str,data: bytes,acl: dict,context,trace_id: str) -> dict:
        """接收单个原件、已校验 ACL 与可信用户，保存新对象/申请并审计；每次上传独立，数据库失败清理本次新对象。"""
        kind = file_kind(filename,data)
        application_id = new_id()
        row = {'application_id':application_id,'creator_user_id':context.user_id,'title':filename,'filename':filename,'file_format':kind,
               'byte_size':len(data),'original_sha256':hashlib.sha256(data).hexdigest(),'object_key':'knowledge/'+application_id+'/original',
               **acl,'acl_snapshot':acl,'source_ids':[]}
        row['fingerprint'] = fingerprint(row)
        await self.storage.put(row['object_key'],data)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    actor = await self.actor(conn,context)
                    direct = actor['role']=='admin'
                    row.update(status='approved_pending_ingest' if direct else 'pending_review',approval_origin='admin_direct' if direct else None)
                    await conn.execute('''INSERT INTO app_knowledge.applications(application_id,creator_user_id,title,filename,file_format,byte_size,
                        original_sha256,object_key,allowed_department_ids,minimum_grade,status,acl_snapshot,source_ids,fingerprint,approval_origin)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)''',
                        application_id,context.user_id,row['title'],filename,kind,len(data),row['original_sha256'],row['object_key'],
                        row['allowed_department_ids'],row['minimum_grade'],row['status'],acl,[],row['fingerprint'],row['approval_origin'])
                    if direct:
                        await self.approve_document(conn,row,trace_id)
                    else:
                        await self.add_job(conn,row,'preview',trace_id)
                    await self.event(conn,trace_id,'knowledge.admin_direct_submitted' if direct else 'knowledge.submitted',row)
        except Exception:
            await self.storage.remove_new(row['object_key'])
            raise
        return metadata(await self.application(application_id,context),context)

    async def review(self,application_id: str,body,context,trace_id: str) -> dict:
        """接收材料编号、只含决定/指纹/意见的审核参数，锁定当前管理员并保存不可变审核；不修改 ACL。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self.actor(conn,context,admin=True)
                found = await conn.fetchrow('SELECT * FROM app_knowledge.applications WHERE application_id=$1 FOR UPDATE',application_id)
                if found is None:
                    raise auth_error(404,'NOT_FOUND','申请不存在')
                row = dict(found)
                if row['status']!='pending_review':
                    raise auth_error(409,'REVIEW_STATE','申请已经处理或撤回，请刷新列表')
                if body.expected_fingerprint!=row['fingerprint'] or fingerprint(row)!=row['fingerprint']:
                    raise auth_error(409,'APPLICATION_CHANGED','申请或 ACL 已变化，不能使用旧页面批准')
                if body.decision=='approved' and row['parse_status']!='completed':
                    raise auth_error(409,'PARSE_NOT_READY','正文尚未成功解析，请检查预览或驳回此申请')
                review_id = new_id()
                await conn.execute('''INSERT INTO app_knowledge.reviews(review_id,application_id,reviewed_by,decision,application_fingerprint,acl_snapshot,comment)
                    VALUES($1,$2,$3,$4,$5,$6,$7)''',review_id,application_id,context.user_id,body.decision,row['fingerprint'],row['acl_snapshot'],body.comment)
                status = 'approved_pending_ingest' if body.decision=='approved' else 'rejected'
                await conn.execute('UPDATE app_knowledge.applications SET status=$1,approval_origin=$2,updated_at=now() WHERE application_id=$3',
                    status,'review' if body.decision=='approved' else None,application_id)
                if body.decision=='approved':
                    await self.approve_document(conn,row,trace_id,review_id)
                await self.event(conn,trace_id,'knowledge.reviewed',row,{'review_id':review_id,'reviewed_by':context.user_id,'decision':body.decision,'comment':body.comment})
        return metadata(await self.application(application_id,context),context)

    async def change_state(self,application_id: str,action: str,context,trace_id: str):
        """接收维护动作和当前所有者/管理员，撤回或立即禁用文档并排清理任务；晚到解析不得重新激活。"""
        await self.application(application_id,context)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self.actor(conn,context)
                row = dict(await conn.fetchrow('SELECT * FROM app_knowledge.applications WHERE application_id=$1 FOR UPDATE',application_id))
                if row['status'] in INACTIVE:
                    return
                if action=='withdraw' and row['status']!='pending_review':
                    raise auth_error(409,'WITHDRAW_STATE','仅待审核申请可以撤回，已批准材料请删除')
                status = 'withdrawn' if action=='withdraw' else 'deleting'
                await conn.execute('UPDATE app_knowledge.applications SET status=$1,error_json=NULL,updated_at=now() WHERE application_id=$2',status,application_id)
                await conn.execute("UPDATE app_knowledge.documents SET status='invalidated' WHERE application_id=$1",application_id)
                await conn.execute("UPDATE app_knowledge.jobs SET status='cancelled',lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE application_id=$1 AND status IN ('pending','running')",application_id)
                if action=='delete':
                    await self.add_job(conn,row,'delete',trace_id)
                await self.event(conn,trace_id,'knowledge.'+action+'_requested',row,{'actor_user_id':context.user_id})

    async def retry(self,application_id: str,context,trace_id: str):
        """接收本人或管理员材料，重试未成功解析或经批准的失败任务；指纹变化、失效和超次数均拒绝。"""
        await self.application(application_id,context)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self.actor(conn,context)
                row = dict(await conn.fetchrow('SELECT * FROM app_knowledge.applications WHERE application_id=$1 FOR UPDATE',application_id))
                kind = 'preview' if row['status']=='pending_review' and row['parse_status']=='failed' else 'publish'
                if row['status']=='deleting' and row['error_json']:
                    kind = 'delete'
                if kind=='publish' and (not context.is_admin or row['status']!='failed'):
                    raise auth_error(409,'RETRY_STATE','当前材料不能重试，请刷新状态')
                if fingerprint(row)!=row['fingerprint']:
                    raise auth_error(409,'APPLICATION_CHANGED','申请或 ACL 已变化，请重新提交')
                count = await conn.fetchval('SELECT count(*) FROM app_knowledge.jobs WHERE application_id=$1 AND job_type=$2',application_id,kind)
                active = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM app_knowledge.jobs WHERE application_id=$1 AND job_type=$2 AND status IN ('pending','running'))",application_id,kind)
                if active or count>=3:
                    raise auth_error(409,'RETRY_LIMIT','任务正在处理或已达三次尝试，请重新提交文件')
                document_id = await conn.fetchval('SELECT document_id FROM app_knowledge.documents WHERE application_id=$1',application_id)
                if kind=='delete':
                    await conn.execute('UPDATE app_knowledge.applications SET error_json=NULL,updated_at=now() WHERE application_id=$1',application_id)
                    await self.add_job(conn,row,'delete',trace_id,document_id)
                    await self.event(conn,trace_id,'knowledge.delete_retry_requested',row,{'actor_user_id':context.user_id})
                    return
                await conn.execute('UPDATE app_knowledge.applications SET parse_status=\'pending\',error_json=NULL,status=$1,updated_at=now() WHERE application_id=$2',
                    'pending_review' if kind=='preview' else 'approved_pending_ingest',application_id)
                if document_id:
                    await conn.execute("UPDATE app_knowledge.documents SET status='approved_pending_ingest' WHERE document_id=$1",document_id)
                await self.add_job(conn,row,kind,trace_id,document_id)
                await self.event(conn,trace_id,'knowledge.retry_requested',row,{'kind':kind,'actor_user_id':context.user_id})
