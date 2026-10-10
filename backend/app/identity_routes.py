import asyncio
from app.ids import ShortId

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from minio import Minio
from minio.error import S3Error
from pydantic import Field, model_validator

from app.auth import (CSRF_COOKIE, SESSION_COOKIE, LoginBody, RegistrationBody, StrictBody, admin_context,
    admin_mutation_context, auth_error, current_context, login, mutation_context, register, security_event)
from app.passwords import hash_password, verify_password
from app.permissions import AuthContext

router = APIRouter(prefix="/api")


class StaffUpdate(StrictBody):
    department_id: ShortId | None
    grade: int | None = Field(ge=1,le=10)
    business_query_enabled: bool
    position: str | None = Field(default=None,max_length=80)
    expected_permission_version: int | None = Field(default=None,ge=1)

    @model_validator(mode="after")
    def valid_assignment(self):
        """验证部门、职级配对及业务资格，返回合法管理操作，不允许职级 0。"""
        if (self.department_id is None) != (self.grade is None):
            raise ValueError("部门和职级必须同时分配或清除")
        if self.business_query_enabled and self.grade is None:
            raise ValueError("未分配部门职级时不能开通业务查询")
        return self


class ProfileUpdate(StrictBody):
    display_name: str = Field(min_length=1,max_length=80)


class PasswordUpdate(StrictBody):
    current_password: str = Field(min_length=1,max_length=128)
    new_password: str = Field(min_length=12,max_length=128)


class ToolRequest(StrictBody):
    question: str = Field(min_length=1,max_length=10000)


router.add_api_route("/auth/login",login,methods=["POST"])
router.add_api_route("/auth/register",register,methods=["POST"],status_code=201)


@router.get("/auth/me")
async def me(context: AuthContext = Depends(current_context)):
    """接收服务端身份并返回本人资料与实际能力；不接受客户端指定用户 ID。"""
    return context.public_profile()


@router.post("/auth/logout")
async def logout(request: Request,response: Response,context: AuthContext = Depends(mutation_context)):
    """接收当前会话和 CSRF，原子撤销会话并审计，然后清除浏览器 Cookie。"""
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE app_system.auth_sessions SET revoked_at=now() WHERE session_id=$1",context.session_id)
            await security_event(request,"auth.logged_out",{},context,conn)
    for key in (SESSION_COOKIE,CSRF_COOKIE):
        response.delete_cookie(key,path="/",secure=request.app.state.settings.session_cookie_secure,
                               httponly=key==SESSION_COOKIE,samesite="lax")
    return {"logged_out":True}


@router.patch("/profile")
async def update_profile(body: ProfileUpdate,request: Request,context: AuthContext = Depends(mutation_context)):
    """接收本人姓名与可信身份，仅更新显示姓名并审计，不允许更新角色和职级。"""
    name = body.display_name.strip()
    if not name:
        raise auth_error(422,"INVALID_NAME","姓名不能为空")
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE app_system.users SET display_name=$1,updated_at=now() WHERE user_id=$2",name,context.user_id)
            await security_event(request,"profile.updated",{"display_name":name},context,conn)
    return {"updated":True}


@router.post("/profile/password")
async def change_password(body: PasswordUpdate,request: Request,response: Response,context: AuthContext = Depends(mutation_context)):
    """接收旧/新密码，验证旧密码后原子换哈希、撤销全部会话并审计；返回需重新登录状态。"""
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT password_hash FROM app_system.users WHERE user_id=$1 FOR UPDATE",context.user_id)
            if not await asyncio.to_thread(verify_password,row["password_hash"],body.current_password):
                await security_event(request,"auth.password_change_rejected",{},context,conn)
                changed = False
            else:
                encoded = await asyncio.to_thread(hash_password,body.new_password)
                await conn.execute("UPDATE app_system.users SET password_hash=$1,updated_at=now() WHERE user_id=$2",encoded,context.user_id)
                await conn.execute("UPDATE app_system.auth_sessions SET revoked_at=now() WHERE user_id=$1 AND revoked_at IS NULL",context.user_id)
                await security_event(request,"auth.password_changed",{"sessions_revoked":True},context,conn)
                changed = True
    if not changed:
        raise auth_error(403,"CURRENT_PASSWORD_INVALID","当前密码不正确")
    response.delete_cookie(SESSION_COOKIE,path="/",httponly=True,secure=request.app.state.settings.session_cookie_secure)
    response.delete_cookie(CSRF_COOKIE,path="/",secure=request.app.state.settings.session_cookie_secure)
    return {"login_required":True}


@router.get("/admin/departments")
async def departments(request: Request,context: AuthContext = Depends(admin_context)):
    """接收管理员身份，读取启用部门目录供分配，返回 IDs 与名称，不开放策略编辑。"""
    async with request.app.state.pool.acquire() as conn:
        return [dict(row) for row in await conn.fetch("SELECT department_id,name FROM app_system.departments WHERE enabled ORDER BY code")]


