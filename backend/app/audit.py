import re
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import UUID, uuid4

import asyncpg
from pydantic import BaseModel, Field, model_validator


class Usage(BaseModel):
    """审计用量契约：未知值保持空，估算值必须说明模型和方法。"""

    source: Literal["reported", "estimated", "unavailable"] = "unavailable"
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    method: str | None = None
    model: str | None = None

    @model_validator(mode="after")
    def validate_source(self):
        """检查当前来源与 token 字段一致；返回自身，不允许用零冒充未知。"""
        values = (self.input_tokens, self.output_tokens, self.total_tokens)
        if self.source == "unavailable" and any(value is not None for value in values):
            raise ValueError("未知用量必须保持空值")
        if self.source != "unavailable" and all(value is None for value in values):
            raise ValueError("已报告或估算用量至少提供一项数值")
        if self.source == "estimated" and (not self.method or not self.model):
            raise ValueError("估算用量必须记录模型与方法")
        return self


def redact_secrets(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """接收原始 JSON 与已知运行密钥；递归遮蔽凭证字段，保留业务正文和用量。"""
    sensitive = {"password", "passwordhash", "apikey", "authorization", "cookie", "setcookie",
                 "accesstoken", "refreshtoken", "token", "secret", "secretkey", "signature"}
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.sub(r"[^a-z0-9]", "", str(key).lower()) in sensitive
                else redact_secrets(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if len(secret) >= 6:
                value = value.replace(secret, "[REDACTED]")
    return value


class AuditRecorder:
    """通过受限连接追加永久审计；支持参与调用方事务，提供内部只读查询。"""

    def __init__(self, pool: asyncpg.Pool | None, service: str, secrets: tuple[str, ...] = ()):
        """接收应用连接池、服务名和运行密钥；保存依赖，不读取或输出密钥。"""
        self.pool = pool
        self.service = service
        self.secrets = secrets

    @asynccontextmanager
    async def _transaction(self, connection: asyncpg.Connection | None):
        """接收可选事务连接；复用现有事务或从池新建，产出连接并保证失败回滚。"""
        if connection is not None:
            if not connection.is_in_transaction():
                raise RuntimeError("外部审计连接必须已进入事务")
            yield connection
        else:
            if self.pool is None:
                raise RuntimeError("审计连接池未配置")
            async with self.pool.acquire() as acquired:
                async with acquired.transaction():
                    yield acquired

    async def append(self, trace_id: UUID, event_type: str, raw_json: dict, *, span_id: UUID | None = None,
                     parent_span_id: UUID | None = None, span_name: str = "operation", user_id: UUID | None = None,
                     conversation_id: UUID | None = None, run_id: UUID | None = None, call_id: str | None = None,
                     usage: Usage | None = None, duration_ms: float | None = None,
                     connection: asyncpg.Connection | None = None) -> dict:
        """接收 trace/span、身份快照、原始 JSON 和用量；事务内追加并返回事件编号及 UTC 时间。"""
        # 每次动作可生成独立 span；同一 trace 下按 event_id 排列所有追加记录。
        span_id = span_id or uuid4()
        usage = usage or Usage()
        payload = redact_secrets(raw_json, self.secrets)
        async with self._transaction(connection) as conn:
            await conn.execute("""INSERT INTO app_audit.traces(trace_id,user_id,conversation_id,run_id,service)
                               VALUES($1,$2,$3,$4,$5) ON CONFLICT (trace_id) DO NOTHING""",
                               trace_id, user_id, conversation_id, run_id, self.service)
            await conn.execute("""INSERT INTO app_audit.spans(span_id,trace_id,parent_span_id,name)
                               VALUES($1,$2,$3,$4) ON CONFLICT (span_id) DO NOTHING""",
                               span_id, trace_id, parent_span_id, span_name)
            row = await conn.fetchrow("""INSERT INTO app_audit.events
                (trace_id,span_id,user_id,conversation_id,run_id,call_id,event_type,raw_json,
                 usage_source,input_tokens,output_tokens,total_tokens,usage_method,model,duration_ms)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                RETURNING event_id,trace_id,span_id,created_at""",
                trace_id, span_id, user_id, conversation_id, run_id, call_id, event_type, payload,
                usage.source, usage.input_tokens, usage.output_tokens, usage.total_tokens,
                usage.method, usage.model, duration_ms)
            return dict(row)

    async def read_trace(self, trace_id: UUID) -> list[dict]:
        """接收 trace ID，通过内部受限连接读取有序事件；返回列表，不作为公共审计接口。"""
        if self.pool is None:
            raise RuntimeError("审计连接池未配置")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM app_audit.events WHERE trace_id=$1 ORDER BY event_id", trace_id)
            return [dict(row) for row in rows]
