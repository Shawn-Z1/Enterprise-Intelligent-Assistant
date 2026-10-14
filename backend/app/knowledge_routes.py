import base64
import json
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import Field, ValidationError, field_validator
from starlette.datastructures import UploadFile

from app.auth import StrictBody, auth_error, current_context, mutation_context, security_event
from app.ids import ShortId
from app.knowledge_service import KnowledgeService, metadata
from app.permissions import AuthContext

router = APIRouter(prefix='/api/knowledge')


class ACLBody(StrictBody):
    allowed_department_ids: list[ShortId] = Field(default_factory=list,max_length=20)
    minimum_grade: int | None = Field(default=None,ge=1,le=10)

    @field_validator('allowed_department_ids')
    @classmethod
    def normalize_departments(cls,value):
        """接收部门编号数组，去重排序后返回稳定 ACL，不允许客户端自行指定创建者身份。"""
        return sorted(set(value))


class ReviewBody(StrictBody):
    decision: Literal['approved','rejected']
    expected_fingerprint: str = Field(min_length=64,max_length=64,pattern='^[a-f0-9]+$')
    comment: str = Field(default='',max_length=2000)


def service(request: Request) -> KnowledgeService:
    """接收请求，返回复用当前低权限池、应用存储和审计的知识服务，无新凭证或厂商客户端。"""
    return KnowledgeService(request.app.state.pool,request.app.state.knowledge_storage,request.app.state.audit)


async def qualified(request: Request,context: AuthContext=Depends(mutation_context)):
    """接收已认证写操作身份，在解析上传或修改材料前检查当前知识资格，成功返回可信用户。"""
    if not context.can_use_knowledge:
        await security_event(request,'permissions.denied',{'capability':'knowledge_management'},context)
        raise auth_error(403,'KNOWLEDGE_NOT_AUTHORIZED','尚未获得知识权限，请联系管理员分配部门和职级')
    return context


@router.get('/options')
async def options(request: Request,context: AuthContext=Depends(current_context)):
    """接收当前身份，返回合法上传限制和启用部门；未授权用户不取得用于提交的目录。"""
    if not context.can_use_knowledge:
        raise auth_error(403,'KNOWLEDGE_NOT_AUTHORIZED','尚未获得知识权限')
    async with request.app.state.pool.acquire() as conn:
        departments = [dict(row) for row in await conn.fetch('SELECT department_id,name FROM app_system.departments WHERE enabled ORDER BY code')]
    settings = request.app.state.settings
    return {'departments':departments,'max_files':settings.knowledge_max_files,'max_file_bytes':settings.knowledge_max_file_bytes,'max_batch_bytes':settings.knowledge_max_batch_bytes,'formats':['pdf','docx','txt','md']}


