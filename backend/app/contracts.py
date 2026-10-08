from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel


class DependencyStatus(BaseModel):
    """单项依赖探针结果；不携带密码、连接串或原始异常正文。"""

    status: Literal["ready", "unavailable"]
    target: str
    latency_ms: float
    detail: str


class RunEvent(BaseModel):
    """统一事件信封；M01 仅基础设施事件，业务运行标识暂为空。"""

    event_id: int
    trace_id: UUID
    run_id: UUID | None = None
    timestamp: datetime
    type: str
    stage: str = "infrastructure"
    data: dict[str, Any]