@router.get("/admin/users")
async def staff_list(request: Request,context: AuthContext = Depends(admin_context)):
    """接收当前管理员，从系统身份读取职员资料，排除密码哈希与认证凭证。"""
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch("""SELECT u.user_id,u.username,u.display_name,u.role,u.status,u.department_id,d.name AS department_name,
            u.grade,u.position,u.employee_id,u.knowledge_enabled,u.business_query_enabled,u.permission_version
            FROM app_system.users u LEFT JOIN app_system.departments d ON d.department_id=u.department_id ORDER BY u.created_at,u.username""")
        return [dict(row) for row in rows]


@router.patch("/admin/users/{user_id}")
async def update_staff(user_id: ShortId,body: StaffUpdate,request: Request,context: AuthContext = Depends(admin_mutation_context)):
    """接收目标 ID 与管理字段，锁定当前管理员/目标账号，原子赋权、失效旧审批并永久审计。"""
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            # 修改时再检查当前管理员，不能只依赖进入路由时的角色快照。
            actor = await conn.fetchrow("SELECT role,status FROM app_system.users WHERE user_id=$1 FOR UPDATE",context.user_id)
            if actor["role"] != "admin" or actor["status"] != "active":
                raise auth_error(403,"ADMIN_REQUIRED","管理员身份已失效")
            previous = await conn.fetchrow("SELECT department_id,grade,position,business_query_enabled,knowledge_enabled,permission_version FROM app_system.users WHERE user_id=$1 FOR UPDATE",user_id)
            if previous is None:
                raise auth_error(404,"NOT_FOUND","职员不存在")
            if body.expected_permission_version is not None and body.expected_permission_version != previous["permission_version"]:
                raise auth_error(409,"STAFF_CHANGED","资料已发生变化，请刷新后重试")
            if body.department_id is not None and not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM app_system.departments WHERE department_id=$1 AND enabled)",body.department_id):
                raise auth_error(422,"INVALID_DEPARTMENT","请选择有效部门")
            knowledge = body.department_id is not None and body.grade is not None
            version = await conn.fetchval("""UPDATE app_system.users SET department_id=$1,grade=$2,position=$3,
                business_query_enabled=$4,knowledge_enabled=$5,permission_version=permission_version+1,updated_at=now()
                WHERE user_id=$6 RETURNING permission_version""",body.department_id,body.grade,body.position,body.business_query_enabled,knowledge,user_id)
            await conn.execute("UPDATE app_runtime.query_approvals SET decision='invalidated' WHERE user_id=$1 AND decision='pending'",user_id)
            await security_event(request,"identity.permissions_changed",{"target_user_id":str(user_id),
                "before":{**dict(previous),"department_id":str(previous["department_id"]) if previous["department_id"] else None},"after":{"department_id":str(body.department_id) if body.department_id else None,
                "grade":body.grade,"position":body.position,"business_query_enabled":body.business_query_enabled,
                "knowledge_enabled":knowledge,"permission_version":version},"cache_policy":"identity_read_from_pg_every_request"},context,conn)
    return {"updated":True,"permission_version":version}


@router.post("/tools/search_knowledge")
async def knowledge_gate(body: ToolRequest,request: Request,context: AuthContext = Depends(mutation_context)):
    """接收问题与当前身份，在任何召回前校验资格并执行真实混合 RAG，返回有权访问的来源。"""
    if not context.can_use_knowledge:
        await security_event(request,"permissions.denied",{"capability":"knowledge"},context)
        raise auth_error(403,"KNOWLEDGE_NOT_AUTHORIZED","尚未获得知识库权限，请联系管理员分配部门和职级")
    return await request.app.state.rag.search(body.question,context,request.state.trace_id)


@router.get("/admin/audit/status")
async def audit_status(context: AuthContext = Depends(admin_context)):
    """接收可信管理员，只验证审计管理入口权限；查询工作台未实现时明确返回 501。"""
    raise auth_error(501,"FEATURE_NOT_READY","管理员审计查询页面将在后续阶段开放")


@router.post("/tools/query_business_data")
async def business_gate(body: ToolRequest,request: Request,context: AuthContext = Depends(mutation_context)):
    """接收问题并先检查业务资格，不执行 SQL、不向模型提供 schema；合法请求也明确未开放。"""
    if not context.can_query_business:
        await security_event(request,"permissions.denied",{"capability":"business_query"},context)
        raise auth_error(403,"BUSINESS_NOT_AUTHORIZED","尚未获得业务查询权限，请联系管理员开通")
    raise auth_error(501,"FEATURE_NOT_READY","业务查询将在后续阶段开放，当前没有执行 SQL")