@router.post('/applications')
async def upload(request: Request,context: AuthContext=Depends(qualified)):
    """接收受限 multipart 批量原件与 ACL，逐文件保存独立申请并返回独立结果；任何未批准文件都不入索引。"""
    settings = request.app.state.settings
    received = 0
    async def limited_receive():
        """无参数有界读取实际请求字节，超过总量立即拒绝，限制未声明长度的上传请求。"""
        nonlocal received
        message = await request.receive()
        received += len(message.get('body',b''))
        if received>settings.knowledge_max_batch_bytes+65536:
            raise auth_error(413,'BATCH_LIMIT','批量上传超过总大小限制，请分批提交')
        return message
    length = request.headers.get('content-length','')
    if length.isdigit() and int(length)>settings.knowledge_max_batch_bytes+65536:
        raise auth_error(413,'BATCH_LIMIT','批量上传超过总大小限制，请分批提交')
    guarded = Request(request.scope,receive=limited_receive)
    async with guarded.form(max_files=settings.knowledge_max_files,max_fields=1,max_part_size=65536) as form:
        if set(form.keys())-{'files','metadata'} or not isinstance(form.get('metadata','{}'),str):
            raise auth_error(422,'INVALID_UPLOAD','上传字段不正确')
        try:
            acl = ACLBody.model_validate_json(form.get('metadata','{}')).model_dump()
        except ValidationError:
            raise auth_error(422,'INVALID_ACL','访问范围不正确，最低职级只能为 1～10 或不限')
        files = form.getlist('files')
        if not files or any(not isinstance(item,UploadFile) for item in files):
            raise auth_error(422,'NO_FILES','请选择至少一个文件')
        async with request.app.state.pool.acquire() as conn:
            count = await conn.fetchval('SELECT count(*) FROM app_system.departments WHERE enabled AND department_id=ANY($1::varchar[])',acl['allowed_department_ids'])
        if count!=len(acl['allowed_department_ids']):
            raise auth_error(422,'INVALID_DEPARTMENT','请选择有效的允许部门')
        results,total = [],0
        for item in files:
            name = item.filename or ''
            try:
                if item.size is not None and item.size>settings.knowledge_max_file_bytes:
                    raise auth_error(413,'FILE_LIMIT','单个文件超过大小限制')
                data = await item.read(settings.knowledge_max_file_bytes+1)
                total += len(data)
                if len(data)>settings.knowledge_max_file_bytes or total>settings.knowledge_max_batch_bytes:
                    raise auth_error(413,'FILE_LIMIT','文件超过单个或批量大小限制')
                row = await service(request).submit(name,data,acl,context,request.state.trace_id)
                results.append({'filename':name,'success':True,'application':row})
            except Exception as error:
                detail = getattr(error,'detail',None)
                if not isinstance(detail,dict):
                    detail = {'code':'UPLOAD_STORAGE_ERROR','message':'文件保存或审计失败，请稍后重试'}
                await security_event(request,'knowledge.upload_rejected',{'filename':name[:200],'error':detail},context)
                results.append({'filename':name,'success':False,'error':detail})
    return {'results':results}


@router.get('/applications')
async def applications(request: Request,scope: Literal['mine','all']='mine',limit: int=Query(100,ge=1,le=200),offset: int=Query(0,ge=0),context: AuthContext=Depends(current_context)):
    """接收目录范围和分页，普通用户仅列本人管理资料；管理员可选择全部，不返回原件路径或正文。"""
    if scope=='all' and not context.is_admin:
        raise auth_error(403,'ADMIN_REQUIRED','仅管理员可管理全部申请')
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch('''SELECT a.*,d.document_id,d.status AS document_status,u.display_name AS creator_name FROM app_knowledge.applications a
            LEFT JOIN app_knowledge.documents d USING(application_id) JOIN app_system.users u ON u.user_id=a.creator_user_id WHERE a.management_deleted_at IS NULL AND ($1::boolean OR a.creator_user_id=$2)
            ORDER BY a.created_at DESC,a.application_id LIMIT $3 OFFSET $4''',scope=='all',context.user_id,limit,offset)
    return [metadata(dict(row),context) for row in rows]


