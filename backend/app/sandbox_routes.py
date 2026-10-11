"""会话分析文件和产物路由，所有访问以当前登录用户为准，不接受对象键或宿主路径。"""
import hashlib
from pathlib import PurePath
from urllib.parse import quote
from fastapi import APIRouter,Depends,File,Request,UploadFile
from fastapi.responses import Response
from app.auth import auth_error,current_context,mutation_context,security_event
from app.ids import ShortId,new_id

router=APIRouter(prefix='/api')


@router.post('/conversations/{conversation_id}/attachments',status_code=201)
async def upload(conversation_id: ShortId,request: Request,files: list[UploadFile]=File(...),context=Depends(mutation_context)):
    """接收本人会话CSV/XLSX字节，仅校验扩展名/大小/指纹；正文在沙箱内检查，不在API开表。"""
    pool=request.app.state.pool;storage=request.app.state.knowledge_storage
    async with pool.acquire() as conn:
        if not await conn.fetchval('SELECT 1 FROM app_runtime.conversations WHERE conversation_id=$1 AND user_id=$2',conversation_id,context.user_id):raise auth_error(404,'NOT_FOUND','会话不可访问')
        count=await conn.fetchval('SELECT count(*) FROM app_runtime.analysis_attachments WHERE conversation_id=$1',conversation_id)
    if len(files)>10 or count+len(files)>30:raise auth_error(413,'FILE_LIMIT','会话最多30个分析附件，单批最多10个')
    total=0;answer=[]
    for file in files:
        name=PurePath((file.filename or '').replace('\\','/')).name[:180];kind=name.rsplit('.',1)[-1].lower()
        if kind not in ('csv','xlsx'):raise auth_error(422,'FORMAT_NOT_SUPPORTED','分析附件只支持CSV/XLSX')
        raw=await file.read(20*1024*1024+1);total+=len(raw)
        if not raw or len(raw)>20*1024*1024 or total>50*1024*1024:raise auth_error(413,'FILE_LIMIT','单文件20MiB，批次50MiB')
        ident=new_id();key=f'analysis/{context.user_id}/{ident}';sha=hashlib.sha256(raw).hexdigest()
        await storage.put(key,raw)
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute('INSERT INTO app_runtime.analysis_attachments(attachment_id,user_id,conversation_id,filename,format,size_bytes,original_sha256,object_key) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',ident,context.user_id,conversation_id,name,kind,len(raw),sha,key)
                    await security_event(request,'analysis.uploaded',{'attachment_id':ident,'filename':name,'bytes':len(raw),'sha256':sha},context,conn)
        except BaseException:await storage.remove_new(key);raise
        answer.append({'attachment_id':ident,'filename':name,'format':kind,'size_bytes':len(raw),'original_sha256':sha})
    return answer


@router.get('/conversations/{conversation_id}/attachments')
async def attachments(conversation_id: ShortId,request: Request,context=Depends(current_context)):
    """返回本人会话原文件元数据，刷新可恢复附件列表，不返回存储密钥。"""
    async with request.app.state.pool.acquire() as conn:
        if not await conn.fetchval('SELECT 1 FROM app_runtime.conversations WHERE conversation_id=$1 AND user_id=$2',conversation_id,context.user_id):raise auth_error(404,'NOT_FOUND','会话不可访问')
        return [dict(row) for row in await conn.fetch('SELECT attachment_id,filename,format,size_bytes,original_sha256 FROM app_runtime.analysis_attachments WHERE conversation_id=$1 AND user_id=$2 ORDER BY created_at',conversation_id,context.user_id)]


@router.get('/sandbox/results/{result_id}')
async def result(result_id: ShortId,request: Request,context=Depends(current_context)):
    """读取本人持久化沙箱结果，猜测他人结果ID统一404，读取不重新执行代码。"""
    async with request.app.state.pool.acquire() as conn:
        value=await conn.fetchval('SELECT result_json FROM app_runtime.sandbox_results WHERE result_id=$1 AND user_id=$2',result_id,context.user_id)
    if value is None:raise auth_error(404,'NOT_FOUND','结果不可访问')
    return value


@router.get('/sandbox/artifacts/{artifact_id}')
async def artifact(artifact_id: ShortId,request: Request,context=Depends(current_context)):
    """核验本人产物SHA后提供PNG安全预览或CSV下载，不运行HTML/JS或暴露原对象URL。"""
    async with request.app.state.pool.acquire() as conn:
        row=await conn.fetchrow('SELECT a.* FROM app_runtime.sandbox_artifacts a JOIN app_runtime.sandbox_results r USING(result_id) WHERE artifact_id=$1 AND r.user_id=$2',artifact_id,context.user_id)
    if row is None:raise auth_error(404,'NOT_FOUND','产物不可访问')
    data=await request.app.state.knowledge_storage.read_verified({**dict(row),'original_sha256':row['sha256']})
    png=row['filename'].endswith('.png')
    return Response(data,media_type='image/png' if png else 'text/csv',headers={'Content-Disposition':('inline' if png else 'attachment')+"; filename*=UTF-8''"+quote(row['filename']),'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})
