import asyncio
import json
import logging
import os
import time
from pathlib import Path
from uuid import uuid4

from app.config import get_settings
from app.dependencies import check_dependencies
from app.events import InfrastructureEvents

logging.basicConfig(level=logging.INFO, format="%(message)s")


async def run() -> None:
    """无参数运行基础 worker；定期验证依赖并写心跳，不伪造尚未实现的业务任务。"""
    settings = get_settings()
    events = InfrastructureEvents()
    trace_id = uuid4()
    state_path = Path(settings.worker_state_path)
    events.record(trace_id, "service.started", {"service": "worker", "mode": "infrastructure_only"})
    while True:
        results = await check_dependencies(settings)
        # 心跳同时要求进程持续运行与真实依赖就绪，避免陈旧文件被误判为健康。
        state = {"timestamp": time.time(), "pid": os.getpid(),
                 "ready": all(result.status == "ready" for result in results.values()),
                 "dependencies": {name: item.status for name, item in results.items()}}
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(state_path)
        events.record(trace_id, "worker.heartbeat", state["dependencies"])
        await asyncio.sleep(settings.worker_interval_seconds)


if __name__ == "__main__":
    asyncio.run(run())