@router.get('/applications/catalog')
async def application_catalog(request: Request,scope: Literal['mine','all']='mine',category: Literal['effective','review','processing','progress','failed','history']='effective',
                              q: str=Query('',max_length=200),file_format: Literal['','pdf','docx','txt','md']='',department_id: ShortId | None=None,
                              page: int=Query(1,ge=1),limit: int=Query(10,ge=1,le=50),context: AuthContext=Depends(current_context)):
    """接收范围、分类、搜索和分页，在同一只读快照内先鉴权筛选，再统计与分页；成员仅查看本人且无管理员审核分类。"""
    if not context.is_admin and (scope=='all' or category in ('review','processing')):
        raise auth_error(403,'ADMIN_REQUIRED','普通成员仅可查看自己的提交进度')
    # 分类以真实申请和索引状态为准，历史记录不混入生效列表，所有筛选均在分页前完成。
    base = '''WITH catalog AS (SELECT a.*,d.document_id,d.status AS document_status,u.display_name AS creator_name,
        CASE WHEN a.status IN ('invalidated','withdrawn','rejected') THEN 'history'
             WHEN a.status='failed' OR (a.status='deleting' AND a.error_json IS NOT NULL) THEN 'failed'
             WHEN a.status='pending_review' THEN 'review'
             WHEN a.status='retrievable' AND d.status='retrievable' THEN 'effective'
             ELSE 'processing' END AS category
        FROM app_knowledge.applications a LEFT JOIN app_knowledge.documents d USING(application_id)
        JOIN app_system.users u ON u.user_id=a.creator_user_id
        WHERE a.management_deleted_at IS NULL AND ($1::boolean OR a.creator_user_id=$2)
          AND ($3::text='' OR strpos(lower(a.filename||' '||u.display_name),lower($3))>0)
          AND ($4::text='' OR a.file_format=$4)
          AND ($5::varchar IS NULL OR cardinality(a.allowed_department_ids)=0 OR $5=ANY(a.allowed_department_ids))) '''
    args = (scope=='all',context.user_id,q.strip(),file_format,department_id)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction(isolation='repeatable_read',readonly=True):
            counts = {key:0 for key in ('effective','review','processing','failed','history')}
            counts.update({row['category']:row['count'] for row in await conn.fetch(base+'SELECT category,count(*) AS count FROM catalog GROUP BY category',*args)})
            counts['progress'] = counts['review']+counts['processing']
            total = counts[category]
            page = min(page,max(1,(total+limit-1)//limit))
            rows = await conn.fetch(base+'''SELECT * FROM catalog WHERE category=$6 OR ($6='progress' AND category IN ('review','processing'))
                ORDER BY updated_at DESC,application_id LIMIT $7 OFFSET $8''',*args,category,limit,(page-1)*limit)
    visible_counts = counts if context.is_admin else {key:counts[key] for key in ('effective','progress','failed','history')}
    return {'items':[metadata(dict(row),context) for row in rows],'total':total,'counts':visible_counts,'page':page,'limit':limit}


@router.get('/applications/{application_id}')
async def application_detail(application_id: ShortId,request: Request,context: AuthContext=Depends(current_context)):
    """接收申请编号和当前用户，返回本人或管理员可见管理资料、审核与任务履历，排除正文。"""
    row = await service(request).application(application_id,context)
    async with request.app.state.pool.acquire() as conn:
        reviews = await conn.fetch('SELECT review_id,reviewed_by,decision,comment,created_at FROM app_knowledge.reviews WHERE application_id=$1 ORDER BY created_at',application_id)
        jobs = await conn.fetch('SELECT job_id,job_type,status,error_json,created_at,updated_at FROM app_knowledge.jobs WHERE application_id=$1 ORDER BY created_at',application_id)
    return {**metadata(row,context),'reviews':[dict(item) for item in reviews],'jobs':[dict(item) for item in jobs]}


@router.post('/applications/{application_id}/review')
async def review(application_id: ShortId,body: ReviewBody,request: Request,context: AuthContext=Depends(qualified)):
    """接收只读审批快照与通过/驳回决定，调用统一审核服务；普通成员不能因高职级变成审核者。"""
    if not context.is_admin:
        await security_event(request,'permissions.denied',{'capability':'knowledge_review'},context)
        raise auth_error(403,'ADMIN_REQUIRED','仅管理员可以审核')
    return await service(request).review(application_id,body,context,request.state.trace_id)


@router.post('/applications/{application_id}/{action}')
async def maintain(application_id: ShortId,action: Literal['withdraw','delete','retry','remove-record'],request: Request,context: AuthContext=Depends(mutation_context)):
    """接收材料和明确维护动作，服务器验证所有者及状态；删除立即拒绝正文读取，后台完成失效。"""
    if action=='remove-record':
        await service(request).remove_record(application_id,context,request.state.trace_id)
        return {'updated':True}
    await qualified(request,context)
    if action=='retry':
        await service(request).retry(application_id,context,request.state.trace_id)
    else:
        await service(request).change_state(application_id,action,context,request.state.trace_id)
    return {'updated':True}


async def read_row(request,context,identifier,formal):
    """接收资源模式及当前身份，读取已授权材料或正式来源，返回内部元数据，统一所有预览与下载边界。"""
    return await (service(request).document(identifier,context) if formal else service(request).application(identifier,context,body=True))


@router.get('/sources/{chunk_id}')
async def source(chunk_id: ShortId,request: Request,context: AuthContext=Depends(current_context)):
    """接收引用切片编号，逐次验证正式来源的当前状态/ACL/版本及原件，返回定位正文；不授予他人管理权限。"""
    async with request.app.state.pool.acquire() as conn:
        chunk = await conn.fetchrow('SELECT * FROM app_knowledge.chunks WHERE chunk_id=$1 AND status=\'active\'',chunk_id)
    if chunk is None:
        raise auth_error(404,'SOURCE_UNAVAILABLE','知识来源已失效或不可访问')
    row = await service(request).document(chunk['document_id'],context)
    if row['document_status']!='retrievable' or row['embedding_space']!=chunk['embedding_space'] or row['index_version']!=chunk['index_version'] or row['acl_snapshot']!=chunk['acl_snapshot'] or row['original_sha256']!=chunk['source_sha256']:
        raise auth_error(404,'SOURCE_UNAVAILABLE','知识来源版本已经失效')
    await request.app.state.knowledge_storage.read_verified(row)
    await service(request).document(chunk['document_id'],await current_context(request))
    await security_event(request,'knowledge.source_read',{'chunk_id':chunk_id,'document_id':chunk['document_id']},context)
    return {'chunk_id':chunk_id,'document_id':chunk['document_id'],'filename':row['filename'],'file_format':row['file_format'],
            'page':chunk['page_number'],'page_numbers':chunk['page_numbers'],'position':chunk['position'],
            'heading':chunk['heading'],'heading_path':chunk['heading_path'],'section_paths':chunk['section_paths'],'content':chunk['content']}


@router.get('/applications/{identifier}/preview')
@router.get('/documents/{identifier}/preview')
async def preview(identifier: ShortId,request: Request,context: AuthContext=Depends(current_context)):
    """接收材料/文档编号，在前后两次鉴权和原件哈希检查后返回实际解析正文，不执行 HTML 或脚本。"""
    formal = '/documents/' in request.url.path
    row = await read_row(request,context,identifier,formal)
    await request.app.state.knowledge_storage.read_verified(row)
    async with request.app.state.pool.acquire() as conn:
        parsed = await conn.fetchval('SELECT content_json FROM app_knowledge.parses WHERE application_id=$1 AND fingerprint=$2',row['application_id'],row['fingerprint'])
    if parsed is None:
        raise auth_error(409,'PREVIEW_NOT_READY','正文正在解析或解析失败，请刷新处理状态')
    await read_row(request,await current_context(request),identifier,formal)
    await security_event(request,'knowledge.preview_read',{'application_id':row['application_id']},context)
    return {'application_id':row['application_id'],'filename':row['filename'],**parsed}


@router.get('/applications/{identifier}/pages/{page}')
@router.get('/documents/{identifier}/pages/{page}')
async def pdf_page(identifier: ShortId,page: int,request: Request,context: AuthContext=Depends(current_context)):
    """接收 PDF 编号与从 1 起页码，鉴权后隔离渲染 PNG，并在输出前复检状态与当前权限。"""
    formal = '/documents/' in request.url.path
    row = await read_row(request,context,identifier,formal)
    if row['file_format']!='pdf' or page<1:
        raise auth_error(422,'INVALID_PAGE','仅 PDF 支持页面预览，页码从 1 开始')
    data = await request.app.state.knowledge_storage.read_verified(row)
    image = await request.app.state.knowledge_storage.parse(data,'pdf',page)
    await read_row(request,await current_context(request),identifier,formal)
    await security_event(request,'knowledge.page_read',{'application_id':row['application_id'],'page':page},context)
    return Response(base64.b64decode(image['png']),media_type='image/png',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@router.get('/applications/{identifier}/download')
@router.get('/documents/{identifier}/download')
async def download(identifier: ShortId,request: Request,context: AuthContext=Depends(current_context)):
    """接收材料/文档编号，每次读取鉴权并核验原件；仅作为附件输出，不泄露 MinIO URL 或执行文件脚本。"""
    formal = '/documents/' in request.url.path
    row = await read_row(request,context,identifier,formal)
    data = await request.app.state.knowledge_storage.read_verified(row)
    await read_row(request,await current_context(request),identifier,formal)
    await security_event(request,'knowledge.original_downloaded',{'application_id':row['application_id']},context)
    return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(row['filename'],safe=''),
        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
