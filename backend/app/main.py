import logging
from time import perf_counter
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.config import get_settings
from app.audit import AuditRecorder
from app.database import create_app_pool
from app.dependencies import check_dependencies
from app.events import InfrastructureEvents

logging.basicConfig(level=logging.INFO, format="%(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """接收应用，创建受限数据库池与审计服务；关闭时释放连接池。"""
    app.state.settings = get_settings()
    app.state.events = InfrastructureEvents()
    app.state.pool = await create_app_pool(app.state.settings)
    app.state.audit = AuditRecorder(app.state.pool, "api", (
        app.state.settings.postgres_password.get_secret_value(),
        app.state.settings.business_db_password.get_secret_value(),
        app.state.settings.app_minio_secret_key.get_secret_value()))
    try:
        trace_id = uuid4()
        await app.state.audit.append(trace_id, "service.started", {"service": "api", "milestone": "M02"})
        app.state.events.record(trace_id, "service.started", {"service": "api", "milestone": "M02"})
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="知行基础服务", version="0.2.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)


@app.middleware("http")
async def trace_request(request: Request, call_next):
    """接收请求和下游处理器；生成服务端追踪 ID，返回带统一追踪响应头的响应。"""
    # 追踪 ID 由服务端产生，不信任客户端字段作为身份或权限依据。
    request.state.trace_id = uuid4()
    # 健康探针保持独立，数据库故障时 live 仍可响应；普通 API 必须写入审计。
    audit_required = request.url.path.startswith("/api/") and not request.url.path.startswith("/api/health/")
    span_id = uuid4()
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
            await app.state.audit.append(request.state.trace_id, "http.completed", {
                "method": request.method, "path": request.url.path, "status_code": response.status_code},
                span_id=span_id, span_name="http.request", duration_ms=(perf_counter()-started)*1000)
        except Exception:
            return error_response(request, "AUDIT_UNAVAILABLE", "审计存储不可用，本次请求未放行", 503)
    response.headers["X-Trace-ID"] = str(request.state.trace_id)
    return response


def error_response(request: Request, code: str, message: str, status: int) -> JSONResponse:
    """接收请求、错误码、中文说明及 HTTP 状态；返回不包含敏感输入的错误信封。"""
    return JSONResponse(status_code=status, headers={"X-Trace-ID": str(request.state.trace_id)},
                        content={"error": {"code": code, "message": message},
                                                   "trace_id": str(request.state.trace_id)})


@app.exception_handler(HTTPException)
async def http_error(request: Request, error: HTTPException):
    """接收路由错误；保留状态码并返回统一安全格式，不回显请求正文。"""
    return error_response(request, "HTTP_ERROR", "请求的资源或操作不可用", error.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    """接收参数校验错误；返回 422 信封，不记录密码等未经验证的输入。"""
    return error_response(request, "VALIDATION_ERROR", "请求参数不符合接口约定", 422)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, error: Exception):
    """接收未处理异常，记录安全错误类型；返回 500 信封及可定位的追踪 ID。"""
    app.state.events.record(request.state.trace_id, "service.error", {"error_type": type(error).__name__})
    return error_response(request, "INTERNAL_ERROR", "服务发生异常，请根据追踪编号检查日志", 500)


@app.get("/api/health/live")
async def live(request: Request):
    """接收请求；仅返回 API 进程存活状态，不将它宣称为依赖就绪。"""
    return {"status": "alive", "service": "api", "milestone": "M02", "trace_id": str(request.state.trace_id)}


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