@router.get("/conversations")
async def conversations(request: Request,context: AuthContext = Depends(current_context)):
    """接收可信用户，返回本人会话元数据，不接受客户端用户 ID 作为授权依据。"""
    async with request.app.state.pool.acquire() as conn:
        return [dict(row) for row in await conn.fetch("SELECT conversation_id,title,created_at,updated_at FROM app_runtime.conversations WHERE user_id=$1 ORDER BY updated_at DESC",context.user_id)]


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: ShortId,request: Request,context: AuthContext = Depends(current_context)):
    """接收资源 ID 与可信用户，在读取原消息前校验本人归属；不存在或他人资源统一 404。"""
    async with request.app.state.pool.acquire() as conn:
        owned = await conn.fetchrow("SELECT conversation_id,title FROM app_runtime.conversations WHERE conversation_id=$1 AND user_id=$2",conversation_id,context.user_id)
        if owned is not None:
            messages = await conn.fetch("""SELECT m.message_id,m.role,m.content,m.created_at,m.message_order,m.run_id,r.status AS run_status,
                r.state_json->'error' AS error,m.protocol_json->>'model' AS model,m.protocol_json->'usage' AS usage,m.protocol_json->>'finish_reason' AS finish_reason,
                m.protocol_json->'message'->'citations' AS citations
                FROM app_runtime.messages m LEFT JOIN app_runtime.runs r ON r.run_id=m.run_id
                WHERE m.conversation_id=$1 AND m.user_id=$2 ORDER BY m.message_order""",conversation_id,context.user_id)
            active = await conn.fetchrow("SELECT run_id,trace_id,status,created_at,updated_at FROM app_runtime.runs WHERE conversation_id=$1 AND user_id=$2 AND status IN ('pending','running') ORDER BY created_at DESC LIMIT 1",conversation_id,context.user_id)
            last_run = await conn.fetchrow("SELECT run_id,trace_id,conversation_id,status,created_at,updated_at,state_json->'error' AS error FROM app_runtime.runs WHERE conversation_id=$1 AND user_id=$2 ORDER BY created_at DESC LIMIT 1",conversation_id,context.user_id)
            public_messages = []
            for message in messages:
                value = dict(message)
                # 原引用档案保留，但历史接口不重放受限正文；点击来源时重新请求当前鉴权接口。
                value['citations'] = [{key:item.get(key) for key in ('type','label','chunk_id','document_id','filename','page','page_numbers','heading','heading_path','position',
                    'source_id','title','url','website','published_at','fetched_at','content_kind','page_read','truncated')} for item in (value['citations'] or [])]
                public_messages.append(value)
            return {**dict(owned),"messages":public_messages,"active_run":dict(active) if active else None,"last_run":dict(last_run) if last_run else None}
    await security_event(request,"resources.access_rejected",{"resource":"conversation","resource_id":str(conversation_id)},context)
    raise auth_error(404,"NOT_FOUND","会话不存在或不可访问")


async def owned_attachment(attachment_id: ShortId,request: Request,context: AuthContext) -> dict:
    """接收附件 ID、请求和可信身份，从 PG 检查附件及关联会话归属，返回内部元数据。"""
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow("""SELECT a.* FROM app_runtime.attachments a WHERE attachment_id=$1 AND user_id=$2
            AND (conversation_id IS NULL OR EXISTS(SELECT 1 FROM app_runtime.conversations c WHERE c.conversation_id=a.conversation_id AND c.user_id=$2))""",attachment_id,context.user_id)
    if row is None:
        await security_event(request,"resources.access_rejected",{"resource":"attachment","resource_id":str(attachment_id)},context)
        raise auth_error(404,"NOT_FOUND","附件不存在或不可访问")
    return dict(row)


@router.get("/attachments/{attachment_id}")
async def attachment_metadata(attachment_id: ShortId,request: Request,context: AuthContext = Depends(current_context)):
    """接收附件 ID 并校验归属，返回类型与下载入口，不暴露对象路径、Bucket 或凭证。"""
    row = await owned_attachment(attachment_id,request,context)
    return {"attachment_id":str(attachment_id),"mime_type":row["mime_type"],"sha256":row["sha256"],"download_url":f"/api/attachments/{attachment_id}/download"}


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(attachment_id: ShortId,request: Request,context: AuthContext = Depends(current_context)):
    """接收资源 ID，鉴权后从应用 Bucket 流式下载；内容只作为附件输出，绝不执行脚本。"""
    row = await owned_attachment(attachment_id,request,context)
    settings = request.app.state.settings
    if row["bucket"] != settings.app_minio_bucket:
        raise auth_error(404,"NOT_FOUND","附件存储位置不可用")
    client = Minio(settings.app_minio_endpoint,access_key=settings.app_minio_access_key,
        secret_key=settings.app_minio_secret_key.get_secret_value(),secure=False,region="us-east-1")
    try:
        stream = await asyncio.to_thread(client.get_object,row["bucket"],row["object_key"])
    except S3Error:
        raise auth_error(404,"NOT_FOUND","附件不存在或不可访问")

    def chunks():
        """无参数读取已授权对象块，结束或断线后关闭连接；逐块产出字节内容。"""
        try:
            yield from stream.stream(64*1024)
        finally:
            stream.close()
            stream.release_conn()

    return StreamingResponse(chunks(),media_type="application/octet-stream",headers={
        "Content-Disposition":f'attachment; filename="attachment-{attachment_id}"',"X-Content-Type-Options":"nosniff"})
