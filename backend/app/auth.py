import asyncio
import hashlib
import hmac
import secrets
from datetime import timedelta
from app.timezones import beijing_now
from app.ids import ShortId, new_id

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.passwords import hash_password, verify_password
from app.permissions import AuthContext

SESSION_COOKIE = "zhixing_session"
CSRF_COOKIE = "zhixing_csrf"
# 不存在的账号也执行相同的密码验证，减少账号存在性时序差异。
DUMMY_HASH = hash_password("M03-dummy-password-not-a-credential")


class StrictBody(BaseModel):
    """接口只接收明确字段，禁止静默接受角色、用户 ID 或职级自选参数。"""
    model_config = ConfigDict(extra="forbid")


class LoginBody(StrictBody):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        """接收账号字符串，统一小写后返回，避免大小写重复账号。"""
        return value.lower()


class RegistrationBody(LoginBody):
    password: str = Field(min_length=12, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        """接收姓名，去除首尾空白并拒绝空姓名，返回可展示文本。"""
        if not value.strip():
            raise ValueError("姓名不能为空")
        return value.strip()


def digest(value: str) -> str:
    """接收随机会话或 CSRF 字符串，返回 SHA-256 摘要，不存储明文凭证。"""
    return hashlib.sha256(value.encode()).hexdigest()


def auth_error(status: int, code: str, message: str) -> HTTPException:
    """接收状态、错误码与中文说明，返回统一身份/权限异常，不含敏感输入。"""
    return HTTPException(status_code=status, detail={"code":code,"message":message})


async def security_event(request: Request, event_type: str, data: dict, context: AuthContext | None = None, connection=None) -> None:
    """接收请求、事件和可选可信上下文/事务，追加永久安全审计；失败禁止放行。"""
    await request.app.state.audit.append(request.state.trace_id, event_type, data,
        user_id=context.user_id if context else None, span_name="identity.permission", connection=connection)


def check_origin(request: Request) -> None:
    """接收请求，拒绝浏览器跨源写操作；无 Origin 的非浏览器客户端仍需会话 CSRF 校验。"""
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise auth_error(403,"ORIGIN_DENIED","不允许跨站请求")


async def current_context(request: Request) -> AuthContext:
    """接收请求，从 HttpOnly 会话摘要和当前账号读取可信身份；无效会话审计后返回 401。"""
    token = request.cookies.get(SESSION_COOKIE, "")
    row = None
    if 20 <= len(token) <= 128:
        async with request.app.state.pool.acquire() as conn:
            row = await conn.fetchrow("""SELECT u.user_id,u.username,u.display_name,u.role,u.department_id,
                d.name AS department_name,u.grade,u.employee_id,u.position,u.knowledge_enabled,
                u.business_query_enabled,u.permission_version,s.session_id,s.csrf_token_hash
                FROM app_system.auth_sessions s JOIN app_system.users u ON u.user_id=s.user_id
                LEFT JOIN app_system.departments d ON d.department_id=u.department_id
                WHERE s.token_hash=$1 AND s.revoked_at IS NULL AND s.expires_at>now() AND u.status='active'""", digest(token))
    if row is None or not row["csrf_token_hash"]:
        await security_event(request,"auth.session_rejected",{"reason":"invalid_expired_or_disabled"})
        raise auth_error(401,"UNAUTHENTICATED","请登录后继续")
    context = AuthContext(**dict(row))
    request.state.auth_context = context
    return context


async def mutation_context(request: Request, context: AuthContext = Depends(current_context)) -> AuthContext:
    """接收请求和可信身份，校验同源与绑定当前会话的 CSRF；成功返回上下文。"""
    header = request.headers.get("x-csrf-token", "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    try:
        check_origin(request)
        if not 20<=len(header)<=128 or not header.isascii() or not cookie.isascii() or not hmac.compare_digest(header, cookie) or not hmac.compare_digest(digest(header), context.csrf_token_hash):
            raise auth_error(403,"CSRF_REJECTED","请求校验失败，请刷新页面后重试")
    except HTTPException:
        await security_event(request,"auth.csrf_rejected",{},context)
        raise
    return context


async def admin_context(request: Request, context: AuthContext = Depends(current_context)) -> AuthContext:
    """接收请求及当前身份；仅管理员可继续，普通成员的伪造请求写审计并返回 403。"""
    if not context.is_admin:
        await security_event(request,"permissions.denied",{"capability":"staff_management"},context)
        raise auth_error(403,"ADMIN_REQUIRED","仅系统管理员可以管理职员")
    return context


async def admin_mutation_context(request: Request, context: AuthContext = Depends(mutation_context)) -> AuthContext:
    """接收已校验 CSRF 的身份，再验证当前管理员角色；返回可信管理员上下文。"""
    return await admin_context(request, context)


async def issue_session(conn, request: Request, user_id: ShortId) -> tuple[str,str]:
    """接收事务连接、请求和服务端用户 ID，创建有限期会话；返回仅用于响应 Cookie 的随机凭证。"""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    await conn.execute("""INSERT INTO app_system.auth_sessions(session_id,user_id,token_hash,csrf_token_hash,expires_at)
        VALUES($1,$2,$3,$4,$5)""", new_id(),user_id,digest(token),digest(csrf),
        beijing_now()+timedelta(hours=request.app.state.settings.session_hours))
    return token,csrf


def attach_cookies(response: Response, request: Request, token: str, csrf: str) -> None:
    """接收响应、配置和随机凭证，写入 HttpOnly 会话及可读 CSRF Cookie，不返回令牌 JSON。"""
    options = {"max_age":request.app.state.settings.session_hours*3600,"secure":request.app.state.settings.session_cookie_secure,"samesite":"lax","path":"/"}
    response.set_cookie(SESSION_COOKIE,token,httponly=True,**options)
    response.set_cookie(CSRF_COOKIE,csrf,httponly=False,**options)


async def login(request: Request, response: Response, body: LoginBody) -> dict:
    """接收登录凭据，以 Argon2id 验证并原子创建会话和审计；返回账号摘要，不回显密码。"""
    check_origin(request)
    async with request.app.state.pool.acquire() as conn:
        bucket = digest(f"{request.client.host}:{body.username}")
        attempts = await conn.fetchval("""INSERT INTO app_system.login_limits(bucket_key,attempts) VALUES($1,1)
            ON CONFLICT(bucket_key) DO UPDATE SET attempts=CASE WHEN login_limits.window_start<now()-interval '1 minute' THEN 1 ELSE login_limits.attempts+1 END,
            window_start=CASE WHEN login_limits.window_start<now()-interval '1 minute' THEN now() ELSE login_limits.window_start END RETURNING attempts""",bucket)
        row = await conn.fetchrow("SELECT user_id,password_hash,status FROM app_system.users WHERE username=$1",body.username)
    # 先释放查询连接，再做密码校验和独立审计，避免并发失败登录耗尽连接池后互相等待。
    if attempts > request.app.state.settings.login_limit_per_minute:
        await security_event(request,"auth.login_limited",{"username":body.username})
        raise auth_error(429,"LOGIN_RATE_LIMIT","登录尝试过于频繁，请稍后重试")
    valid = await asyncio.to_thread(verify_password,row["password_hash"] if row else DUMMY_HASH,body.password)
    if not row or not valid or row["status"] != "active":
        await security_event(request,"auth.login_failed",{"username":body.username})
        raise auth_error(401,"INVALID_CREDENTIALS","账号或密码不正确")
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            # 再锁定账号状态，避免校验密码期间账号已被禁用或密码已改变。
            locked = await conn.fetchrow("SELECT password_hash,status FROM app_system.users WHERE user_id=$1 FOR UPDATE",row["user_id"])
            if locked["status"] != "active" or locked["password_hash"] != row["password_hash"]:
                raise auth_error(401,"INVALID_CREDENTIALS","账号或密码不正确")
            token,csrf = await issue_session(conn,request,row["user_id"])
            await request.app.state.audit.append(request.state.trace_id,"auth.login_succeeded",{},user_id=row["user_id"],connection=conn)
    attach_cookies(response,request,token,csrf)
    return {"user_id":str(row["user_id"])}


async def register(request: Request, response: Response, body: RegistrationBody) -> dict:
    """接收有限注册字段，创建零权限普通成员并登录；账号和会话与审计共同提交。"""
    check_origin(request)
    encoded = await asyncio.to_thread(hash_password,body.password)
    user_id = new_id()
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            created = await conn.fetchval("""INSERT INTO app_system.users(user_id,username,display_name,password_hash,role)
                VALUES($1,$2,$3,$4,'member') ON CONFLICT(username) DO NOTHING RETURNING user_id""",user_id,body.username,body.display_name,encoded)
            if created is None:
                await security_event(request,"auth.registration_rejected",{"reason":"duplicate_username"},connection=conn)
                # 拒绝审计在本事务正常提交，错误响应在事务外产生。
                token = None
            else:
                token,csrf = await issue_session(conn,request,user_id)
                await request.app.state.audit.append(request.state.trace_id,"auth.registered",{"username":body.username,"role":"member","authorized":False},user_id=user_id,connection=conn)
    if token is None:
        raise auth_error(409,"USERNAME_EXISTS","账号已存在，请更换账号")
    attach_cookies(response,request,token,csrf)
    return {"user_id":str(user_id)}
