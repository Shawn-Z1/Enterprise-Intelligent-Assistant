import argparse
import asyncio
import json
import os
from pathlib import Path
from app.ids import new_id, ShortId

import httpx

from app.audit import AuditRecorder
from app.config import get_settings
from app.database import create_app_pool


async def snapshot(pool) -> dict:
    """接收应用池，读取账号、历史与业务计数；返回不含密码哈希的持久化摘要。"""
    async with pool.acquire() as conn:
        accounts = await conn.fetch("SELECT username,role,grade,business_query_enabled,employee_id::text AS employee_id,md5(password_hash) AS hash_fingerprint FROM app_system.users ORDER BY username")
        # 应用身份不直接读业务表，业务数据计数通过独立业务连接取得。
        return {"accounts": [dict(row) for row in accounts],
                "messages": await conn.fetchval("SELECT count(*) FROM app_runtime.messages"),
                "conversations": await conn.fetchval("SELECT count(*) FROM app_runtime.conversations")}


async def run(phase: str, trace: ShortId | None) -> dict:
    """接收 prepare/check 与可选追踪 ID；实际验证 API 和永久审计并返回安全 JSON 证据。"""
    settings = get_settings()
    pool = await create_app_pool(settings)
    audit = AuditRecorder(pool, "M02-runtime-verification")
    try:
        async with pool.acquire() as conn:
            application_role = await conn.fetchval("SELECT current_user")
            is_superuser = await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
            assert application_role == settings.postgres_user and not is_superuser
        if phase == "prepare":
            trace = new_id()
            event = await audit.append(trace, "verification.persistence.marker", {"text":"M02 容器重启前的永久审计标记"})
        rows = await audit.read_trace(trace)
        assert len(rows) == 1 and rows[0]["raw_json"]["text"] == "M02 容器重启前的永久审计标记"
        checks = []
        # 生产默认检查 web 主入口；开发模式由验收脚本指定内部 API 地址。
        async with httpx.AsyncClient(base_url=os.environ.get('VERIFICATION_API_URL','http://web'), timeout=15) as client:
            ready = await client.get("/api/health/ready")
            assert ready.status_code == 200 and ready.json()["status"] == "ready"
            checks.append({"name":"指定入口真实依赖就绪", "passed":True, "evidence":ready.json()})
            for method, path in (("GET","/api/audit/events"), ("PATCH","/api/audit/events/test"), ("DELETE","/api/audit/events/test")):
                response = await client.request(method, path)
                assert response.status_code == 404, response.text
                request_trace = str(response.headers["X-Trace-ID"])
                logged = await audit.read_trace(request_trace)
                assert logged and logged[-1]["raw_json"]["status_code"] == 404
                checks.append({"name":f"{method} 审计公共接口不可用且请求已记录", "passed":True,
                               "trace_id":str(request_trace), "event_id":logged[-1]["event_id"]})
        return {"phase":phase, "application_role":application_role, "application_is_superuser":is_superuser,
                "trace_id":str(trace), "event_id":rows[0]["event_id"],
                "created_at":rows[0]["created_at"].isoformat(), "snapshot":await snapshot(pool), "checks":checks}
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M02 真实服务与重启验收")
    parser.add_argument("phase", choices=("prepare","check"))
    parser.add_argument("--trace", type=str)
    parser.add_argument("--state-file")
    args = parser.parse_args()
    before = None
    if args.state_file:
        before = json.loads(Path(args.state_file).read_text(encoding="utf-8-sig"))
        args.trace = str(before["trace_id"])
    if args.phase == "check" and args.trace is None:
        parser.error("check 需要 --trace")
    result = asyncio.run(run(args.phase,args.trace))
    if before:
        for key in ("trace_id","event_id","created_at","snapshot"):
            assert result[key] == before[key], f"重启后持久数据发生变化：{key}"
        result["persistence_passed"] = True
    print(json.dumps(result, ensure_ascii=False, default=str))
