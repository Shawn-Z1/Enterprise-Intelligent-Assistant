import logging
from time import perf_counter
from contextlib import asynccontextmanager
from app.ids import new_id

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import get_settings
from app.audit import AuditRecorder
from app.database import create_app_pool
from app.dependencies import check_dependencies
from app.events import InfrastructureEvents
from app.identity_routes import router as identity_router
from app.chat_routes import router as chat_router
from app.knowledge_routes import router as knowledge_router
from app.knowledge_storage import KnowledgeStorage
from app.rag_index import RagIndex
from app.rag_service import RagService
from app.web_routes import router as web_router
from app.action_routes import router as action_router
from app.sandbox_routes import router as sandbox_router
from app.sandbox_queue import router as sandbox_queue_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger('httpx').setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """接收应用，创建受限数据库池与审计服务；关闭时释放连接池。"""
    app.state.settings = get_settings()
    app.state.events = InfrastructureEvents()
    app.state.pool = await create_app_pool(app.state.settings)
    app.state.knowledge_storage = KnowledgeStorage(app.state.settings)
    app.state.rag_index = RagIndex(app.state.settings)
    app.state.audit = AuditRecorder(app.state.pool, "api", (
        app.state.settings.postgres_password.get_secret_value(),
        app.state.settings.business_db_password.get_secret_value(),
        app.state.settings.app_minio_secret_key.get_secret_value(),
        app.state.settings.primary_api_key.get_secret_value(),
        app.state.settings.fallback_api_key.get_secret_value(),
        app.state.settings.review_api_key.get_secret_value(),
        app.state.settings.web_search_api_key.get_secret_value(),app.state.settings.sandbox_control_token.get_secret_value()))
    app.state.rag = RagService(app.state.pool,app.state.settings,app.state.rag_index,app.state.knowledge_storage,app.state.audit)
    try:
        trace_id = new_id()
        await app.state.audit.append(trace_id, "service.started", {"service": "api", "milestone": "M07C"})
        app.state.events.record(trace_id, "service.started", {"service": "api", "milestone": "M07C"})
        yield
    finally:
        await app.state.rag_index.close()
        await app.state.pool.close()


app = FastAPI(title="知行企业智能助理", version="0.7.3", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
app.include_router(identity_router)
app.include_router(chat_router)
app.include_router(knowledge_router)
app.include_router(web_router)
app.include_router(action_router)
app.include_router(sandbox_router)
app.include_router(sandbox_queue_router)


@app.middleware("http")
async def trace_request(request: Request, call_next):
    """接收请求和下游处理器；生成服务端追踪 ID，返回带统一追踪响应头的响应。"""
    # 追踪 ID 由服务端产生，不信任客户端字段作为身份或权限依据。
    request.state.trace_id = new_id()
    # 健康探针保持独立，数据库故障时 live 仍可响应；普通 API 必须写入审计。
    audit_required = request.url.path.startswith("/api/") and not request.url.path.startswith("/api/health/")
    span_id = new_id()
    started = perf_counter()
    if audit_required:
        try:
            await app.state.audit.append(request.state.trace_id, "http.started", {
                "method": request.method, "path": request.url.path}, span_id=span_id, span_name="http.request")
        except Exception:
            return error_response(request, "AUDIT_UNAVAILABLE", "审计存储不可用，本次请求未放行", 503)
    try:
        response = await call_next(request)
    except Exception as error:
        response = await unexpected_error(request, error)
    if audit_required:
        try:
            is_stream = response.headers.get('content-type','').startswith('text/event-stream')
            await app.state.audit.append(request.state.trace_id, "http.stream_opened" if is_stream else "http.completed", {
                "method": request.method, "path": request.url.path, "status_code": response.status_code},
                span_id=span_id, span_name="http.request", duration_ms=(perf_counter()-started)*1000)
        except Exception:
            return error_response(request, "AUDIT_UNAVAILABLE", "审计存储不可用，本次请求未放行", 503)
        if is_stream:
            original_body = response.body_iterator

            async def audited_stream():
                """无参数透传既有 SSE 字节，连接结束后记录真实流持续时间，不把刚发响应头记作任务完成。"""
                try:
                    async for part in original_body:
                        yield part
                finally:
                    await app.state.audit.append(request.state.trace_id,'http.stream_closed',{'path':request.url.path},
                        span_id=span_id,span_name='http.request',duration_ms=(perf_counter()-started)*1000)

            response.body_iterator = audited_stream()
    response.headers["X-Trace-ID"] = str(request.state.trace_id)
    response.headers["Cache-Control"] = "no-store"
    return response


def error_response(request: Request, code: str, message: str, status: int) -> JSONResponse:
    """接收请求、错误码、中文说明及 HTTP 状态；返回不包含敏感输入的错误信封。"""
    return JSONResponse(status_code=status, headers={"X-Trace-ID": str(request.state.trace_id),"Cache-Control":"no-store"},
                        content={"error": {"code": code, "message": message},
                                                   "trace_id": str(request.state.trace_id)})


@app.exception_handler(HTTPException)
async def http_error(request: Request, error: HTTPException):
    """接收路由错误；保留状态码并返回统一安全格式，不回显请求正文。"""
    detail = error.detail if isinstance(error.detail,dict) else {}
    return error_response(request,detail.get("code","HTTP_ERROR"),detail.get("message","请求的资源或操作不可用"),error.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    """接收参数校验错误；返回 422 信封，不记录密码等未经验证的输入。"""
    fields = [str(item["loc"][-1]) for item in error.errors() if item["loc"]]
    context = getattr(request.state,"auth_context",None)
    await app.state.audit.append(request.state.trace_id,"request.validation_rejected",{"fields":fields},user_id=context.user_id if context else None)
    return error_response(request, "VALIDATION_ERROR", "请求字段不符合要求，请检查账号格式、密码长度或分配的部门职级", 422)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, error: Exception):
    """接收未处理异常，记录安全错误类型；返回 500 信封及可定位的追踪 ID。"""
    app.state.events.record(request.state.trace_id, "service.error", {"error_type": type(error).__name__})
    return error_response(request, "INTERNAL_ERROR", "服务发生异常，请根据追踪编号检查日志", 500)


@app.get("/api/health/live")
async def live(request: Request):
    """接收请求；仅返回 API 进程存活状态，不将它宣称为依赖就绪。"""
    return {"status": "alive", "service": "api", "milestone": "M07C", "trace_id": str(request.state.trace_id)}


@app.get("/api/health/ready")
async def ready(request: Request):
    """接收请求并执行真实依赖探针；全部可用返回 200，否则返回 503 与逐项结果。"""
    dependencies = await check_dependencies(app.state.settings)
    all_ready = all(item.status == "ready" for item in dependencies.values())
    app.state.events.record(request.state.trace_id, "dependencies.checked",
                           {name: item.status for name, item in dependencies.items()})
    return JSONResponse(status_code=200 if all_ready else 503, content={
        "status": "ready" if all_ready else "not_ready", "service": "api",
        "dependencies": {name: item.model_dump() for name, item in dependencies.items()},
        "trace_id": str(request.state.trace_id),
    })


@app.get("/api/events")
async def events(request: Request, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    """接收事件序号和条数；返回 M01 基础设施事件，明确进程内缓存与保留限制。"""
    return {"events": [event.model_dump(mode="json") for event in app.state.events.read(after, limit)],
            "scope": "infrastructure_only", "storage": "process_memory", "durable": False,
            "capacity": 100, "trace_id": str(request.state.trace_id)}
